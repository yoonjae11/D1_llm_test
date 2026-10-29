"""LangGraph node that builds a guarded ADX query candidate."""

import hashlib
import json
import re
import time
from typing import Any, Dict, Mapping, Optional

from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate

from chatbot.graph.state import AgentState
from chatbot.config import get_settings, normalize_kql_generate_method
from chatbot.nodes.common.support import (
    clean_model_text as _clean_model_text,
    legacy_debug as _debug,
    elapsed_ms as _elapsed_ms,
    format_kql_for_log as _format_kql_for_log,
    latest_question as _latest_question,
    llm,
    logger,
    now_string,
)
from chatbot.policies.plan_sanitization import (
    sanitize_plan,
)
from chatbot.policies.kql_generation import (
    _kql_phone_window_for_plan,
    build_deterministic_query,
    build_fallback_query,
)
from chatbot.policies.semantic_resources import prompt_resource_values as _resource_prompt_values
from chatbot.policies.time_ranges import active_evaluation_time, evaluation_time_family
from chatbot.policies.reactive_query_scope import reactive_query_bounds, reference_clock_local, query_time_scope
from chatbot.prompts import load_role_prompt, load_prompt


ADX_SCHEMA_CONTEXT = load_prompt("d1_sensor_data/adx_schema_context.txt")
KQL_GENERATION_PROMPT_RESOURCE = "d1_sensor_data/generate_sensor_query.txt"
KQL_GENERATION_PROMPT_VERSION = "sha256:" + hashlib.sha256(
    (
        ADX_SCHEMA_CONTEXT
        + "\0"
        + load_prompt(KQL_GENERATION_PROMPT_RESOURCE)
    ).encode("utf-8")
).hexdigest()[:16]


def _configured_model_name(settings: Any, method: str) -> Optional[str]:
    """Return the configured generation model when the LLM path is selected."""

    if method != "llm":
        return None
    from chatbot.local_models import active_model
    profile = active_model()
    if profile is not None:
        return profile.deployment
    if str(settings.ai_provider or "").strip().lower() == "azure_openai_v1":
        value = settings.azure_openai_default_deployment
    else:
        value = settings.openai_chat_model
    return str(value or "").strip() or None


def _generation_trace(
    *,
    method: str,
    pre_guard_kql: Optional[str],
    latency_ms: int,
    model: Optional[str],
    guard_pass: Optional[bool] = None,
) -> Dict[str, Any]:
    """Build the stable trace shape completed by guard and repair nodes."""

    from chatbot.local_models import active_model
    profile = active_model()
    return {
        "method": method,
        "pre_guard_kql": pre_guard_kql or None,
        "guard_pass": guard_pass,
        "repair_attempted": False,
        "final_kql": None,
        "latency_ms": max(0, int(latency_ms)),
        "model": model,
        "temperature": (profile.temperature if profile is not None else 0) if method == "llm" else None,
        # The current LLM port returns text only. Keep explicit null fields so
        # evaluation traces distinguish unavailable provider usage metadata
        # from a zero-token or zero-cost call.
        "prompt_tokens": None,
        "completion_tokens": None,
        "total_tokens": None,
        "estimated_cost_usd": None,
        "prompt_version": KQL_GENERATION_PROMPT_VERSION if method == "llm" else None,
    }


def _query_reference_clock(plan: Dict[str, Any]) -> str:
    """Keep prompt context aligned with local-test bounds; production stays current."""
    return reference_clock_local(plan)


