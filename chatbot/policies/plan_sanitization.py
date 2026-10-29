"""Plan sanitisation and deterministic path helpers."""

import re
from typing import Any, Dict, List

from .domain_catalog import (
    VALID_CASE_TYPES,
    RESOURCE_PRIORITY,
    METRIC_DEFINITIONS,
    PHONE_PROFILE_METRICS,
)
from .metric_normalization import (
    _normalize_metrics,
    _normalize_metric_name,
    _apply_source_scope,
    _default_followup_question,
    _mentions_both_sources,
)
from .question_analysis import (
    _asks_for_aggregate,
    _asks_for_explicit_latest_wording,
    _asks_for_latest_sample,
    _asks_for_measurement_time,
    _is_contextual_followup,
    _is_comparison_question,
    _is_time_only_followup,
)
from .query_algebra import (
    SCHEMA_VERSION as COMPUTATION_SCHEMA_VERSION,
    computation_dependencies,
    has_v2_intent,
    sanitize_computation,
    validate_llm_computation_support,
)
from .query_algebra_kql import validate_computation_support
from .semantic_resources import (
    CONVERSATION_HISTORY,
    HEALTH_SAMPLES,
    WITHINGS_RESOURCE_IDS,
    logical_name,
    physical_name,
)
from .time_ranges import (
    _default_time_range,
    _has_policy_defined_time_expression,
    _parse_time_range_from_question,
    _parse_time_of_day_range_from_question,
    _normalize_time_of_day_range,
    _normalize_time_range,
    validate_v2_time_range as _validate_v2_time_range,
)


# Broad default metrics when LLM returns none, keyed by case_type.
_DEFAULT_METRICS_BY_CASE = {
    "monitoring": ["total_sleep_time", "steps", "heart_rate"],
    "factor_analysis": ["total_sleep_time", "sleep_efficiency", "heart_rate", "steps"],
    "prescription": ["total_sleep_time", "sleep_efficiency", "heart_rate", "steps"],
}

# Logical resources are the planner/runtime contract. Physical ADX object names
# are exposed below only through the deprecated compatibility facade.
_WITHINGS_RESOURCES = WITHINGS_RESOURCE_IDS
_CONVERSATION_HISTORY_RESOURCE = CONVERSATION_HISTORY
_HEALTH_SAMPLES_RESOURCE = HEALTH_SAMPLES
_CURATED_HEALTH_RESOURCES = tuple(
    resource
    for resource in RESOURCE_PRIORITY
    if resource not in _WITHINGS_RESOURCES
    and resource != _CONVERSATION_HISTORY_RESOURCE
)
_CURATED_HEALTH_TABLES = tuple(
    table
    for table in (physical_name(resource) for resource in _CURATED_HEALTH_RESOURCES)
    if table
)

_LATEST_SAMPLE_METRICS = {
    "heart_rate",
    "phone_heart_rate",
    "phone_resting_heart_rate",
}


def _as_logical_resource(value: Any) -> str:
    """Normalize a logical ID or legacy physical name without trusting text."""

    token = str(value or "").strip()
    if not token:
        return ""
    if token in RESOURCE_PRIORITY:
        return token
    try:
        return logical_name(token)
    except (KeyError, TypeError, ValueError):
        return ""


def _explicit_single_heart_metric(question: str) -> str:
    """Return a high-confidence single heart-rate metric from the current turn."""

    q = " ".join(str(question or "").lower().split())
    heart_tokens = ("heart rate", "heartrate", "bpm")
    if not any(token in q for token in heart_tokens):
        return ""
    # Do not collapse an explicit multi-metric request into heart rate alone.
    other_topics = (
        "step",
        "sleep",
        "weight",
        "blood pressure",
        "glucose",
        "temperature",
        "calorie",
        "distance",
        "hrv",
        "rmssd",
    )
    if any(token in q for token in other_topics):
        return ""
    if "resting" in q:
        return "phone_resting_heart_rate"
    if re.search(r"\b(?:maximum|max|peak|highest)\b", q):
        return "heart_rate_max"
    if re.search(r"\b(?:minimum|min|lowest)\b", q):
        return "heart_rate_min"
    return "heart_rate"


def _truthy_plan_flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "latest_sample"}


