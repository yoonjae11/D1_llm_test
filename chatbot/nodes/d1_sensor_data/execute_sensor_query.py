"""LangGraph node that executes guard-approved KQL against ADX."""

import datetime
import json
import time
from typing import Any, Dict, List, Tuple

from chatbot.graph.state import AgentState
from chatbot.config import get_settings, normalize_kql_generate_method
from chatbot.services.adx import ADXQueryPort, default_adx_service
from chatbot.nodes.common.support import (
    clean_model_text as _clean_model_text,
    legacy_debug as _debug,
    elapsed_ms as _elapsed_ms,
    format_kql_for_log as _format_kql_for_log,
    latest_question as _latest_question,
    logger,
    safe_json_loads as _safe_json_loads,
)
from chatbot.policies.result_processing import (
    normalize_result_values,
    trim_records_for_plan,
)
from chatbot.policies.kql_generation import (
    build_deterministic_query,
    build_fallback_query,
    build_independent_metric_queries,
)
from chatbot.policies.kql_safety import inspect_query_safety
from chatbot.policies.time_ranges import compute_window_bounds
from .daily_flat_recheck import recheck_daily_with_flat


adx_manager: ADXQueryPort = default_adx_service()


def _build_retry_candidates(
    *,
    primary_query: str,
    user_id: str,
    question: str,
    plan: Dict[str, Any],
    generation_method: str = "deterministic",
) -> List[Tuple[str, str]]:
    """Return de-duplicated policy-built fallbacks after an ADX failure."""

    if str(generation_method or "").strip().lower() != "deterministic":
        # An explicit LLM experiment must not silently switch producers after
        # ADX rejects its query. The caller reports the original failure.
        return []

    deterministic = _clean_model_text(build_deterministic_query(user_id, question, plan) or "")
    # A typed computation must either retry its exact compiler output or fail.
    # The broad legacy fallback understands metrics but not conditions,
    # selectors, or entity filters and would therefore change the answer.
    fallback = ""
    if plan.get("schema_version") != 2:
        fallback = _clean_model_text(build_fallback_query(user_id, plan) or "")
    seen = {_clean_model_text(primary_query)}
    candidates: List[Tuple[str, str]] = []
    for label, candidate in (
        ("deterministic fallback", deterministic),
        ("hard fallback", fallback),
    ):
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        decision = inspect_query_safety(candidate, user_id)
        if not decision.get("is_safe"):
            logger.warning(
                "Skipping unsafe ADX retry candidate %s: %s",
                label,
                decision.get("reason"),
            )
            continue
        candidates.append((label, candidate))
    return candidates