def _sleep_shape_conflicts(query: str, observations: dict) -> list[str]:
    """Detect an incompatible interval interpretation, not a question pattern."""
    if not re.search(r"\bSensorSleepNights\s*\(", query, re.IGNORECASE):
        return []
    fields = {"SLEEP_DEEP": ("DeepMin", "DeepPct"),
              "SLEEP_REM": ("RemMin", "RemPct"),
              "SLEEP_LIGHT": ("LightMin", "LightPct"),
              "SLEEP_AWAKE": ("AwakeMin", "AwakePct")}
    conflicts = []
    for entry in observations.get("observations", []):
        if (entry.get("resource") == "sleep.nightly"
                and entry.get("HasAsleepTrueRows") == 0
                and entry.get("HasAsleepNullRows", 0) > 0
                and re.search(r"\bHasAsleepDuration\s*==\s*true\b", query, re.IGNORECASE)):
            conflicts.append("The optional HasAsleepDuration flag is unknown for observed numeric "
                             "sleep records. A strict true filter would erase that evidence. "
                             "Use the requested recorded quantity from compatible numeric evidence; "
                             "unknown flags are not false flags.")
    for entry in observations.get("observations", []):
        kind = entry.get("Type")
        if (entry.get("resource") != "health.samples" or kind not in fields
                or entry.get("IntervalDurationMismatchRows", 0) <= 0):
            continue
        affected = (*fields[kind], "AsleepMin", "SleepEfficiencyPct")
        if any(re.search(r"\b" + field + r"\b", query, re.IGNORECASE) for field in affected):
            conflicts.append(f"{kind}: observed numeric duration differs from its observation window; "
                             "the interval-based nightly fields do not represent that recorded amount. "
                             "Use compatible recorded numeric Type evidence in SensorHealthSamples "
                             "instead of those derived nightly fields; keep the same record-date key "
                             "across eligibility and target streams.")
    return conflicts


def _workout_shape_conflicts(query: str, observations: dict) -> list[str]:
    """Check observed extraction compatibility, not question templates."""
    if "unusable_workout_payload" in query and re.search(
        r"\bValue\s*=\s*real\s*\(\s*null\s*\)", query, re.I,
    ):
        return []
    resources = _resource_prompt_values()
    uses_sessions = bool(re.search(
        r"\b" + re.escape(resources["workout_sessions_resource"]) + r"\s*\(", query, re.I,
    ))
    conflicts = []
    if uses_sessions:
        for entry in observations.get("observations", []):
            if entry.get("resource") != "workout.sessions" or entry.get("RowCount", 0) <= 0:
                continue
            if (entry.get("ActivityNameRows") == 0 and entry.get("ActivityCodeRows") == 0
                    and re.search(r"\b(?:ActivityName|ActivityCode)\b", query)):
                conflicts.append("Workout rows exist but normalized activity labels are unparsed. "
                                 "Use observed Value JSON paths if usable, not invented category literals.")
            for name in ("AverageHeartRateBpm", "Distance", "CaloriesKcal", "Steps",
                         "ActiveZoneMinutes", "Speed", "ElevationGain"):
                if entry.get(name + "Rows") == 0 and re.search(r"\b" + name + r"\b", query):
                    conflicts.append(f"Normalized {name} has no usable observations. Inspect the bounded "
                                     "Value schema for compatible recorded evidence before declaring absence.")
    payload = observations.get("workout_payload") or {}
    # A historical adapter can attach a fixed speed unit to a unitless payload.
    # Do not promote that label to measured unit evidence. Let the model choose
    # a documented payload unit or explicitly unitless recorded output.
    payload_fields = payload.get("fields", [])
    if (uses_sessions and payload.get("status") == "observed"
            and payload.get("object_rows", 0) > 0
            and not payload.get("sample_limit_reached") and not payload.get("schema_limit_reached")
            and any(field.get("path") == ["speed"] for field in payload_fields)
            and not any(field.get("path") in (["speedUnit"], ["speed", "unit"])
                        and field.get("nonnull_rows", 0) > 0 for field in payload_fields)
            and re.search(r"\bSpeedUnit\b", query, re.I)):
        contracts = observations.get("producer_measurement_contracts") or []
        speed_contract = next((item for item in contracts if item.get("type") == "WORKOUT"
                               and item.get("path") == ["speed"] and item.get("unit")), None)
        if speed_contract:
            conflicts.append("The SpeedUnit column is a fixed adapter label, not the recorded unit. "
                             "The declared producer unit of the recorded speed is "
                             + speed_contract["unit"] + ": compare and report Speed in that unit "
                             "(convert only the question's threshold) and do not use SpeedUnit.")
        else:
            conflicts.append("The SpeedUnit column is a fixed adapter label, not the recorded unit. "
                             "Report the recorded Speed unchanged with Unit=\"\" and do not use "
                             "SpeedUnit or replace Speed with distance/time.")
    for field in payload_fields:
        path = field.get("path") or []
        if (field.get("decoded_json_types") and path
                and all(isinstance(key, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) for key in path)
                and re.search(r"\." + r"\s*\.\s*".join(map(re.escape, path)) + r"\s*\.", query)):
            conflicts.append(f"Value slot {'.'.join(path)} is JSON encoded in a string. "
                             "Parse that slot with parse_json(tostring(slot)) before accessing children; "
                             "direct property access to its string contents returns missing evidence.")
    if (payload.get("status") == "unusable"
            and re.search(r"\b(?:parse_json|todynamic)\s*\(", query, re.I)
            and re.search(r"['\"]WORKOUT['\"]", query, re.I)):
        conflicts.append("The canonical Value payload is not a usable JSON object. "
                         "Do not infer zero matching workouts from unparsed category/metric fields.")
    return conflicts


