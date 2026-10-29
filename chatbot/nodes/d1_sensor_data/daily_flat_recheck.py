"""Evidence-driven recheck of uncovered daily activity using the shared flat contract.

The diagnostic query decides only whether a recheck is warranted. The LLM,
not this module, writes the answer query from the same sanitized plan used by
the primary path. Both queries go through the ordinary read-only KQL guard.
"""

from __future__ import annotations

import json
import logging
import math
import re
from typing import Any, Callable, Dict, List, Mapping, Optional

from langchain_core.output_parsers import StrOutputParser

from chatbot.config import get_settings
from chatbot.nodes.common.support import clean_model_text, llm
from chatbot.policies.reactive_query_scope import reactive_query_bounds
from chatbot.policies.kql_errors import is_query_validation_error
from chatbot.policies.kql_safety import (
    _all_tabular_function_arguments, inspect_query_safety, repair_query_deterministically,
)
from chatbot.policies.query_algebra_kql import _ACTIVITY_METRICS, _literal, _string_list
from chatbot.policies.semantic_resources import prompt_resource_values
from chatbot.prompts import load_chat_prompt, load_prompt
from .repair_sensor_query import repair_query_with_llm


logger = logging.getLogger(__name__)
_PROMPT = "d1_sensor_data/recheck_daily_with_flat.txt"
_SCHEMA = load_prompt("d1_sensor_data/adx_schema_context.txt")


def _requested_activity_types(plan: Mapping[str, Any]) -> List[str]:
    metrics = plan.get("metrics")
    if not isinstance(metrics, list):
        return []
    result: List[str] = []
    for metric in metrics:
        spec = _ACTIVITY_METRICS.get(str(metric or ""))
        if spec is not None:
            result.extend(spec.activity_types)
    return list(dict.fromkeys(result))


def build_daily_flat_probe(plan: Mapping[str, Any], user_id: str, primary_kql: str) -> Optional[str]:
    """Find activity Type/day keys present in flat but absent in the daily MV.

    This is a bounded coverage diagnostic, not an answer template. It never
    guesses values or chooses a dataset-specific schema.
    """

    resources = prompt_resource_values()
    daily = resources["activity_daily_resource"]
    flat = resources["health_samples_resource"]
    types = _requested_activity_types(plan)
    if not types or not user_id or daily.lower() not in primary_kql.lower():
        return None
    if plan.get("time_of_day") or str(plan.get("granularity") or "").lower() == "intraday":
        return None
    try:
        start, end = reactive_query_bounds(dict(plan))
    except (TypeError, ValueError, KeyError):
        return None
    type_list = _string_list(types)
    return (
        f"let TargetUserId={_literal(user_id)};\n"
        f"let StartLocal={start};\nlet EndLocal={end};\n"
        f"let Daily={daily}(TargetUserId, StartLocal, EndLocal)\n"
        "| where UserId == TargetUserId and LocalDate >= StartLocal and LocalDate < EndLocal\n"
        f"| where Type in ({type_list}) and isnotnull(ValueSumExact)\n"
        "| summarize DailyRows=count() by Day=LocalDate, Type;\n"
        f"let Flat={flat}(TargetUserId, StartLocal, EndLocal)\n"
        "| where UserId == TargetUserId and DateFrom >= StartLocal and DateFrom < EndLocal\n"
        f"| where Type in ({type_list}) and isnotnull(ValueNumber)\n"
        "| extend Day=startofday(DateFrom), DurationHours=todouble((DateTo-DateFrom)/1h)\n"
        "| summarize FlatRows=count(), DayTotalRows=countif(DateFrom == Day and DateTo <= Day + 1d and DurationHours >= 20), "
        "IntervalRows=countif(DateFrom != Day or DurationHours < 20), "
        "Sources=dcount(strcat(tostring(SourcePlatform), ':', tostring(SourceName))), "
        "FirstDateFrom=min(DateFrom), LastDateTo=max(DateTo), "
        "Units=make_set(tostring(Unit), 5) by Day, Type;\n"
        "Flat | join kind=leftouter Daily on Day, Type\n"
        "| where coalesce(DailyRows, 0) == 0\n"
        "| extend TotalUsableDailyRows=toscalar(Daily | summarize Total=sum(DailyRows))\n"
        "| project Day, Type, FlatRows, DayTotalRows, IntervalRows, Sources, TotalUsableDailyRows, "
        "FirstDateFrom, LastDateTo, Units\n"
        "| sort by Day asc, Type asc | take 100"
    )


def _as_rows(raw: Any) -> Optional[List[Dict[str, Any]]]:
    if isinstance(raw, str):
        if raw.lower().startswith("error"):
            return None
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return None
    return [row for row in raw if isinstance(row, dict)] if isinstance(raw, list) else None