def _normalize_typed_plan_shape(candidate: Dict[str, Any]) -> Dict[str, Any]:
    """Remove redundant planner JSON fields without changing query semantics.

    A dimension or entity is not a measurement, an empty selector selects
    nothing, and a period belongs to the outer plan. Conflicting periods and
    nonempty selectors remain subject to the ordinary validator.
    """
    if not has_v2_intent(candidate) or not isinstance(candidate.get("computation"), dict):
        return candidate
    normalized = dict(candidate)
    computation = dict(candidate["computation"])
    # Some models place an explicit numeric metric predicate in `filters`
    # under the key `dimension`. The v2 schema calls that same predicate a
    # `condition`. Move only fully specified equivalent predicates; retain
    # the metric, operator, value and unit, and leave unknown shapes to repair.
    filters = computation.get("filters")
    conditions = computation.get("conditions", [])
    if isinstance(filters, list) and isinstance(conditions, list):
        retained = []
        relocated = []
        for item in filters:
            metric = _normalize_metric_name(str(item.get("dimension") or "")) if isinstance(item, dict) else ""
            if (isinstance(item, dict) and metric in METRIC_DEFINITIONS
                    and set(item) <= {"dimension", "operator", "value", "unit"}
                    and item.get("operator") in {"eq", "gt", "gte", "lt", "lte"}
                    and isinstance(item.get("value"), (int, float))
                    and not isinstance(item.get("value"), bool)):
                relocated.append({"metric": metric,
                                  **{key: value for key, value in item.items() if key != "dimension"}})
            else:
                retained.append(item)
        if relocated:
            computation["filters"] = retained
            computation["conditions"] = [*conditions, *relocated]
    if computation.get("selector") == {}:
        computation.pop("selector")
    nested_range = computation.get("time_range")
    if isinstance(nested_range, dict):
        if "time_range" not in normalized:
            normalized["time_range"] = nested_range
            computation.pop("time_range")
        elif nested_range == normalized["time_range"]:
            computation.pop("time_range")
    normalized["computation"] = computation

    metrics = normalized.get("metrics")
    if isinstance(metrics, list):
        dimensions = {
            str(item.get("dimension") or "").strip().lower()
            for item in computation.get("filters", [])
            if isinstance(item, dict)
        } if isinstance(computation.get("filters", []), list) else set()
        entity = str(computation.get("entity") or "").strip().lower()
        structural_names = dimensions | {entity}
        normalized["metrics"] = [
            metric for metric in metrics
            if not (
                isinstance(metric, str)
                and metric.strip().lower() in structural_names
                and metric.strip().lower() not in METRIC_DEFINITIONS
            )
        ]
    return normalized