def _selection_evidence_conflicts(query: str, plan: dict) -> list[str]:
    """Require actual cardinality evidence, not a requested selector count."""
    computation = plan.get("computation") or {}
    selector = computation.get("selector") or {}
    aggregate = any(output.get("aggregation") != "none"
                    for output in computation.get("outputs", []))
    outputs = computation.get("outputs", [])
    from chatbot.policies.domain_catalog import METRIC_DEFINITIONS
    output_resources = {METRIC_DEFINITIONS.get(output.get("metric"), {}).get("resource")
                        for output in outputs if output.get("metric")}
    cross_stream = any(
        METRIC_DEFINITIONS.get(condition.get("metric"), {}).get("resource") not in output_resources
        for condition in computation.get("conditions", [])
    ) or any(predicate.get("dimension") == "activity_name"
             for predicate in computation.get("filters", []))
    if (selector and computation.get("entity") == "day" and outputs
            and all(output.get("aggregation") == "none" for output in outputs) and cross_stream):
        fields_present = all(re.search(r"\b" + field + r"\b", query, re.I)
                             for field in ("Period", "EvidenceRows", "ResultStatus"))
        inner_only = bool(re.search(r"\bjoin\b", query, re.I)) and not re.search(
            r"\bkind\s*=\s*(?:leftouter|rightouter|fullouter)\b", query, re.I)
        if not fields_present or inner_only:
            return ["This dependent day lookup must retain the selected date as Period, "
                    "EvidenceRows and ResultStatus even if the target measurement is missing. "
                    "Select the identifying date before looking up the target. An inner-only "
                    "join drops that date on missing target evidence; use selected dates as "
                    "the preserved side of an outer lookup and return null/no_metric_evidence, "
                    "not a different date or an unanchored empty result."]
        if re.search(r"\barg_(?:max|min)\s*\(\s*Date(?:From|To)\b", query, re.I):
            return ["A date selected from the identifying stream does not request an additional "
                    "first/last target record. Preserve every deduplicated target measurement on "
                    "that date, with its observation identity. Deduplication selects a revision "
                    "by IngestionTime; selecting a target by DateFrom/DateTo changes the requested "
                    "population. Add a target reduction only if the question explicitly asks for it."]
    if (selector and aggregate and not re.search(r"\bSelectedRows\b", query, re.I)):
        return ["The aggregate of a selected subset does not retain SelectedRows. "
                "Count the actual selected entities before removing null optional metric values "
                "and include that count in the result; requested N is not observed cardinality."]
    return []


def _numeric_evidence_conflicts(query: str, plan: dict) -> list[str]:
    """Numeric reductions need evidence cardinality to distinguish empty sum=0."""
    outputs = (plan.get("computation") or {}).get("outputs", [])
    numeric_aggregate = any(output.get("metric") and output.get("aggregation") in {
        "sum", "mean", "min", "max", "median", "stddev_sample", "stddev_population",
    } for output in outputs)
    if (numeric_aggregate and not all(re.search(r"\b" + field + r"\b", query, re.I)
                                     for field in ("EvidenceRows", "ResultStatus"))):
        return ["This numeric aggregate lacks EvidenceRows or ResultStatus. Kusto sum of an "
                "empty input is zero, not a recorded zero. Retain usable numeric observation "
                "count and return null/no_metric_evidence when that count is zero. "
                "A genuine observed zero with a positive evidence count remains valid."]
    return []