def _parse_row_date(value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""
    try:
        return datetime.date.fromisoformat(text[:10]).isoformat()
    except Exception:
        return ""


def _coverage_context(plan: Dict[str, Any], rows: Any) -> Dict[str, Any]:
    if not isinstance(plan, dict) or not isinstance(rows, list):
        return {}
    if plan.get("schema_version") == 2:
        computation = (
            plan.get("computation")
            if isinstance(plan.get("computation"), dict)
            else {}
        )

        def safe_count(value: Any) -> int:
            try:
                return max(0, int(value))
            except (TypeError, ValueError):
                return 0

        evidence_by_metric: Dict[str, int] = {}
        status_by_metric: Dict[str, str] = {}
        # An omitted counter is unknown, not an observed zero-cardinality set.
        base_rows = matched_rows = selected_rows = None
        for row in rows:
            if not isinstance(row, dict):
                continue
            metric = str(row.get("Metric") or "").strip()
            if "BaseCoverageRows" in row:
                base_rows = max(base_rows or 0, safe_count(row["BaseCoverageRows"]))
            if "MatchedRows" in row:
                matched_rows = max(matched_rows or 0, safe_count(row["MatchedRows"]))
            if "SelectedRows" in row:
                selected_rows = max(selected_rows or 0, safe_count(row["SelectedRows"]))
            if metric:
                row_evidence = safe_count(row.get("EvidenceRows"))
                if str(row.get("Aggregation") or "").strip().lower() == "none":
                    evidence_by_metric[metric] = (
                        evidence_by_metric.get(metric, 0) + row_evidence
                    )
                else:
                    evidence_by_metric[metric] = row_evidence
                status_by_metric[metric] = str(
                    row.get("ResultStatus") or "unknown"
                ).strip()
        context = {
            "policy": "typed_computation_evidence",
            "entity": str(computation.get("entity") or ""),
            "base_coverage_rows": base_rows,
            "matched_rows": matched_rows,
            "selected_rows": selected_rows,
            "evidence_rows_by_metric": evidence_by_metric,
            "result_status_by_metric": status_by_metric,
        }
        outputs = (
            computation.get("outputs")
            if isinstance(computation.get("outputs"), list)
            else []
        )
        series_metrics = [
            str(output.get("metric") or "").strip()
            for output in outputs
            if isinstance(output, dict)
            and str(output.get("aggregation") or "").strip().lower() == "none"
            and str(output.get("metric") or "").strip()
        ]
        # Short day/night series need an explicit missing-date map so the final
        # answer can say "no data" rather than silently omitting or zero-filling
        # dates. Session lists intentionally do not imply one expected event per
        # calendar day.
        restricted_output = bool(computation.get("selector") or computation.get("conditions")
                                 or computation.get("filters"))
        context["date_coverage_known"] = False
        if restricted_output:
            context["coverage_scope"] = "selected_or_filtered_output_only"
        # A selected minimum or eligible subset is not an exhaustive series.
        # Inferring missing dates from such rows fabricates coverage evidence.
        if series_metrics and context["entity"] in {"day", "night"} and not restricted_output:
            time_range = (
                plan.get("time_range")
                if isinstance(plan.get("time_range"), dict)
                else {}
            )
            try:
                start, end = compute_window_bounds(
                    time_range, str(plan.get("timezone_iana") or "")
                )
            except Exception:
                start = end = None
            expected_dates = []
            current = start
            while (
                isinstance(current, datetime.date)
                and isinstance(end, datetime.date)
                and current <= end
                and len(expected_dates) < 370
            ):
                expected_dates.append(current.isoformat())
                current += datetime.timedelta(days=1)
            if expected_dates:
                expected_set = set(expected_dates)
                observed_by_metric: Dict[str, set[str]] = {
                    metric: set() for metric in series_metrics
                }
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    metric = str(row.get("Metric") or "").strip()
                    period = _parse_row_date(row.get("Period"))
                    if metric in observed_by_metric and period in expected_set:
                        observed_by_metric[metric].add(period)
                observed_union = set().union(*observed_by_metric.values())
                missing_by_metric = {
                    metric: [
                        date_value
                        for date_value in expected_dates
                        if date_value not in observed_by_metric[metric]
                    ]
                    for metric in series_metrics
                }
                context.update(
                    {
                        "expected_day_count": len(expected_dates),
                        "date_coverage_known": True,
                        "observed_day_count": len(observed_union),
                        "missing_dates": [
                            date_value
                            for date_value in expected_dates
                            if date_value not in observed_union
                        ],
                        "has_gap": len(observed_union) < len(expected_dates),
                        "observed_dates_by_metric": {
                            metric: sorted(values)
                            for metric, values in observed_by_metric.items()
                        },
                        "missing_dates_by_metric": missing_by_metric,
                        "has_gap_by_metric": {
                            metric: bool(missing_dates)
                            for metric, missing_dates in missing_by_metric.items()
                        },
                        "missing_day_policy": "do_not_fill_missing_days_with_zero",
                    }
                )
        return context
    time_range = plan.get("time_range") if isinstance(plan.get("time_range"), dict) else {}
    metrics = plan.get("metrics") if isinstance(plan.get("metrics"), list) else []
    if not time_range:
        return {}
    try:
        start, end = compute_window_bounds(time_range, str(plan.get("timezone_iana") or ""))
    except Exception:
        return {}
    expected_dates = []
    current = start
    while current <= end and len(expected_dates) < 370:
        expected_dates.append(current.isoformat())
        current += datetime.timedelta(days=1)
    expected_date_set = set(expected_dates)
    observed_dates = []
    requested_metrics = [
        str(metric).strip()
        for metric in metrics
        if str(metric).strip()
    ]
    observed_dates_by_metric: Dict[str, set[str]] = {
        metric: set() for metric in requested_metrics
    }
    saw_metric_period = False
    for row in rows:
        if not isinstance(row, dict):
            continue
        # Scalar aggregates and latest-date metadata cannot establish which
        # individual days were observed. Only actual dated result buckets can
        # support a missing-day claim.
        for key in ("LocalDate", "Period", "HourStartLocal"):
            parsed = _parse_row_date(row.get(key))
            if parsed:
                observed_dates.append(parsed)
                break
        # Per-metric coverage is safe only for results that expose an actual
        # time bucket. Aggregate metadata such as LatestDateFromLocal does not
        # prove that every preceding day was observed.
        period_date = ""
        for key in ("Period", "LocalDate", "HourStartLocal"):
            period_date = _parse_row_date(row.get(key))
            if period_date:
                break
        if not period_date or period_date not in expected_date_set:
            continue
        metric_label = str(row.get("Metric") or "").strip()
        if metric_label:
            observed_dates_by_metric.setdefault(metric_label, set()).add(period_date)
            saw_metric_period = True
        elif len(requested_metrics) == 1:
            observed_dates_by_metric[requested_metrics[0]].add(period_date)
            saw_metric_period = True

    observed_set = sorted({item for item in observed_dates if item in expected_date_set})
    missing = (
        [item for item in expected_dates if item not in set(observed_set)]
        if observed_set else []
    )
    context = {
        "expected_day_count": len(expected_dates),
        "observed_day_count": len(observed_set),
        "missing_dates": missing,
        "has_gap": bool(missing and observed_set),
        "metrics": metrics,
        "policy": "do_not_fill_missing_days_with_zero",
    }
    if saw_metric_period:
        metric_names = list(dict.fromkeys(requested_metrics + list(observed_dates_by_metric)))
        observed_by_metric = {
            metric: sorted(observed_dates_by_metric.get(metric, set()))
            for metric in metric_names
        }
        missing_by_metric = {
            metric: [
                date_value
                for date_value in expected_dates
                if date_value not in observed_dates_by_metric.get(metric, set())
            ]
            for metric in metric_names
        }
        context.update(
            {
                "observed_dates_by_metric": observed_by_metric,
                "missing_dates_by_metric": missing_by_metric,
                "has_gap_by_metric": {
                    metric: bool(missing_dates)
                    for metric, missing_dates in missing_by_metric.items()
                },
            }
        )
    return context


def execute_sensor_query(state: AgentState):
    """
    Execute generated KQL against ADX.
    """
    _debug("\n[Node] Query Executor")
    kql = state.get("kql_query")
    question = _latest_question(state)
    user_id = state.get("user_id", "")
    plan = state.get("plan") if isinstance(state.get("plan"), dict) else {}
    settings = get_settings()
    raw_generation_trace = state.get("kql_generation_trace")
    traced_method = (
        raw_generation_trace.get("method")
        if isinstance(raw_generation_trace, dict)
        else None
    )
    generation_method = normalize_kql_generate_method(
        settings.kql_generate_method if traced_method is None else traced_method
    )
    max_result_rows = max(1, int(settings.max_result_rows))
    generation_trace = (
        dict(raw_generation_trace) if isinstance(raw_generation_trace, dict) else {}
    )
    generation_trace["method"] = generation_method
    if state.get("guard_status") not in {None, "execute"}:
        message = str(state.get("error") or "KQL did not pass the read-only guard.")
        _debug(f"  -> Guard did not allow execution: {message}")
        return {"query_result": "[]", "error": message, "execution_status": "blocked"}

    if not kql:
        _debug("  -> KQL is empty. Skipping execution.")
        return {"query_result": "[]", "error": "KQL is empty.", "execution_status": "error"}

    def _normalize_success_result(raw_data: Any) -> Dict[str, Any]:
        if isinstance(raw_data, str):
            parsed = _safe_json_loads(raw_data, default=[])
            _debug(f"  -> Records fetched: {len(parsed) if isinstance(parsed, list) else 0}")
            normalized = normalize_result_values(
                trim_records_for_plan(parsed, plan, max_rows=max_result_rows)
            )
            return {"query_result": json.dumps(normalized, ensure_ascii=False, default=str), "coverage_context": _coverage_context(plan, parsed), "error": None, "execution_status": "success"}
        if isinstance(raw_data, list):
            _debug(f"  -> Records fetched: {len(raw_data)}")
            normalized = normalize_result_values(
                trim_records_for_plan(raw_data, plan, max_rows=max_result_rows)
            )
            return {"query_result": json.dumps(normalized, ensure_ascii=False, default=str), "coverage_context": _coverage_context(plan, raw_data), "error": None, "execution_status": "success"}
        _debug("  -> Non-list result fetched.")
        normalized = normalize_result_values(raw_data)
        return {"query_result": json.dumps(normalized, ensure_ascii=False, default=str), "coverage_context": {}, "error": None, "execution_status": "success"}

    def _short_error_text(raw: Any) -> str:
        text = str(raw or "").replace("\r", " ").replace("\n", " ").strip()
        if len(text) > 220:
            return text[:220] + "..."
        return text

    def _run_query_once(query_text: str, label: str) -> Any:
        started = time.perf_counter()
        result = adx_manager.execute_query(query_text, suppress_error_log=True)
        _debug(f"  -> ADX {label} latency: {_elapsed_ms(started)} ms")
        return result

    def _recover_independent_metrics(failure: Any) -> Dict[str, Any] | None:
        """Preserve healthy resources when a schema-v1 union fails."""

        if generation_method != "deterministic":
            return None
        failure_text = str(failure or "").lower()
        global_failure_markers = (
            "not initialized",
            "unauthorized",
            "forbidden",
            "authentication",
            "certificate",
            "network",
            "connection",
            "timed out",
            "timeout",
            "temporarily unavailable",
            "too many requests",
            "429",
            "503",
        )
        if any(marker in failure_text for marker in global_failure_markers):
            return None
        candidates = build_independent_metric_queries(user_id, plan)
        if not candidates:
            return None

        combined_rows: List[Dict[str, Any]] = []
        successful_metrics: List[str] = []
        failed_metrics: List[str] = []
        for metric, candidate_kql in candidates:
            decision = inspect_query_safety(candidate_kql, user_id)
            if not decision.get("is_safe"):
                failed_metrics.append(metric)
                logger.warning(
                    "Skipping unsafe independent metric recovery for %s: %s",
                    metric,
                    decision.get("reason"),
                )
                continue
            try:
                raw = _run_query_once(candidate_kql, f"independent metric {metric}")
            except Exception as exc:
                failed_metrics.append(metric)
                logger.warning(
                    "Independent metric recovery failed for %s: %s",
                    metric,
                    type(exc).__name__,
                )
                continue
            if isinstance(raw, str) and raw.lower().startswith("error"):
                failed_metrics.append(metric)
                logger.warning(
                    "Independent metric recovery failed for %s: %s",
                    metric,
                    _short_error_text(raw),
                )
                continue

            parsed_rows = raw
            if isinstance(raw, str):
                parsed_rows = _safe_json_loads(raw, default=None)
            if isinstance(parsed_rows, dict):
                parsed_rows = [parsed_rows]
            if not isinstance(parsed_rows, list):
                failed_metrics.append(metric)
                logger.warning(
                    "Independent metric recovery returned an invalid payload for %s.",
                    metric,
                )
                continue

            successful_metrics.append(metric)
            for row in parsed_rows:
                if not isinstance(row, dict):
                    continue
                normalized_row = dict(row)
                normalized_row.setdefault("Metric", metric)
                combined_rows.append(normalized_row)

        if not successful_metrics:
            return None

        generation_trace["repair_attempted"] = True
        generation_trace["recovery_mode"] = "independent_metric_queries"
        generation_trace["recovered_metrics"] = successful_metrics
        generation_trace["failed_metrics"] = failed_metrics
        success = _normalize_success_result(combined_rows)
        coverage = dict(success.get("coverage_context") or {})
        coverage.update(
            {
                "partial_query_recovery": True,
                "successful_metrics": successful_metrics,
                "failed_metrics": failed_metrics,
            }
        )
        success["coverage_context"] = coverage
        success["kql_generation_trace"] = dict(generation_trace)
        return success

    try:
        primary_kql = _clean_model_text(str(kql))
        data = _run_query_once(primary_kql, "primary")
        if isinstance(data, str) and data.lower().startswith("error"):
            _debug(f"  -> Primary query failed in ADX: {_short_error_text(data)}")
            for label, retry_kql in _build_retry_candidates(
                primary_query=primary_kql,
                user_id=user_id,
                question=question,
                plan=plan,
                generation_method=generation_method,
            ):
                generation_trace["repair_attempted"] = True
                generation_trace["final_kql"] = retry_kql
                _debug(f"  -> Retrying with {label}.")
                _debug("  -> Retry KQL:")
                _debug(_format_kql_for_log(retry_kql))
                retry_data = _run_query_once(retry_kql, "retry")
                if isinstance(retry_data, str) and retry_data.lower().startswith("error"):
                    _debug(f"  -> {label} failed: {_short_error_text(retry_data)}")
                    continue
                success = _normalize_success_result(retry_data)
                success["kql_query"] = retry_kql
                success["kql_generation_trace"] = dict(generation_trace)
                return success
            recovered = _recover_independent_metrics(data)
            if recovered is not None:
                return recovered
            logger.warning("ADX query failed after retries: %s", _short_error_text(data))
            result = {"query_result": "[]", "error": data, "execution_status": "error"}
            if generation_trace.get("repair_attempted"):
                result["kql_generation_trace"] = generation_trace
            return result
        success = _normalize_success_result(data)
        if generation_method == "llm":
            primary_rows = _safe_json_loads(data, default=[]) if isinstance(data, str) else data
            recheck = recheck_daily_with_flat(
                plan=plan,
                user_id=str(user_id),
                question=question,
                primary_kql=primary_kql,
                primary_rows=primary_rows,
                execute_query=lambda query: _run_query_once(query, "daily/flat recheck"),
            )
            if recheck is not None:
                if "rows" in recheck:
                    success = _normalize_success_result(recheck["rows"])
                    success["kql_query"] = recheck["kql"]
                coverage = dict(success.get("coverage_context") or {})
                coverage.update(recheck["coverage"])
                if (
                    recheck["coverage"]["recheck_status"] != "completed"
                    and recheck["coverage"].get("daily_flat_gap_detected")
                ):
                    coverage["has_gap"] = True
                success["coverage_context"] = coverage
                generation_trace["daily_flat_recheck"] = {
                    "status": recheck["coverage"]["recheck_status"],
                    "primary_kql": primary_kql,
                    "final_kql": recheck.get("kql"),
                    "candidate_kql": recheck.get("candidate_kql"),
                    "pre_guard_kql": recheck.get("pre_guard_kql"),
                    "guard_reason": recheck.get("guard_reason"),
                    "adx_error": recheck.get("adx_error"),
                    "generation_error_type": recheck.get("generation_error_type"),
                    "generation_error_kind": recheck.get("generation_error_kind"),
                    "generation_error_reason": recheck.get("generation_error_reason"),
                    "repairs": recheck.get("repairs", []),
                    "repair_history": recheck.get("repair_history", []),
                    "llm_repair_attempts": recheck.get("llm_repair_attempts", 0),
                }
                success["kql_generation_trace"] = dict(generation_trace)
        return success
    except Exception as exc:
        logger.exception("ADX query execution failed")
        _debug(f"  -> Execution exception: {exc}")
        primary_kql = _clean_model_text(str(kql))
        for label, retry_kql in _build_retry_candidates(
            primary_query=primary_kql,
            user_id=user_id,
            question=question,
            plan=plan,
            generation_method=generation_method,
        ):
            try:
                generation_trace["repair_attempted"] = True
                generation_trace["final_kql"] = retry_kql
                _debug(f"  -> Retrying after exception with {label}.")
                retry_data = _run_query_once(retry_kql, "retry")
                if isinstance(retry_data, str) and retry_data.lower().startswith("error"):
                    _debug(f"  -> {label} failed: {_short_error_text(retry_data)}")
                    continue
                success = _normalize_success_result(retry_data)
                success["kql_query"] = retry_kql
                success["kql_generation_trace"] = dict(generation_trace)
                return success
            except Exception as retry_exc:
                _debug(f"  -> {label} exception: {retry_exc}")
                continue
        recovered = _recover_independent_metrics(exc)
        if recovered is not None:
            return recovered
        result = {"query_result": "[]", "error": str(exc), "execution_status": "error"}
        if generation_trace.get("repair_attempted"):
            result["kql_generation_trace"] = generation_trace
        return result