def sanitize_plan(candidate: Dict[str, Any], question: str, last_plan: Dict[str, Any]) -> Dict[str, Any]:
    """Return the runtime-safe plan used by the planning node."""

    candidate = _normalize_typed_plan_shape(candidate)
    plan: Dict[str, Any] = {}
    candidate_v2_intent = has_v2_intent(candidate)
    inherited_v2_intent = False
    computation_errors: List[Dict[str, str]] = []

    # Case type: trust LLM, only validate.
    case_type = str(candidate.get("case_type", "monitoring")).strip().lower()
    if case_type not in VALID_CASE_TYPES:
        case_type = "monitoring"
    plan["case_type"] = case_type

    # A typed LLM plan owns time meaning, including latest searches versus
    # completed-period reductions. Text parsing only recovers missing/invalid
    # time structure; legacy plans retain their existing normalization.
    default_range = _default_time_range(case_type)
    time_range_parse_errors: List[Dict[str, str]] = []
    try:
        parsed_range, ambiguous_recent, has_explicit_time = (
            _parse_time_range_from_question(question)
        )
    except (ValueError, OverflowError):
        # Python deliberately refuses integers with thousands of digits, and
        # downstream date arithmetic may overflow even for shorter enormous
        # values. Keep legacy planning on its established fallback range; a v2
        # request is marked non-executable below instead of reaching KQL.
        parsed_range, ambiguous_recent, has_explicit_time = None, False, False
        time_range_parse_errors.append(
            {
                "code": "time_range_too_large",
                "path": "time_range.value",
                "message": "Schema v2 time ranges are limited to approximately ten years.",
            }
        )

    candidate_time_range = candidate.get("time_range")
    # A comparison has two model-selected periods. Parsing a single date
    # phrase from its question must not replace one side with the other.
    comparison_range = candidate.get("comparison_time_range")
    has_comparison_range = isinstance(comparison_range, dict)
    enforce_parsed_range = bool(
        parsed_range and _has_policy_defined_time_expression(question)
    )
    # Keep even an invalid typed period for the existing LLM schema repair.
    # A lexical parser can mistake a measurement condition (e.g. hours slept)
    # for the retrieval window and hide the actual model representation error.
    # Do not replace the declared period with that unrelated parsed duration.
    if candidate_v2_intent and isinstance(candidate_time_range, dict):
        v2_time_range_input = candidate_time_range
        time_range = _normalize_time_range(candidate_time_range, default_range)
    elif has_comparison_range and isinstance(candidate_time_range, dict):
        v2_time_range_input = candidate_time_range
        time_range = _normalize_time_range(candidate_time_range, default_range)
    elif parsed_range and (
        str(parsed_range.get("anchor", "")).lower() == "absolute"
        or enforce_parsed_range
    ):
        v2_time_range_input = parsed_range
        time_range = _normalize_time_range(parsed_range, default_range)
    elif isinstance(candidate_time_range, dict) and candidate_time_range.get("anchor"):
        v2_time_range_input = candidate_time_range
        time_range = _normalize_time_range(candidate_time_range, default_range)
    elif parsed_range:
        v2_time_range_input = parsed_range
        time_range = _normalize_time_range(parsed_range, default_range)
    else:
        v2_time_range_input = candidate_time_range
        time_range = _normalize_time_range(candidate_time_range, default_range)
    plan["time_range"] = time_range
    comparison_errors = []
    if "comparison_time_range" in candidate:
        for field, value in (("time_range", candidate_time_range),
                             ("comparison_time_range", comparison_range)):
            for error in _validate_v2_time_range(value):
                comparison_errors.append({**error, "path": error["path"].replace("time_range", field)})
        if not comparison_errors:
            plan["comparison_time_range"] = _normalize_time_range(comparison_range, default_range)
    time_of_day = _normalize_time_of_day_range(candidate.get("time_of_day"))
    if time_of_day is None:
        time_of_day = _parse_time_of_day_range_from_question(question)
    if time_of_day:
        plan["time_of_day"] = time_of_day

    # Metrics: trust LLM, fallback to broad defaults.
    metrics_raw = candidate.get("metrics", [])
    metric_tokens = metrics_raw if isinstance(metrics_raw, list) else []
    metrics = _normalize_metrics(metric_tokens)
    candidate_had_metrics = bool(metrics)
    candidate_supplied_metric_tokens = any(
        str(item or "").strip() for item in metric_tokens
    )
    unsupported_metrics: List[str] = []
    for item in metric_tokens:
        canonical = _normalize_metric_name(str(item or ""))
        if not canonical or canonical in METRIC_DEFINITIONS:
            continue
        # Keep only a bounded identifier for diagnostics/user feedback. Never
        # carry arbitrary planner text into a query plan.
        safe_name = re.sub(r"[^a-z0-9_]+", "_", canonical).strip("_")[:64]
        safe_name = safe_name or "unmapped_metric"
        if safe_name not in unsupported_metrics:
            unsupported_metrics.append(safe_name)

    # Follow-up handling: the planner LLM owns the semantic decision (its prompt
    # carries the follow-up few-shots). Structural text checks apply ONLY when the
    # LLM returned no plan at all, so a rule can never override the model's
    # reading of a multi-turn conversation.
    llm_plan_ok = bool(candidate)
    is_followup = bool(candidate.get("is_followup") or candidate.get("resolved_from_previous_plan"))
    if not llm_plan_ok:
        is_followup = _is_contextual_followup(question) or _is_time_only_followup(
            question
        )
    # A context-dependent clock-time-only turn after a latest measurement is
    # asking about that same observation. A complete question such as "What
    # time do I wake up today?" starts a new topic and must keep the planner's
    # sleep metric instead of inheriting the previous heart-rate metric.
    last_metrics = _normalize_metrics(
        last_plan.get("metrics", [])
        if isinstance(last_plan, dict) and isinstance(last_plan.get("metrics"), list)
        else []
    )
    legacy_latest_plan = isinstance(last_plan, dict) and "latest_sample" not in last_plan
    last_plan_was_latest = bool(
        isinstance(last_plan, dict)
        and _truthy_plan_flag(last_plan.get("latest_sample"))
    )
    measurement_time_followup = bool(
        isinstance(last_plan, dict)
        and not candidate_had_metrics
        and _is_contextual_followup(question)
        and _asks_for_measurement_time(question)
        and len(last_metrics) == 1
        and last_metrics[0] in _LATEST_SAMPLE_METRICS
        and (last_plan_was_latest or legacy_latest_plan)
    )
    if measurement_time_followup:
        is_followup = True
        metrics = list(last_metrics)

    explicit_v1 = str(candidate.get("schema_version") or "").strip() == "1"
    last_plan_has_v2_intent = bool(
        isinstance(last_plan, dict) and has_v2_intent(last_plan)
    )
    period_only_v2_followup = bool(
        is_followup
        and _is_time_only_followup(question)
        and not explicit_v1
        and last_plan_has_v2_intent
    )
    may_inherit_prior_plan_fields = bool(
        not last_plan_has_v2_intent or period_only_v2_followup
    )

    if is_followup and last_plan and may_inherit_prior_plan_fields:
        if not metrics and not candidate_supplied_metric_tokens:
            metrics = _normalize_metrics(last_plan.get("metrics", []))
        # Inherit the previous time range only when NEITHER the LLM nor the text
        # supplied one — an LLM-chosen anchor must never be overwritten by the
        # previous turn's range.
        llm_gave_time = isinstance(candidate_time_range, dict) and bool(candidate_time_range.get("anchor"))
        if measurement_time_followup:
            llm_gave_time = False
        if not has_explicit_time and not llm_gave_time:
            old_range = last_plan.get("time_range")
            if isinstance(old_range, dict):
                time_range = _normalize_time_range(old_range, time_range)
                plan["time_range"] = time_range
        # Inherit case_type only when the LLM did not choose one itself.
        llm_gave_case = str(candidate.get("case_type", "")).strip().lower() in VALID_CASE_TYPES
        if not llm_gave_case and last_plan.get("case_type") in VALID_CASE_TYPES:
            plan["case_type"] = str(last_plan["case_type"])

    # Typed computation plans describe calculation semantics without exposing
    # physical tables, columns, joins, or KQL. Only a genuine period-only turn
    # may reuse the prior typed operation. A generic model-produced follow-up
    # flag is insufficient: it could otherwise carry an old selector/condition
    # into a complete new topic. An explicit compatibility-version choice also
    # always wins over the previous v2 plan.
    inherited_v2_intent = bool(
        period_only_v2_followup and not candidate_v2_intent
    )
    v2_requested = candidate_v2_intent or inherited_v2_intent
    computation = None
    if candidate_v2_intent:
        computation, computation_errors = sanitize_computation(
            candidate,
            last_computation=last_plan if isinstance(last_plan, dict) else None,
            inherit=period_only_v2_followup,
        )
    elif inherited_v2_intent:
        computation, computation_errors = sanitize_computation(
            None,
            last_computation=last_plan,
            inherit=True,
        )

    # Structural validation and generation capability validation are separate.
    # The LLM can query supported sample/hour streams without forcing a daily
    # reduction merely because the legacy compiler lacks those entity grains.
    # Unsupported plans must never fall through to a changed legacy query.
    if computation is not None and not computation_errors:
        from chatbot.config import get_settings

        capability_check = (
            validate_llm_computation_support
            if get_settings().kql_generate_method == "llm"
            else validate_computation_support
        )
        computation_errors.extend(capability_check(computation))
    if v2_requested:
        range_errors = list(time_range_parse_errors)
        range_input = (v2_time_range_input if isinstance(v2_time_range_input, dict)
                       else plan.get("time_range"))
        inherited_range = bool(is_followup and last_plan and may_inherit_prior_plan_fields
                               and not has_explicit_time
                               and isinstance(last_plan.get("time_range"), dict))
        if (candidate_v2_intent and not isinstance(candidate_time_range, dict)
                and parsed_range is None and not inherited_range):
            # Missing/malformed typed periods are model representation errors.
            # Validate the original field so the existing planner repair sees
            # it; a legacy default must not silently narrow this new request.
            range_input = candidate_time_range
        range_errors.extend(
            _validate_v2_time_range(range_input)
        )
        # The normalized value is checked as well so inherited/default ranges
        # cannot bypass the bound. Deduplicate stable errors from raw and
        # normalized representations before exposing them to telemetry.
        range_errors.extend(_validate_v2_time_range(plan.get("time_range")))
        seen_range_errors = set()
        for error in range_errors:
            key = (error.get("code"), error.get("path"), error.get("message"))
            if key not in seen_range_errors:
                computation_errors.append(error)
                seen_range_errors.add(key)

    if v2_requested:
        requested_version = (
            candidate.get("schema_version")
            if candidate_v2_intent
            else last_plan.get("schema_version")
        )
        try:
            safe_version = int(requested_version)
        except (TypeError, ValueError):
            safe_version = 0
        plan["schema_version"] = safe_version
        plan["execution_ready"] = bool(
            safe_version == COMPUTATION_SCHEMA_VERSION
            and computation is not None
            and not computation_errors
        )
        if computation is not None:
            plan["computation"] = computation
            dependencies = computation_dependencies(computation)
            dependency_metrics: List[str] = []
            for output in computation.get("outputs", []):
                metric = str(output.get("metric") or "") if isinstance(output, dict) else ""
                if metric and metric not in dependency_metrics:
                    dependency_metrics.append(metric)
            for condition in computation.get("conditions", []):
                metric = str(condition.get("metric") or "") if isinstance(condition, dict) else ""
                if metric and metric not in dependency_metrics:
                    dependency_metrics.append(metric)
            selector = computation.get("selector")
            selector_metric = (
                str(selector.get("by_metric") or "")
                if isinstance(selector, dict)
                else ""
            )
            if selector_metric and selector_metric not in dependency_metrics:
                dependency_metrics.append(selector_metric)
            # Entity-only counts still need an evidence metric/resource in the
            # legacy plan inventory used by availability and response metadata.
            entity_default_metric = {
                "workout_session": "workout_count",
                "sleep_session": "phone_sleep_sessions",
                "night": "total_sleep_time",
            }
            entity_outputs = [
                str(output.get("entity") or "")
                for output in computation.get("outputs", [])
                if isinstance(output, dict) and output.get("entity")
            ]
            for entity in entity_outputs:
                default_metric = entity_default_metric.get(entity)
                if default_metric and default_metric not in dependency_metrics:
                    dependency_metrics.append(default_metric)
            if "day" in entity_outputs and any(
                isinstance(item, dict)
                and item.get("dimension") == "activity_name"
                for item in computation.get("filters", [])
            ):
                # A distinct-day workout question is sourced by normalized
                # workout sessions even though its output is the day entity.
                if "workout_count" not in dependency_metrics:
                    dependency_metrics.append("workout_count")
            metrics = _normalize_metrics(dependency_metrics)
            candidate_had_metrics = bool(metrics)
            candidate_supplied_metric_tokens = True
        if computation_errors:
            plan["validation_errors"] = computation_errors[:20]
    elif str(candidate.get("schema_version") or "").strip() == "1" or (
        is_followup
        and isinstance(last_plan, dict)
        and str(last_plan.get("schema_version") or "").strip() == "1"
    ):
        # Preserve the explicit compatibility version in persisted/diagnostic
        # plans while continuing to accept older plans that omitted it.
        plan["schema_version"] = 1

    # An absent inference may use a broad case default. Explicit but unknown
    # metric tokens must remain unsupported instead of silently turning into a
    # sleep/activity/heart-rate query.
    if not metrics and not candidate_supplied_metric_tokens and not v2_requested:
        metrics = list(_DEFAULT_METRICS_BY_CASE.get(plan["case_type"], _DEFAULT_METRICS_BY_CASE["monitoring"]))

    # Current/latest and explicit aggregate heart-rate questions are precise
    # single-metric requests. Recover them only when the planner supplied no
    # metric; a valid model-selected list remains authoritative, even when it
    # happens to equal the historical broad fallback list.
    explicit_heart_metric = _explicit_single_heart_metric(question)
    explicit_heart_topic_change = bool(
        explicit_heart_metric
        and is_followup
        and not candidate_had_metrics
    )
    may_narrow_fallback = bool(
        not candidate_had_metrics and not candidate_supplied_metric_tokens
    )
    if (
        explicit_heart_metric
        and may_narrow_fallback
        and (
            _asks_for_latest_sample(question)
            or _asks_for_aggregate(question)
            or explicit_heart_topic_change
        )
    ):
        metrics = [explicit_heart_metric]
        if str(candidate.get("case_type", "")).strip().lower() not in VALID_CASE_TYPES:
            plan["case_type"] = "monitoring"

    # Without an explicit date, "current/right now" should not inherit the
    # broad rolling 14-day fallback and present a days-old observation as
    # current. The most recent/last-recorded wording intentionally retains the
    # bounded lookback so it can still find the last available sample.
    q_normalized = " ".join(str(question or "").lower().split())
    asks_current_now = bool(
        re.search(r"\b(?:current|now)\b", q_normalized)
        or "right now" in q_normalized
    )
    if (
        explicit_heart_metric
        and asks_current_now
        and not has_explicit_time
    ):
        plan["time_range"] = {
            "mode": "relative",
            "value": 1,
            "unit": "day",
            "anchor": "today",
        }

    # A monitoring request may deliberately combine a stable Phone profile
    # field with time-bounded measurements (for example steps and reported
    # gender).  Keep that explicit field so the deterministic composer can
    # return every requested metric.  Analysis/prescription remains numeric
    # and time-series oriented, so profile fields are excluded there.
    if plan["case_type"] != "monitoring":
        metrics = [m for m in metrics if m not in PHONE_PROFILE_METRICS]
    if not metrics and not candidate_supplied_metric_tokens and not v2_requested:
        metrics = list(_DEFAULT_METRICS_BY_CASE.get(plan["case_type"], _DEFAULT_METRICS_BY_CASE["monitoring"]))

    # A v2 computation already contains canonical metrics.  Rewriting only the
    # compatibility inventory based on wording would desynchronise it from the
    # typed AST that the compiler executes.
    if not v2_requested:
        metrics = _apply_source_scope(metrics, question)
    plan["metrics"] = metrics
    if any(METRIC_DEFINITIONS.get(metric, {}).get("resource") == "sleep.nightly"
           for metric in metrics):
        basis = str(candidate.get("sleep_date_basis") or "night_day").lower()
        if basis not in {"night_day", "wake_day", "start_day"}:
            basis = "night_day"
        plan["sleep_date_basis"] = basis
    if unsupported_metrics:
        plan["unsupported_metrics"] = unsupported_metrics

    # Preserve whether the user wants the latest individual observation and
    # whether the response should emphasize its recorded time. These fields are
    # deliberately allowlisted instead of passing arbitrary planner output into
    # KQL or response formatting.
    latest_eligible = bool(
        plan["case_type"] == "monitoring"
        and len(metrics) == 1
        and metrics[0] in _LATEST_SAMPLE_METRICS
    )
    asks_measurement_time = _asks_for_measurement_time(question)
    asks_latest = _asks_for_latest_sample(question)
    explicit_latest_wording = _asks_for_explicit_latest_wording(question)
    asks_aggregate = _asks_for_aggregate(question)
    candidate_latest_value = candidate.get("latest_sample")
    candidate_has_latest_flag = isinstance(candidate_latest_value, bool)
    inherited_latest = bool(
        is_followup
        and isinstance(last_plan, dict)
        and _truthy_plan_flag(last_plan.get("latest_sample"))
    )
    aggregate_time_followup = bool(
        not candidate_has_latest_flag
        and is_followup
        and asks_measurement_time
        and not explicit_latest_wording
        and isinstance(last_plan, dict)
        and "latest_sample" in last_plan
        and not _truthy_plan_flag(last_plan.get("latest_sample"))
        and len(last_metrics) == 1
        and last_metrics[0] in _LATEST_SAMPLE_METRICS
    )
    effective_latest_request = bool(
        explicit_latest_wording
        or measurement_time_followup
        or (asks_measurement_time and not last_plan)
        or inherited_latest
    )
    if candidate_has_latest_flag:
        # The model interprets whether this is one observation or an aggregate.
        # Text heuristics recover absent/invalid fields, not explicit booleans.
        latest_sample = bool(latest_eligible and candidate_latest_value)
    else:
        latest_sample = bool(
            latest_eligible
            and not asks_aggregate
            and not aggregate_time_followup
            and effective_latest_request
        )
    if v2_requested:
        # Selection/aggregation semantics are owned by the typed computation;
        # the legacy latest-sample switch must not select a second code path.
        latest_sample = False

    candidate_focus = str(candidate.get("answer_focus") or "").strip().lower()
    if candidate_focus not in {"value", "measurement_time", "value_and_time"}:
        candidate_focus = ""
    inherited_focus = str(last_plan.get("answer_focus") or "").strip().lower()
    if inherited_focus not in {"value", "measurement_time", "value_and_time"}:
        inherited_focus = ""
    if latest_sample and asks_measurement_time:
        answer_focus = "measurement_time"
    elif latest_sample and asks_latest:
        answer_focus = "value_and_time"
    elif latest_sample:
        answer_focus = candidate_focus or inherited_focus or "value_and_time"
        if answer_focus == "measurement_time" and not is_followup:
            answer_focus = "value_and_time"
    else:
        answer_focus = "value"
    plan["latest_sample"] = latest_sample
    plan["answer_focus"] = answer_focus

    # Resources: metric semantics determine the approved logical resource set.
    # The LLM may order resources within that set, but it cannot introduce an
    # unrelated raw/vendor resource as a missing-data fallback. Old candidates
    # and persisted follow-up plans may still carry physical `tables_priority`;
    # convert those names at this boundary and emit the legacy field below.
    derived_resources = _select_resources(metrics) if metrics else []
    priority_raw: Any = candidate.get("resources_priority")
    if not isinstance(priority_raw, list) or not priority_raw:
        priority_raw = candidate.get("tables_priority")
    if (
        (not isinstance(priority_raw, list) or not priority_raw)
        and is_followup
        and isinstance(last_plan, dict)
    ):
        priority_raw = last_plan.get("resources_priority")
        if not isinstance(priority_raw, list) or not priority_raw:
            priority_raw = last_plan.get("tables_priority")

    requested_resources: List[str] = []
    if isinstance(priority_raw, list):
        for value in priority_raw:
            resource = _as_logical_resource(value)
            if (
                resource in derived_resources
                and resource not in requested_resources
            ):
                requested_resources.append(resource)
    resources = requested_resources + [
        resource
        for resource in derived_resources
        if resource not in requested_resources
    ]
    plan["resources_priority"] = resources
    # Deprecated compatibility field for persisted plans and telemetry. Query
    # construction should consume logical resources and bind physical names only
    # at the ADX compiler boundary.
    plan["tables_priority"] = [
        table
        for table in (physical_name(resource) for resource in resources)
        if table
    ]

    # Granularity.
    granularity = str(candidate.get("granularity", "")).strip().lower()
    if granularity not in {"daily", "intraday"}:
        granularity = _derive_granularity(metrics, plan["case_type"])
    if computation is not None:
        granularity = (
            "daily"
            if computation.get("entity") in {"day", "night"}
            else "intraday"
        )
    if plan.get("time_of_day") or latest_sample:
        granularity = "intraday"
    plan["granularity"] = granularity

    # Assumptions.
    assumptions_raw = candidate.get("assumptions", [])
    assumptions: List[str] = []
    if isinstance(assumptions_raw, list):
        assumptions.extend(str(item) for item in assumptions_raw if str(item).strip())

    provider_comparison_unavailable = bool(
        _mentions_both_sources(question)
        and not any(
            str(METRIC_DEFINITIONS.get(metric, {}).get("resource") or "")
            in _WITHINGS_RESOURCES
            for metric in metrics
        )
    )
    needs_followup = (
        bool(candidate.get("needs_followup", False))
        or bool(unsupported_metrics)
        or provider_comparison_unavailable
        or bool(v2_requested and not plan.get("execution_ready", False))
    )
    # JSON null is an absent question, not the literal response "None".
    followup_question = str(candidate.get("followup_question") or "").strip()

    if provider_comparison_unavailable:
        # Equivalent provider-specific totals are intentionally collapsed into
        # one source-resolved curated metric. Asking the model to fabricate two
        # series here would misrepresent the current ADX contract.
        followup_question = (
            "The unified data path does not expose separate Withings and phone "
            "values for an equivalent-metric comparison. Would you like the "
            "source-resolved curated value instead?"
        )

    if unsupported_metrics and not followup_question:
        unsupported_text = ", ".join(unsupported_metrics)
        followup_question = (
            f"I could not map {unsupported_text} to the current data catalog. "
            "Which supported metric should I check instead?"
        )

    if v2_requested and not plan.get("execution_ready", False):
        # Do not leak schema-validator internals or echo unsafe candidate text.
        # The structured errors remain in the plan for tests/telemetry while the
        # user receives one concise request to restate the intended calculation.
        followup_question = (
            "I could not safely represent that calculation. Please restate the "
            "target metric, any condition, and the desired total, average, or count."
        )

    if aggregate_time_followup:
        needs_followup = True
        followup_question = (
            "A period average does not have one measurement time. "
            "Do you mean the time of your latest heart-rate measurement?"
        )

    # "Most recent" can describe an entity selector, not a vague period.
    # Honor a valid model-resolved scope (and its own needs_followup flag);
    # a lexical legacy fallback must not block that semantic interpretation.
    llm_resolved_time = bool(
        llm_plan_ok and isinstance(candidate_time_range, dict)
        and not _validate_v2_time_range(candidate_time_range)
    )
    if ambiguous_recent and not has_explicit_time and not llm_resolved_time:
        assumptions.append("I interpreted 'recent' as the last 14 days by default.")
        needs_followup = True
        if not followup_question:
            followup_question = "Would you like the same analysis for 7 days, 30 days, or a custom period?"

    # Comparison: trust the LLM's judgement; the keyword check and last-plan
    # inheritance apply only when the LLM produced no plan (fallback).
    comparison_requested = bool(candidate.get("comparison_requested", False) or has_comparison_range)
    if not llm_plan_ok:
        if _is_comparison_question(question):
            comparison_requested = True
        if is_followup and last_plan and not comparison_requested:
            comparison_requested = bool(last_plan.get("comparison_requested", False))

    # Latest-point and comparison plans have different query/result contracts.
    # Prefer the explicit comparison instead of emitting a trend with a
    # latest-sample answer focus and then suppressing its analysis footer.
    if comparison_requested:
        plan["latest_sample"] = False
        plan["answer_focus"] = "value"

    # A case type alone does not establish a comparative calculation. Preserve
    # the planner's declared assumptions rather than claiming a fixed 7+7-day
    # comparison that the selected plan/query may never have performed.
    if unsupported_metrics:
        assumptions.append(
            "Unsupported requested metrics were not substituted with unrelated health data: "
            + ", ".join(unsupported_metrics)
            + "."
        )
    if provider_comparison_unavailable:
        assumptions.append(
            "Equivalent provider-specific series are not exposed by the unified curated data contract."
        )
    if any(
        resource in _CURATED_HEALTH_RESOURCES
        for resource in plan["resources_priority"]
    ):
        assumptions.append(
            "Resource priorities are semantic defaults, not proof of available data. "
            "Choose equivalent evidence through the shared Sensor functions using bounded inventory; "
            "preserve units, entity grain and source deduplication, and never invent missing values."
        )
    if len(followup_question) > 220:
        followup_question = _default_followup_question(plan["case_type"])
    # The system already manages the user's timezone/region. The planner LLM
    # sometimes asks "which timezone should I use?" as a follow-up; drop any such
    # clarification so it never reaches the user (all dates are already local).
    if _mentions_timezone(followup_question):
        followup_question = ""
        needs_followup = False
    if needs_followup and not followup_question:
        followup_question = _default_followup_question(plan["case_type"])

    plan["assumptions"] = list(dict.fromkeys(assumptions))
    plan["needs_followup"] = needs_followup
    plan["followup_question"] = followup_question
    plan["comparison_requested"] = comparison_requested
    if comparison_errors:
        plan["execution_ready"] = False
        plan["validation_errors"] = [*plan.get("validation_errors", []), *comparison_errors]
        plan["needs_followup"] = True
        plan["followup_question"] = "Please specify the two periods you want to compare."
    return plan