def _measurement_scope_conflicts(query: str, plan: dict) -> list[str]:
    """Reject an identifiable catalog-subtype substitution, not author KQL.

    This deliberately checks only explicit Type predicates. It is not a full
    semantic proof or a required query shape; joins and reductions stay with
    the model, and an absent requested stream can still be queried for null.
    """
    from chatbot.policies.domain_catalog import METRIC_DEFINITIONS

    catalog_types = {}
    for metric, definition in METRIC_DEFINITIONS.items():
        if definition.get("resource") not in {
            "health.samples", "activity.daily", "activity.hourly", "activity.interval",
        }:
            continue
        types = set(re.findall(r"\bType\s*==\s*['\"]([^'\"]+)['\"]",
                               definition.get("summary", ""), re.I))
        if types:
            catalog_types[metric] = types
    computation = plan.get("computation") or {}
    requested = set(plan.get("metrics") or [])
    for member in ("outputs", "conditions"):
        requested.update(item.get("metric") for item in computation.get(member, []))
    expected = set().union(*(catalog_types.get(metric, set()) for metric in requested))
    if not expected:
        return []
    # Match evidence predicates, not comments or arbitrary labels in strings.
    from chatbot.policies.kql_safety import _mask_kql_literals_and_comments
    masked = _mask_kql_literals_and_comments(query)
    selected_types = set()
    for match in re.finditer(r"\bType\s*(?:==|=~|!=|!~)\s*(['\"])([^'\"]+)\1", query, re.I):
        if re.match(r"\bType\s*(?:==|=~)", masked[match.start():], re.I):
            selected_types.add(match.group(2).upper())
    known = set().union(*catalog_types.values())
    expected = {value.upper() for value in expected}
    substituted = selected_types & {value.upper() for value in known} - expected
    missing = expected - selected_types
    conflicts = []
    if substituted and missing:
        conflicts.append("The explicit Type predicates use unrequested measurement types "
                + ", ".join(sorted(substituted)) + " while omitting requested "
                + ", ".join(sorted(missing)) + ". Catalog quantities are not interchangeable. "
                "Keep the planned measurement scope; absent requested evidence means null, "
                "not substitution with a nearby available subtype.")
    # A condition stream cannot disappear into a different session attribute.
    # Accept equivalent expression shapes (including Type in (...) and named
    # scalar constants); this is a minimal dependency check, not a query parser.
    for condition in computation.get("conditions", []):
        condition_types = catalog_types.get(condition.get("metric"), set())
        if condition_types and not any(
            re.search(r"['\"]" + re.escape(value) + r"['\"]", query, re.I)
            for value in condition_types
        ):
            conflicts.append("The planned condition metric " + str(condition.get("metric"))
                + " requires its catalog measurement evidence ("
                + ", ".join(sorted(condition_types)) + "). The query omits that dependency. "
                "A workout attribute or sum of recorded workouts is not that day's full activity "
                "or physiology stream. Preserve the declared condition; unavailable evidence "
                "means null. Do not change its scope merely to obtain a numeric answer.")
    return conflicts


def _empty_rollup_conflicts(query: str, observations: dict) -> list[str]:
    """Observed empty rollups are not a reason to skip available flat data."""
    from chatbot.policies.kql_safety import _mask_kql_literals_and_comments
    statuses = observations.get("resource_status") or {}
    if statuses.get("activity.daily") != "empty" or statuses.get("health.samples") != "observed":
        return []
    daily_name = _resource_prompt_values()["activity_daily_resource"]
    if re.search(r"\b" + re.escape(daily_name) + r"\s*\(",
                 _mask_kql_literals_and_comments(query), re.I):
        return ["Successful bounded inventory found the daily rollup empty while flat records "
                "are available. Form the requested daily quantity from compatible deduplicated "
                "flat Type/unit evidence rather than issue an empty rollup query. If the needed "
                "flat measurement itself is absent, keep that absence as null; do not replace "
                "it with workout totals or another metric. Failed/unknown discovery is not absence."]
    return []


def _evidence_conflicts(query: str, observations: dict) -> list[str]:
    """Observed-data mismatches, recorded in the trace as diagnostics only."""
    return (_sleep_shape_conflicts(query, observations)
            + _workout_shape_conflicts(query, observations)
            + _empty_rollup_conflicts(query, observations))