def _confirmed_daily_evidence(gaps: List[Dict[str, Any]]) -> Optional[int]:
    """Read whole-window usable coverage metadata; gaps alone cannot prove zero."""
    counts = [row.get("TotalUsableDailyRows") for row in gaps]
    if not counts or any(
        isinstance(value, bool) or not isinstance(value, (int, float))
        or not math.isfinite(value) or value < 0 or int(value) != value
        for value in counts
    ):
        return None
    return int(counts[0]) if all(value == counts[0] for value in counts) else None


def _safe_model_error(exc: Exception) -> Dict[str, Any]:
    """Expose only known credential-free adapter diagnostics, not arbitrary text."""
    known = {
        "Selected model returned an incomplete/failed response": "model_incomplete_response",
        "Selected model returned no complete text choice": "model_incomplete_choice",
        "Selected model returned an incomplete/failed message": "model_incomplete_message",
        "Selected model returned no final text (possibly exhausted its reasoning budget)": "model_no_final_text",
    }
    text = str(exc)
    if text in known:
        return {"type": type(exc).__name__, "kind": known[text], "reason": text}
    http_status = re.fullmatch(
        r"Model [A-Za-z0-9_.-]+ \((?:responses|chat|anthropic)\): HTTP (\d{3}); "
        r"check deployment, endpoint, key, quota and supported parameters", text,
    )
    if http_status:
        status = int(http_status.group(1))
        return {"type": type(exc).__name__, "kind": "model_http_error", "http_status": status,
                "reason": f"Selected model endpoint returned HTTP {status}; check configuration, quota and supported parameters."}
    safe_suffixes = {
        "network/timeout error": ("model_network_timeout", "Selected model request failed due to a network/timeout error."),
        "invalid response or timeout configuration": ("model_invalid_response", "Selected model returned invalid JSON, or timeout configuration is invalid."),
        "expected a JSON response object": ("model_invalid_response", "Selected model did not return a JSON response object."),
    }
    for suffix, (kind, reason) in safe_suffixes.items():
        if re.fullmatch(r"Model [A-Za-z0-9_.-]+: " + re.escape(suffix), text):
            return {"type": type(exc).__name__, "kind": kind, "reason": reason}
    return {"type": type(exc).__name__, "kind": "unrecognized_exception",
            "reason": "Recheck failed; unrecognized exception details were omitted to protect credentials."}