_TIMEZONE_FOLLOWUP_TOKENS = (
    "timezone", "time zone", "time-zone", "which timezone", "what timezone",
    "your location", "phone's local time", "local time zone", "region you",
)


def _mentions_timezone(text: str) -> bool:
    low = str(text or "").lower()
    return any(token in low for token in _TIMEZONE_FOLLOWUP_TOKENS)


def _select_resources(metrics: List[str]) -> List[str]:
    """Return metric-authorized logical resources in canonical priority order."""

    resources: List[str] = []
    for metric in metrics:
        definition = METRIC_DEFINITIONS.get(metric, {})
        resource = str(definition.get("resource") or "").strip()
        # Transitional fallback for an out-of-date catalog entry. This keeps the
        # sanitizer safe during rolling deployments without making a physical
        # name part of the canonical plan.
        if not resource:
            resource = _as_logical_resource(definition.get("table"))
        if resource and resource not in resources:
            resources.append(resource)
    if not resources:
        return list(_CURATED_HEALTH_RESOURCES)
    return sorted(
        resources,
        key=lambda item: (
            RESOURCE_PRIORITY.index(item) if item in RESOURCE_PRIORITY else 99
        ),
    )


def _select_tables(metrics: List[str]) -> List[str]:
    """Return default-profile physical names for legacy callers.

    New planning code should use :func:`_select_resources` instead. Keeping this
    facade preserves older tests, persisted plans, and telemetry consumers while
    physical binding moves to the compiler boundary.
    """

    return [
        table
        for table in (physical_name(resource) for resource in _select_resources(metrics))
        if table
    ]


def _derive_granularity(metrics: List[str], case_type: str) -> str:
    if case_type in {"factor_analysis", "prescription"}:
        return "daily"
    if metrics and all(metric in PHONE_PROFILE_METRICS for metric in metrics):
        return "daily"
    for metric in metrics:
        resource = METRIC_DEFINITIONS.get(metric, {}).get("resource")
        if resource == _HEALTH_SAMPLES_RESOURCE:
            return "intraday"
    if any(metric in {"sleep_start_local", "sleep_end_local"} for metric in metrics):
        return "intraday"
    return "daily"


__all__ = [
    "_DEFAULT_METRICS_BY_CASE",
    "_CURATED_HEALTH_RESOURCES",
    "_CURATED_HEALTH_TABLES",
    "_LATEST_SAMPLE_METRICS",
    "_TIMEZONE_FOLLOWUP_TOKENS",
    "_derive_granularity",
    "_mentions_timezone",
    "_select_resources",
    "_select_tables",
    "sanitize_plan",
]