def _metric_schema_context(plan: dict) -> list[dict]:
    """Expose catalog meanings without catalog query/reduction templates."""
    from chatbot.policies.availability import metric_adx_types
    from chatbot.policies.domain_catalog import METRIC_DEFINITIONS
    computation = plan.get("computation") or {}
    metrics = set(plan.get("metrics") or [])
    for member in ("outputs", "conditions"):
        metrics.update(item.get("metric") for item in computation.get(member, []))
    selector = computation.get("selector") or {}
    if selector.get("by_metric"):
        metrics.add(selector["by_metric"])
    context = []
    for metric in sorted(value for value in metrics if value):
        definition = METRIC_DEFINITIONS.get(metric)
        if definition:
            context.append({"metric": metric, "logical_resource": definition["resource"],
                            "catalog_flat_types": sorted(metric_adx_types(metric))})
    return context


def _query_schema_observations(observations: dict, plan: dict) -> dict:
    """Give the query model the complete observed inventory.

    The model chooses the stored evidence from what this user actually has.
    Narrowing the detail to the catalog's usual types made an absent usual
    type look like the only option (e.g. a once-a-day reading was ignored).
    """
    return observations


def _generate_kql_with_llm(user_id: str, question: str, plan: Dict[str, Any],
                         source_review: Optional[dict] = None,
                         query_feedback: Optional[str] = None) -> str:
    """Generate an LLM KQL candidate; the graph guard still decides execution."""

    resources = _resource_prompt_values()
    # Share date semantics with the deterministic path; the LLM still chooses
    # the metric, entity operations, and KQL shape from the sanitized plan.
    start_local, end_local = reactive_query_bounds(plan)
    from chatbot.adx_client import adx_manager
    from chatbot.services.sensor_inventory import observe_sensor_schema

    # Inspect bounded Type/unit/category metadata and heterogeneous JSON shape.
    # No gold labels, personal measurement values or dataset identity are used.
    observations = observe_sensor_schema(
        user_id, start_local, end_local, adx_manager.execute_query,
        include_workout_payload=True,
    )
    from chatbot.services.measurement_contract import producer_measurement_contracts
    declared_contracts = producer_measurement_contracts()
    if declared_contracts:
        observations["producer_measurement_contracts"] = declared_contracts
    system, user = load_role_prompt(KQL_GENERATION_PROMPT_RESOURCE)
    prompt = ChatPromptTemplate.from_messages([("system", system), ("human", user)])
    chain = prompt | llm | StrOutputParser()
    values = {
            "schema": ADX_SCHEMA_CONTEXT.format(user_id=user_id),
            "schema_observations": json.dumps(_query_schema_observations(observations, plan),
                                               ensure_ascii=False, default=str),
            "user_id": user_id,
            "question": question,
            "now": _query_reference_clock(plan),
            # These legacy catalog defaults are not availability decisions.
            # Let the query model select physical evidence from real inventory.
            # The catalog's usual Type per metric is not sent: it read as a fixed
            # metric->Type mapping. The model picks the stored type from the
            # question and the observed inventory (kept in the trace for audit).
            "plan_json": json.dumps({key: value for key, value in plan.items()
                                     if key not in {"tables_priority", "resources_priority"}},
                                    # Query metadata contains no retrieved values or gold.
                                    ensure_ascii=False),
            "start_local_bound": start_local,
            "end_local_bound": end_local,
            "query_feedback": query_feedback or "None",
            **resources,
        }
    model_plan = json.loads(values["plan_json"])
    model_plan["query_time_scope"] = query_time_scope(plan)
    values["plan_json"] = json.dumps(model_plan, ensure_ascii=False)
    candidate = _clean_model_text(chain.invoke(values))
    # The model reads the observed metadata itself and chooses the evidence.
    # The data-shape checks are recorded for diagnosis only; they no longer
    # trigger a code-directed rewrite. Guard, ADX and repair decide execution.
    if source_review is not None:
        source_review.update(attempted=False,
                             diagnostic_conflicts=_evidence_conflicts(candidate, observations),
                             schema_observations=observations,
                             metric_schema_context=_metric_schema_context(plan))
    return candidate