def recheck_daily_with_flat(
    *,
    plan: Dict[str, Any],
    user_id: str,
    question: str,
    primary_kql: str,
    primary_rows: Any,
    execute_query: Callable[[str], Any],
) -> Optional[Dict[str, Any]]:
    """Return a guarded recheck result, or a diagnostic-only incomplete result.

    Failures never silently replace the primary result. A detected coverage gap
    is retained so the final answer cannot present a partial daily aggregate
    as a complete one.
    """

    probe = build_daily_flat_probe(plan, user_id, primary_kql)
    if not probe:
        return None
    decision = inspect_query_safety(probe, user_id)
    if not decision["is_safe"]:
        logger.warning("Daily/flat coverage probe failed KQL guard: %s", decision["reason"])
        return None
    try:
        gaps = _as_rows(execute_query(probe))
    except Exception as exc:
        logger.warning("Daily/flat coverage probe failed: %s", type(exc).__name__)
        gaps = None
    if gaps is None:
        return {"coverage": {
            "daily_flat_coverage_unverified": True,
            "recheck_status": "probe_failed",
        }}
    if not gaps:
        return None

    daily_usable_rows = _confirmed_daily_evidence(gaps)
    require_daily_merge = daily_usable_rows != 0

    context = {
        "daily_flat_gap_detected": True,
        "daily_flat_gap_sample": gaps[:100],
        "daily_flat_gap_sample_truncated": len(gaps) >= 100,
        "recheck_status": "not_completed",
        "daily_usable_rows_in_window": daily_usable_rows,
        "daily_evidence_state": "confirmed_empty" if not require_daily_merge else "mixed_or_unconfirmed",
    }
    try:
        start, end = reactive_query_bounds(plan)
        prompt = load_chat_prompt(_PROMPT)
        raw_kql = (prompt | llm | StrOutputParser()).invoke(
            {
                "schema": _SCHEMA.format(user_id=user_id),
                "user_id": user_id,
                "question": question,
                "plan_json": json.dumps(plan, ensure_ascii=False, default=str),
                "primary_kql": primary_kql,
                "primary_rows": json.dumps(primary_rows, ensure_ascii=False, default=str)[:12000],
                "gap_evidence": json.dumps(gaps[:100], ensure_ascii=False, default=str),
                "daily_evidence_state": context["daily_evidence_state"],
                "daily_usable_rows_in_window": daily_usable_rows,
                "start_local_bound": start,
                "end_local_bound": end,
                **prompt_resource_values(),
            }
        )
        candidate, repairs = repair_query_deterministically(clean_model_text(raw_kql))
        candidate = clean_model_text(candidate)
        pre_guard_kql = candidate
        llm_repairs = 0
        repair_limit = min(1, max(0, int(get_settings().max_kql_repair_attempts)))
        repair_history: List[Dict[str, Any]] = []
        resources = prompt_resource_values()
        required_sources = [resources["health_samples_resource"]]
        if require_daily_merge:
            required_sources.append(resources["activity_daily_resource"])
        def has_required_sources(query: str) -> bool:
            return all(
                bool(_all_tabular_function_arguments(query, name))
                for name in required_sources
            )

        def outcome(status: str, **fields: Any) -> Dict[str, Any]:
            context["recheck_status"] = status
            return {
                "coverage": context, "candidate_kql": candidate,
                "pre_guard_kql": pre_guard_kql, "repairs": repairs,
                "llm_repair_attempts": llm_repairs, "repair_history": repair_history,
                **fields,
            }

        def repair_candidate(origin: str, error_hint: str) -> bool:
            nonlocal candidate, llm_repairs
            llm_repairs += 1
            history = {"attempt": llm_repairs, "origin": origin,
                       "failed_kql": candidate, "error": error_hint[:2000]}
            repair_history.append(history)
            try:
                repaired = repair_query_with_llm(
                    user_id, question, candidate,
                    error_hint[:2000] + ". Retain the same trusted user, bounds and plan semantics. Required evidence streams: "
                    + ", ".join(required_sources) + ".",
                    plan,
                )
                candidate, additional_repairs = repair_query_deterministically(repaired)
                candidate = clean_model_text(candidate)
                repairs.extend(additional_repairs)
                history["candidate_kql"] = candidate
                return True
            except Exception as exc:
                diagnostic = _safe_model_error(exc)
                logger.warning("Daily/flat query repair failed: %s (%s)", diagnostic["type"], diagnostic["kind"])
                history["repair_error"] = diagnostic
                return False

        # A single budget is shared by guard and server validation repairs.
        # Every candidate, including an empty or malicious repair, re-enters
        # the complete read-only guard before any subsequent ADX execution.
        while True:
            decision = inspect_query_safety(candidate, user_id)
            if repair_history and repair_history[-1].get("candidate_kql") == candidate:
                repair_history[-1]["guard_pass"] = bool(decision["is_safe"])
                repair_history[-1]["guard_reason"] = decision["reason"]
            if not has_required_sources(candidate):
                return outcome("missing_required_evidence_stream", guard_reason=decision["reason"])
            if not decision["is_safe"]:
                if decision.get("should_repair") and llm_repairs < repair_limit:
                    if not repair_candidate("guard", str(decision["reason"])):
                        return outcome("repair_failed", guard_reason=decision["reason"])
                    continue
                return outcome("guard_rejected", guard_reason=decision["reason"])
            try:
                raw_result = execute_query(candidate)
            except Exception as exc:
                # Server/compiler exceptions follow the same bounded policy
                # as returned error strings, not the generation-failure path.
                raw_result = "Error (ADX): " + str(exc)
            rows = _as_rows(raw_result)
            if rows is not None:
                break
            error_hint = str(raw_result).replace("\r", " ").replace("\n", " ")[:2000]
            if is_query_validation_error(error_hint) and llm_repairs < repair_limit:
                if not repair_candidate("adx_validation", "ADX query error: " + error_hint):
                    return outcome("repair_failed", adx_error=error_hint)
                continue
            return outcome("adx_failed", adx_error=error_hint)
        if not rows or any(
            row.get("Value") is None
            or str(row.get("ResultStatus") or "").lower() == "no_metric_evidence"
            for row in rows
        ):
            # A successful query returning null/no evidence is an actual result,
            # not a retryable execution error. Preserve its rows rather than the
            # primary partial aggregate; downstream status remains insufficient.
            return outcome("no_evidence_after_recheck", rows=rows, kql=candidate,
                           rows_checked=len(rows), execution_completed=True)
        context["daily_flat_gap_resolved"] = True
        return outcome("completed", rows=rows, kql=candidate)
    except Exception as exc:
        diagnostic = _safe_model_error(exc)
        logger.warning("Daily/flat LLM recheck failed: %s (%s)", diagnostic["type"], diagnostic["kind"])
        context["recheck_status"] = "generation_failed"
        return {"coverage": context, "generation_error_type": diagnostic["type"],
                "generation_error_kind": diagnostic["kind"], "generation_error_reason": diagnostic["reason"],
                **({"generation_http_status": diagnostic["http_status"]} if "http_status" in diagnostic else {})}