def generate_kql_candidate(
    user_id: str,
    question: str,
    sanitized_plan: Dict[str, Any],
    method: Optional[str] = None,
    query_feedback: Optional[str] = None,
) -> Dict[str, Any]:
    """Generate one KQL candidate through an explicit production strategy.

    ``method=None`` uses ``KQL_GENERATE_METHOD``. A trusted evaluation caller
    may pass ``deterministic`` or ``llm`` directly without mutating process
    environment. Both routes consume the same already-sanitized plan, and the
    returned trace always has the complete lifecycle shape expected by the
    downstream guard, repair, and executor nodes.
    """

    started = time.perf_counter()
    settings = get_settings()
    selected_method = normalize_kql_generate_method(
        settings.kql_generate_method if method is None else method
    )
    model_name = _configured_model_name(settings, selected_method)
    plan = sanitized_plan

    if not isinstance(plan, dict):
        trace = _generation_trace(
            method=selected_method,
            pre_guard_kql=None,
            latency_ms=_elapsed_ms(started),
            model=model_name,
        )
        return {
            "kql_query": None,
            "kql_source": "blocked_invalid_plan",
            "status": "blocked",
            "error": "KQL generation requires a sanitized plan object.",
            "error_type": "kql_generation",
            "kql_generation_trace": trace,
        }

    # An explicit but invalid/future typed computation must never degrade into
    # a legacy metric query that silently ignores its aggregation or filters.
    # The graph normally routes this state directly to the response node; keep
    # the node itself fail-closed for direct callers and rolling deployments.
    if plan.get("execution_ready") is False:
        trace = _generation_trace(
            method=selected_method,
            pre_guard_kql=None,
            latency_ms=_elapsed_ms(started),
            model=model_name,
        )
        return {
            "kql_query": None,
            "kql_source": "blocked_invalid_plan",
            "status": "needs_clarification",
            "error": None,
            "error_type": None,
            "kql_generation_trace": trace,
        }

    kql: Optional[str] = None
    kql_source = ""
    pre_guard_candidate: Optional[str] = None
    pre_guard_rejected = False
    producer_error_type = None
    producer_error_message = None
    source_review: dict = {}

    if selected_method == "deterministic":
        # This path is intentionally model-free. A compiler miss goes only to
        # the established deterministic fallback (v1) or fails closed (v2).
        try:
            kql = build_deterministic_query(user_id, question, plan)
            if kql:
                pre_guard_candidate = kql
                kql_source = "deterministic"
                _debug("  -> KQL source: deterministic")
        except Exception as exc:
            logger.warning("deterministic KQL build failed: %s", exc)
            kql = None

        # A validated typed plan must be compiled exactly or not executed at
        # all. Its selector/condition semantics cannot use the broad v1 query.
        if plan.get("schema_version") == 2 and not kql:
            blocked_plan = dict(plan)
            blocked_plan["execution_ready"] = False
            blocked_plan["needs_followup"] = True
            blocked_plan.setdefault(
                "followup_question",
                "I could not safely compile that calculation. Please restate "
                "the target metric and calculation.",
            )
            trace = _generation_trace(
                method=selected_method,
                pre_guard_kql=None,
                latency_ms=_elapsed_ms(started),
                model=model_name,
            )
            return {
                "plan": blocked_plan,
                "kql_query": None,
                "kql_source": "blocked_uncompiled_plan",
                "status": "needs_clarification",
                "error": None,
                "error_type": None,
                "kql_generation_trace": trace,
            }

        if not kql:
            _debug("  -> KQL source: deterministic hard fallback")
            try:
                kql = build_fallback_query(user_id, plan)
                pre_guard_candidate = kql
                kql_source = "deterministic_fallback"
            except Exception as exc:
                logger.warning("deterministic KQL fallback failed: %s", exc)
                kql = None
    else:
        # `llm` is the default producer, not a fallback layered on
        # top of the deterministic builder. It receives the same sanitized plan
        # and shared schema prompt, then remains subject to both safety guards.
        try:
            llm_started = time.perf_counter()
            candidate = _generate_kql_with_llm(user_id, question, plan, source_review,
                                               query_feedback=query_feedback)
            pre_guard_candidate = candidate
            _debug(f"  -> Query LLM latency: {_elapsed_ms(llm_started)} ms")
            # Generation and authorization are deliberately separate.  Return
            # every non-empty candidate to the same graph guard/repair path used
            # by deterministic KQL; no LLM-only allowlist may skew the paired
            # comparison or suppress a repairable guard result.
            if candidate:
                kql = candidate
                kql_source = "llm"
                _debug("  -> KQL source: llm")
            else:
                kql = None
                pre_guard_rejected = bool(candidate)
                logger.warning("LLM KQL generation did not produce a query.")
        except Exception as exc:
            logger.warning("LLM KQL generation failed: %s", exc)
            producer_error_type = type(exc).__name__
            producer_error_message = str(exc)
            kql = None
            pre_guard_rejected = bool(pre_guard_candidate)

    latency_ms = _elapsed_ms(started)
    _debug(f"  -> Query generation latency: {latency_ms} ms")
    clean_kql = _clean_model_text(kql or "")
    clean_pre_guard_kql = _clean_model_text(pre_guard_candidate or "")
    trace = _generation_trace(
        method=selected_method,
        pre_guard_kql=clean_pre_guard_kql or None,
        latency_ms=latency_ms,
        model=model_name,
        guard_pass=False if pre_guard_rejected else None,
    )
    if source_review:
        trace["source_review"] = source_review
    if not clean_kql:
        producer = "LLM" if selected_method == "llm" else "Deterministic"
        trace["producer_error_type"] = producer_error_type
        trace["producer_error_message"] = producer_error_message
        return {
            "kql_query": None,
            "kql_source": f"blocked_{selected_method}_generation",
            "status": "blocked",
            "kql_generation_trace": trace,
            "error": (f"{producer} query generation call failed ({producer_error_type}): {producer_error_message}"
                      if producer_error_type else f"{producer} KQL generation returned no query."),
            "error_type": "kql_generation",
        }
    _debug("  -> KQL:")
    _debug(_format_kql_for_log(clean_kql))
    return {
        "kql_query": clean_kql,
        "kql_source": kql_source,
        "status": "success",
        "error": None,
        "error_type": None,
        "kql_generation_trace": trace,
    }


def generate_sensor_query(state: AgentState):
    """Generate KQL from the current graph plan through the configured method."""

    _debug("\n[Node] Query Generator")
    question = _latest_question(state)
    user_id = str(state.get("user_id") or "")
    plan = state.get("plan")
    if not isinstance(plan, dict):
        last_plan = (
            state.get("last_plan")
            if isinstance(state.get("last_plan"), dict)
            else {}
        )
        plan = sanitize_plan({}, question, last_plan)

    # Pass the validated deployment setting explicitly so production and
    # trusted evaluation overrides share one implementation without relying on
    # ambient environment mutation inside the service.
    method = get_settings().kql_generate_method
    # Set only by the graph's evidence-feedback step within this same run.
    feedback = state.get("evidence_feedback_request")
    feedback = feedback if isinstance(feedback, Mapping) and feedback.get("message") else None
    generated = generate_kql_candidate(user_id, question, plan, method=method,
                                       query_feedback=feedback["message"] if feedback else None)
    status = str(generated.get("status") or "blocked")
    trace = generated.get("kql_generation_trace")
    if feedback and isinstance(trace, dict):
        trace["evidence_feedback"] = dict(feedback)
    result: Dict[str, Any] = {
        "kql_query": generated.get("kql_query"),
        "kql_source": generated.get("kql_source"),
        "kql_generation_status": status,
        "kql_generation_trace": trace,
    }
    if feedback:
        # A second, independent candidate: reset the first one's execution.
        result.update(evidence_feedback_used=True, evidence_feedback_request=None,
                      repair_attempts=0, guard_status=None, execution_status=None,
                      query_result="[]", error=None, error_type=None, adx_error=None,
                      coverage_context={})
    if isinstance(generated.get("plan"), dict):
        result["plan"] = generated["plan"]
    if status != "success":
        result["execution_status"] = status
        if generated.get("error"):
            result["error"] = generated["error"]
        if generated.get("error_type"):
            result["error_type"] = generated["error_type"]
    return result

__all__ = ["generate_kql_candidate", "generate_sensor_query"]
