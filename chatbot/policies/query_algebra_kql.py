"""Compile the validated query algebra into bounded, read-only KQL.

This module is deliberately narrower than the conversational KQL builders.  It
does not inspect the user's words and it never accepts physical table names,
column names, KQL fragments, or arbitrary operators from a plan.  The planner
describes a computation over canonical metrics; this compiler owns all physical
resource bindings, entity grains, joins, unit conversions, and stable ordering.

Only ``schema_version == 2`` plans with a sanitized ``computation`` object are
accepted. Unsupported (but otherwise valid) combinations return ``None`` and
must fail closed; callers must not replace them with a legacy query that would
discard requested operations.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .semantic_resources import (
    ACTIVITY_DAILY,
    HEALTH_SAMPLES,
    SLEEP_NIGHTLY,
    WORKOUT_SESSIONS,
    physical_name,
    tabular_call,
)
from .time_ranges import (
    _kql_phone_local_time_window,
    _valid_timezone,
    validate_v2_time_range,
)


_ENTITIES = frozenset({"day", "night", "workout_session", "sleep_session"})
_AGGREGATIONS = frozenset(
    {
        "none",
        "sum",
        "mean",
        "min",
        "max",
        "median",
        "stddev_sample",
        "stddev_population",
        "count",
        "count_distinct",
    }
)
_FILTER_DIMENSIONS = frozenset(
    {"activity_name", "source_name", "source_platform"}
)
_FILTER_OPERATORS = frozenset({"eq", "ne", "in"})
_CONDITION_OPERATORS = frozenset({"eq", "ne", "lt", "lte", "gt", "gte", "between"})
_SELECTOR_MODES = frozenset({"first", "last", "top", "bottom"})


@dataclass(frozen=True, slots=True)
class _MetricSpec:
    """Trusted physical semantics for one canonical metric."""

    family: str
    column: str
    unit: str
    activity_types: Tuple[str, ...] = ()
    # Reduction used when a session/sample stream has to become one value per
    # calendar day before a cross-resource join.  This is deliberately metric
    # semantics, not the final aggregation requested by the user.  For
    # example, daily workout steps are additive while daily workout average HR
    # must remain an average; summing it would corrupt later mean/min/max work.
    daily_reducer: str = "sum"


# Nightly values are emitted by the canonical sleep contract in minutes/percent. Historical
# ``phone_*`` spellings remain here only as typed compatibility aliases.
_SLEEP_METRICS: Mapping[str, _MetricSpec] = {
    "total_sleep_time": _MetricSpec("sleep", "AsleepMin", "minute"),
    "sleep_duration": _MetricSpec("sleep", "AsleepMin", "minute"),
    "sleep_minutes": _MetricSpec("sleep", "AsleepMin", "minute"),
    "phone_sleep_duration": _MetricSpec("sleep", "AsleepMin", "minute"),
    "deep_sleep": _MetricSpec("sleep", "DeepMin", "minute"),
    "deep_sleep_minutes": _MetricSpec("sleep", "DeepMin", "minute"),
    "phone_deep_sleep": _MetricSpec("sleep", "DeepMin", "minute"),
    "rem_sleep": _MetricSpec("sleep", "RemMin", "minute"),
    "rem_sleep_minutes": _MetricSpec("sleep", "RemMin", "minute"),
    "phone_rem_sleep": _MetricSpec("sleep", "RemMin", "minute"),
    "light_sleep": _MetricSpec("sleep", "LightMin", "minute"),
    "light_sleep_minutes": _MetricSpec("sleep", "LightMin", "minute"),
    "phone_light_sleep": _MetricSpec("sleep", "LightMin", "minute"),
    "awake_duration": _MetricSpec("sleep", "AwakeMin", "minute"),
    "awake_minutes": _MetricSpec("sleep", "AwakeMin", "minute"),
    "time_in_bed": _MetricSpec("sleep", "TimeInBedMin", "minute"),
    "time_in_bed_minutes": _MetricSpec("sleep", "TimeInBedMin", "minute"),
    "sleep_efficiency": _MetricSpec("sleep", "SleepEfficiencyPct", "percent"),
    "phone_sleep_efficiency": _MetricSpec("sleep", "SleepEfficiencyPct", "percent"),
    "deep_sleep_percent": _MetricSpec("sleep", "DeepPct", "percent"),
    "rem_sleep_percent": _MetricSpec("sleep", "RemPct", "percent"),
    "light_sleep_percent": _MetricSpec("sleep", "LightPct", "percent"),
    "awake_percent": _MetricSpec("sleep", "AwakePct", "percent"),
}

_ACTIVITY_METRICS: Mapping[str, _MetricSpec] = {
    "steps": _MetricSpec("activity", "", "count", ("STEPS",)),
    "phone_steps": _MetricSpec("activity", "", "count", ("STEPS",)),
    "distance": _MetricSpec(
        "activity", "", "meter", ("DISTANCE_WALKING_RUNNING", "DISTANCE_DELTA")
    ),
    "phone_distance": _MetricSpec(
        "activity", "", "meter", ("DISTANCE_WALKING_RUNNING", "DISTANCE_DELTA")
    ),
    "calories": _MetricSpec("activity", "", "kilocalorie", ("ACTIVE_ENERGY_BURNED",)),
    "phone_active_calories": _MetricSpec(
        "activity", "", "kilocalorie", ("ACTIVE_ENERGY_BURNED",)
    ),
    "total_calories": _MetricSpec(
        "activity", "", "kilocalorie", ("TOTAL_CALORIES_BURNED",)
    ),
    "phone_total_calories": _MetricSpec(
        "activity", "", "kilocalorie", ("TOTAL_CALORIES_BURNED",)
    ),
    "active": _MetricSpec(
        "activity", "", "minute", ("EXERCISE_TIME", "APPLE_MOVE_TIME")
    ),
    "active_minutes": _MetricSpec(
        "activity", "", "minute", ("EXERCISE_TIME", "APPLE_MOVE_TIME")
    ),
    "elevation": _MetricSpec("activity", "", "meter", ("ELEVATION_GAINED",)),
    "elevation_gain": _MetricSpec("activity", "", "meter", ("ELEVATION_GAINED",)),
    "active_zone_minutes": _MetricSpec(
        "activity", "", "minute", ("ACTIVE_ZONE_MINUTES",)
    ),
    "fatburn_active_zone_minutes": _MetricSpec(
        "activity", "", "minute", ("FATBURN_ACTIVE_ZONE_MINUTES",)
    ),
    "cardio_active_zone_minutes": _MetricSpec(
        "activity", "", "minute", ("CARDIO_ACTIVE_ZONE_MINUTES",)
    ),
    "peak_active_zone_minutes": _MetricSpec(
        "activity", "", "minute", ("PEAK_ACTIVE_ZONE_MINUTES",)
    ),
}

_HEALTH_METRICS: Mapping[str, _MetricSpec] = {
    "heart_rate": _MetricSpec(
        "health", "", "bpm", ("HEART_RATE",), daily_reducer="avg"
    ),
    "phone_heart_rate": _MetricSpec(
        "health", "", "bpm", ("HEART_RATE",), daily_reducer="avg"
    ),
    "heart_rate_max": _MetricSpec(
        "health", "", "bpm", ("HEART_RATE",), daily_reducer="max"
    ),
    "heart_rate_min": _MetricSpec(
        "health", "", "bpm", ("HEART_RATE",), daily_reducer="min"
    ),
    "phone_resting_heart_rate": _MetricSpec(
        "health", "", "bpm", ("RESTING_HEART_RATE",), daily_reducer="avg"
    ),
    "rmssd": _MetricSpec(
        "health",
        "",
        "millisecond",
        ("HEART_RATE_VARIABILITY_RMSSD",),
        daily_reducer="avg",
    ),
    "phone_hrv_rmssd": _MetricSpec(
        "health",
        "",
        "millisecond",
        ("HEART_RATE_VARIABILITY_RMSSD",),
        daily_reducer="avg",
    ),
    "sdnn1": _MetricSpec(
        "health",
        "",
        "millisecond",
        ("HEART_RATE_VARIABILITY_SDNN",),
        daily_reducer="avg",
    ),
    "phone_hrv_sdnn": _MetricSpec(
        "health",
        "",
        "millisecond",
        ("HEART_RATE_VARIABILITY_SDNN",),
        daily_reducer="avg",
    ),
    "respiration_rate": _MetricSpec(
        "health",
        "",
        "count_per_minute",
        ("RESPIRATORY_RATE",),
        daily_reducer="avg",
    ),
}

_WORKOUT_METRICS: Mapping[str, _MetricSpec] = {
    "workout_duration": _MetricSpec("workout", "WorkoutDurationMin", "minute"),
    "phone_workout_duration": _MetricSpec("workout", "WorkoutDurationMin", "minute"),
    "workout_steps": _MetricSpec("workout", "WorkoutSteps", "count"),
    "workout_distance": _MetricSpec("workout", "WorkoutDistance", "meter"),
    "workout_calories": _MetricSpec("workout", "WorkoutCalories", "kilocalorie"),
    "workout_count": _MetricSpec("workout", "WorkoutOccurrence", "count"),
    "phone_workout_count": _MetricSpec("workout", "WorkoutOccurrence", "count"),
    "workout_average_heart_rate": _MetricSpec(
        "workout", "WorkoutAverageHeartRate", "bpm", daily_reducer="avg"
    ),
    "workout_elevation_gain": _MetricSpec(
        "workout", "WorkoutElevationGain", "meter"
    ),
    "workout_active_zone_minutes": _MetricSpec(
        "workout", "WorkoutActiveZoneMinutes", "minute"
    ),
    "workout_speed": _MetricSpec(
        "workout", "WorkoutSpeed", "meter_per_minute", daily_reducer="avg"
    ),
    "speed": _MetricSpec(
        "workout", "WorkoutSpeed", "meter_per_minute", daily_reducer="avg"
    ),
}

# A workout-session question about generic exercise metrics refers to fields on
# that session, not to the whole-day activity total.  The mapping is typed and
# compiler-owned; it is not inferred from the raw question.
_WORKOUT_ENTITY_OVERRIDES: Mapping[str, _MetricSpec] = {
    "steps": _WORKOUT_METRICS["workout_steps"],
    "phone_steps": _WORKOUT_METRICS["workout_steps"],
    "distance": _WORKOUT_METRICS["workout_distance"],
    "phone_distance": _WORKOUT_METRICS["workout_distance"],
    "calories": _WORKOUT_METRICS["workout_calories"],
    "phone_active_calories": _WORKOUT_METRICS["workout_calories"],
    "active": _WORKOUT_METRICS["workout_duration"],
    "active_minutes": _WORKOUT_METRICS["workout_duration"],
    "heart_rate": _WORKOUT_METRICS["workout_average_heart_rate"],
    "phone_heart_rate": _WORKOUT_METRICS["workout_average_heart_rate"],
    "elevation": _WORKOUT_METRICS["workout_elevation_gain"],
    "elevation_gain": _WORKOUT_METRICS["workout_elevation_gain"],
    "active_zone_minutes": _WORKOUT_METRICS["workout_active_zone_minutes"],
    "speed": _WORKOUT_METRICS["workout_speed"],
}

# Exact Health 13.x units accepted from the canonical daily activity contract.
# That contract deliberately retains ``Unit`` in its grain, so values must be converted to a
# canonical metric unit before rows from different devices are combined.  A
# unit absent from this compiler-owned table is incompatible evidence and is
# excluded instead of being silently labelled as metre/minute/kilocalorie/count.
_ACTIVITY_UNIT_EXPRESSIONS: Mapping[str, Mapping[str, str]] = {
    "count": {
        "COUNT": "ActivityValue",
    },
    "meter": {
        "METER": "ActivityValue",
        "CENTIMETER": "ActivityValue / 100.0",
        "INCH": "ActivityValue * 0.0254",
        "FOOT": "ActivityValue * 0.3048",
        "YARD": "ActivityValue * 0.9144",
        "MILE": "ActivityValue * 1609.344",
    },
    "minute": {
        "SECOND": "ActivityValue / 60.0",
        "MILLISECOND": "ActivityValue / 60000.0",
        "MINUTE": "ActivityValue",
        "HOUR": "ActivityValue * 60.0",
        "DAY": "ActivityValue * 1440.0",
    },
    "kilocalorie": {
        "JOULE": "ActivityValue / 4184.0",
        "KILOCALORIE": "ActivityValue",
        "LARGE_CALORIE": "ActivityValue",
        "SMALL_CALORIE": "ActivityValue / 1000.0",
    },
}

_HEALTH_UNIT_EXPRESSIONS: Mapping[str, Mapping[str, str]] = {
    "bpm": {
        "BEATS_PER_MINUTE": "HealthValue",
        "BPM": "HealthValue",
        "COUNT_PER_MINUTE": "HealthValue",
    },
    "millisecond": {
        "MILLISECOND": "HealthValue",
        "SECOND": "HealthValue * 1000.0",
    },
    "count_per_minute": {
        "COUNT_PER_MINUTE": "HealthValue",
        "BREATHS_PER_MINUTE": "HealthValue",
    },
}

_ALL_METRICS: Mapping[str, _MetricSpec] = {
    **_SLEEP_METRICS,
    **_ACTIVITY_METRICS,
    **_HEALTH_METRICS,
    **_WORKOUT_METRICS,
}

# Canonical planner labels mapped onto values emitted by health 13.x.  This is a
# deliberately small semantic bridge, not a free-form utterance parser.
_ACTIVITY_VALUE_ALIASES: Mapping[str, Tuple[str, ...]] = {
    "TREADMILL": ("TREADMILL", "RUNNING_TREADMILL", "WALKING_TREADMILL"),
    "RUN": ("RUN", "RUNNING", "OUTDOOR_RUNNING"),
    "INTERVAL_WORKOUT": (
        "INTERVAL_WORKOUT",
        "HIGH_INTENSITY_INTERVAL_TRAINING",
        "HIIT",
    ),
    # A misspelling of elliptical appears in historical workout exports.  Keep
    # both ordinary spellings equivalent at the semantic boundary so a natural
    # user phrase does not become a dataset-specific planner rule.
    "ELLIPTICAL": ("ELLIPTICAL", "ELIPTICAL"),
    "ELIPTICAL": ("ELLIPTICAL", "ELIPTICAL"),
}

_UNIT_ALIASES: Mapping[str, str] = {
    "native": "native",
    "%": "percent",
    "percent": "percent",
    "percentage": "percent",
    "minute": "minute",
    "minutes": "minute",
    "min": "minute",
    "hour": "hour",
    "hours": "hour",
    "hr": "hour",
    "second": "second",
    "seconds": "second",
    "sec": "second",
    "millisecond": "millisecond",
    "milliseconds": "millisecond",
    "ms": "millisecond",
    "day": "day",
    "days": "day",
    "count": "count",
    "step": "count",
    "steps": "count",
    "bpm": "bpm",
    "beats_per_minute": "bpm",
    "count_per_minute": "count_per_minute",
    "breaths_per_minute": "count_per_minute",
    "meter": "meter",
    "meters": "meter",
    "m": "meter",
    "kilometer": "kilometer",
    "kilometers": "kilometer",
    "km": "kilometer",
    "mile": "mile",
    "miles": "mile",
    "meter_per_second": "meter_per_second",
    "m/s": "meter_per_second",
    "meter_per_minute": "meter_per_minute",
    "m/min": "meter_per_minute",
    "kilometer_per_hour": "kilometer_per_hour",
    "km/h": "kilometer_per_hour",
    "kph": "kilometer_per_hour",
    "mile_per_hour": "mile_per_hour",
    "mph": "mile_per_hour",
    "kilocalorie": "kilocalorie",
    "kilocalories": "kilocalorie",
    "kcal": "kilocalorie",
    "calorie": "calorie",
    "calories": "calorie",
}


def _literal(value: str) -> str:
    """Return one escaped KQL string literal."""

    return json.dumps(str(value), ensure_ascii=False)


def _string_list(values: Iterable[str]) -> str:
    return ", ".join(_literal(value) for value in values)


def _canonical_activity(value: Any) -> Optional[str]:
    text = str(value or "").strip().upper()
    text = re.sub(r"[^A-Z0-9]+", "_", text).strip("_")
    if not text or len(text) > 64:
        return None
    return text


def _activity_values(value: Any) -> Optional[Tuple[str, ...]]:
    canonical = _canonical_activity(value)
    if canonical is None:
        return None
    return _ACTIVITY_VALUE_ALIASES.get(canonical, (canonical,))


def _safe_dimension_value(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > 128 or any(ord(char) < 32 for char in text):
        return None
    return text


def _normalize_filters(value: Any, entity: str) -> Optional[List[Dict[str, Any]]]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 8:
        return None
    normalized: List[Dict[str, Any]] = []
    for raw in value:
        if not isinstance(raw, dict):
            return None
        dimension = str(raw.get("dimension") or raw.get("field") or "").strip().lower()
        operator = str(raw.get("operator") or raw.get("op") or "").strip().lower()
        if dimension not in _FILTER_DIMENSIONS or operator not in _FILTER_OPERATORS:
            return None
        raw_value = raw.get("value")
        if operator == "in":
            if not isinstance(raw_value, list) or not 1 <= len(raw_value) <= 20:
                return None
            values: List[str] = []
            for item in raw_value:
                if dimension == "activity_name":
                    canonical = _canonical_activity(item)
                    if canonical is None:
                        return None
                    values.append(canonical)
                else:
                    safe = _safe_dimension_value(item)
                    if safe is None:
                        return None
                    values.append(safe)
            result: Dict[str, Any] = {
                "dimension": dimension,
                "operator": operator,
                "value": tuple(values),
            }
            applies_to = str(raw.get("applies_to") or "").strip().lower()
            if applies_to:
                if _metric_spec(applies_to, entity) is None:
                    return None
                result["applies_to"] = applies_to
            normalized.append(result)
            continue
        if dimension == "activity_name":
            single = _canonical_activity(raw_value)
        else:
            single = _safe_dimension_value(raw_value)
        if single is None:
            return None
        result = {"dimension": dimension, "operator": operator, "value": single}
        applies_to = str(raw.get("applies_to") or "").strip().lower()
        if applies_to:
            if _metric_spec(applies_to, entity) is None:
                return None
            result["applies_to"] = applies_to
        normalized.append(result)
    return normalized


def _metric_spec(metric: str, entity: str) -> Optional[_MetricSpec]:
    if entity == "workout_session" and metric in _WORKOUT_ENTITY_OVERRIDES:
        return _WORKOUT_ENTITY_OVERRIDES[metric]
    return _ALL_METRICS.get(metric)


def _finite_number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or abs(number) > 1e15:
        return None
    return number


def _convert_value(value: float, source_unit: str, target_unit: str) -> Optional[float]:
    source = _UNIT_ALIASES.get(str(source_unit or "").strip().lower())
    if not source:
        return None
    if source == "native":
        return value
    if source == target_unit:
        return value
    conversions = {
        ("hour", "minute"): 60.0,
        ("minute", "hour"): 1.0 / 60.0,
        ("second", "minute"): 1.0 / 60.0,
        ("millisecond", "minute"): 1.0 / 60000.0,
        ("day", "minute"): 1440.0,
        ("kilometer", "meter"): 1000.0,
        ("mile", "meter"): 1609.344,
        ("meter", "kilometer"): 0.001,
        ("meter_per_second", "meter_per_minute"): 60.0,
        ("kilometer_per_hour", "meter_per_minute"): 1000.0 / 60.0,
        ("mile_per_hour", "meter_per_minute"): 1609.344 / 60.0,
        ("calorie", "kilocalorie"): 0.001,
        ("kilocalorie", "calorie"): 1000.0,
    }
    factor = conversions.get((source, target_unit))
    return None if factor is None else value * factor


def _number_literal(value: float) -> str:
    # The value has already passed ``isfinite``.  Seventeen significant digits
    # preserve a Python double without allowing exponent text from the planner.
    rendered = format(float(value), ".17g")
    return "0" if rendered in {"-0", "-0.0"} else rendered


def _normalize_conditions(
    value: Any,
    entity: str,
) -> Optional[List[Dict[str, Any]]]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 8:
        return None
    normalized: List[Dict[str, Any]] = []
    for raw in value:
        if not isinstance(raw, dict):
            return None
        metric = str(raw.get("metric") or "").strip().lower()
        operator = str(raw.get("operator") or raw.get("op") or "").strip().lower()
        spec = _metric_spec(metric, entity)
        if spec is None or operator not in _CONDITION_OPERATORS:
            return None
        unit = str(raw.get("unit") or spec.unit).strip().lower()
        if operator == "between":
            values = raw.get("values", raw.get("value"))
            if not isinstance(values, (list, tuple)) or len(values) != 2:
                return None
            low = _finite_number(values[0])
            high = _finite_number(values[1])
            if low is None or high is None:
                return None
            low = _convert_value(low, unit, spec.unit)
            high = _convert_value(high, unit, spec.unit)
            if low is None or high is None:
                return None
            if high < low:
                low, high = high, low
            normalized.append(
                {
                    "metric": metric,
                    "operator": operator,
                    "values": (low, high),
                    "unit": spec.unit,
                }
            )
            continue
        number = _finite_number(raw.get("value"))
        if number is None:
            return None
        number = _convert_value(number, unit, spec.unit)
        if number is None:
            return None
        normalized.append(
            {
                "metric": metric,
                "operator": operator,
                "value": number,
                "unit": spec.unit,
            }
        )
    return normalized


def _normalize_selector(value: Any, entity: str) -> Optional[Dict[str, Any]]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        return None
    mode = str(value.get("mode") or "").strip().lower()
    if mode not in _SELECTOR_MODES:
        return None
    try:
        n = int(value.get("n"))
    except (TypeError, ValueError):
        return None
    if isinstance(value.get("n"), bool) or not 1 <= n <= 100:
        return None
    result: Dict[str, Any] = {"mode": mode, "n": n}
    by_metric = str(value.get("by_metric") or "").strip().lower()
    if mode in {"top", "bottom"}:
        if not by_metric or _metric_spec(by_metric, entity) is None:
            return None
        result["by_metric"] = by_metric
    elif by_metric:
        return None
    return result


def _normalize_outputs(value: Any, entity: str) -> Optional[List[Dict[str, str]]]:
    if not isinstance(value, list) or not 1 <= len(value) <= 8:
        return None
    outputs: List[Dict[str, str]] = []
    for raw in value:
        if not isinstance(raw, dict):
            return None
        aggregation = str(raw.get("aggregation") or "").strip().lower()
        if aggregation not in _AGGREGATIONS:
            return None
        metric = str(raw.get("metric") or "").strip().lower()
        output_entity = str(raw.get("entity") or "").strip().lower()
        if metric:
            if output_entity or _metric_spec(metric, entity) is None:
                return None
            outputs.append({"metric": metric, "aggregation": aggregation})
            continue
        if output_entity != entity or aggregation not in {"count", "count_distinct"}:
            return None
        outputs.append({"entity": output_entity, "aggregation": aggregation})
    return outputs


def _filter_values(filter_item: Mapping[str, Any]) -> Optional[Tuple[str, ...]]:
    value = filter_item["value"]
    raw_values: Sequence[Any] = value if isinstance(value, tuple) else (value,)
    if filter_item["dimension"] == "activity_name":
        expanded: List[str] = []
        for item in raw_values:
            values = _activity_values(item)
            if values is None:
                return None
            for candidate in values:
                if candidate not in expanded:
                    expanded.append(candidate)
        return tuple(expanded)
    return tuple(str(item) for item in raw_values)


def _dimension_predicates(
    filters: Sequence[Mapping[str, Any]],
    *,
    include_activity: bool,
    metric: Optional[str] = None,
) -> Optional[List[str]]:
    columns = {
        "source_name": "SourceName",
        "source_platform": "SourcePlatform",
        "activity_name": "ActivityCode",
    }
    predicates: List[str] = []
    for item in filters:
        applies_to = str(item.get("applies_to") or "")
        if metric is None and applies_to:
            # Direct source branches carry only global predicates. Scoped
            # predicates are added to the corresponding typed metric relation.
            continue
        if metric is not None and applies_to and applies_to != metric:
            continue
        dimension = str(item["dimension"])
        if dimension == "activity_name" and not include_activity:
            continue
        values = _filter_values(item)
        if values is None or not values:
            return None
        column = columns[dimension]
        operator = str(item["operator"])
        if dimension == "activity_name":
            expression = column
            literals = _string_list(values)
        else:
            expression = f"tolower(tostring({column}))"
            literals = _string_list(value.casefold() for value in values)
        if operator in {"eq", "in"}:
            predicates.append(f"{expression} in ({literals})")
        elif operator == "ne":
            predicates.append(f"{expression} !in ({literals})")
        else:  # Defensive; normalization should make this unreachable.
            return None
    return predicates


def _timezone(plan: Mapping[str, Any], time_range: Mapping[str, Any]) -> str:
    for value in (
        plan.get("timezone_iana"),
        plan.get("timezone"),
        time_range.get("timezone_iana"),
        time_range.get("timezone"),
    ):
        if value:
            return _valid_timezone(str(value))
    return "Asia/Seoul"


def _source_base_statements(
    user_id: str,
    start: str,
    end: str,
    filters: Sequence[Mapping[str, Any]],
    needed_families: Iterable[str],
) -> Optional[Tuple[List[str], List[str]]]:
    """Build each canonical Sensor-contract branch exactly once."""

    families = set(needed_families)
    statements: List[str] = []
    source_tables: List[str] = []
    source_predicates = _dimension_predicates(filters, include_activity=False)
    workout_predicates = _dimension_predicates(filters, include_activity=True)
    if source_predicates is None or workout_predicates is None:
        return None

    if "sleep" in families:
        table = physical_name(SLEEP_NIGHTLY)
        pipeline = [
            f"let SleepRows={tabular_call(SLEEP_NIGHTLY, 'TargetUserId', 'StartLocal', 'EndLocal')}",
            "| where UserId == TargetUserId",
            "| where SleepDay >= StartLocal and SleepDay < EndLocal",
        ]
        pipeline.extend(f"| where {predicate}" for predicate in source_predicates)
        statements.append("\n".join(pipeline))
        source_tables.append(table)

    if "activity" in families:
        table = physical_name(ACTIVITY_DAILY)
        pipeline = [
            f"let ActivityRows={tabular_call(ACTIVITY_DAILY, 'TargetUserId', 'StartLocal', 'EndLocal')}",
            "| where UserId == TargetUserId",
            "| where LocalDate >= StartLocal and LocalDate < EndLocal",
        ]
        pipeline.extend(f"| where {predicate}" for predicate in source_predicates)
        statements.append("\n".join(pipeline))
        source_tables.append(table)

    if "health" in families:
        table = physical_name(HEALTH_SAMPLES)
        pipeline = [
            f"let HealthRows={tabular_call(HEALTH_SAMPLES, 'TargetUserId', 'StartLocal', 'EndLocal')}",
            "| where UserId == TargetUserId",
            "| where DateFrom >= StartLocal and DateFrom < EndLocal",
            "| extend HealthEventKey=iff(isnotempty(tostring(Uuid)), "
            'strcat("uuid:", tostring(Uuid)), '
            'strcat("fallback:", tostring(Type), "|", tostring(DateFrom), "|", '
            'tostring(DateTo), "|", tostring(ValueNumber), "|", tostring(Unit), "|", '
            'tostring(SourcePlatform), "|", tostring(SourceName)))',
            "| summarize arg_max(IngestionTime, *) by HealthEventKey",
        ]
        pipeline.extend(f"| where {predicate}" for predicate in source_predicates)
        statements.append("\n".join(pipeline))
        source_tables.append(table)

    if "workout" in families:
        table = physical_name(WORKOUT_SESSIONS)
        pipeline = [
            f"let WorkoutRowsRaw={tabular_call(WORKOUT_SESSIONS, 'TargetUserId', 'StartLocal', 'EndLocal')}",
            "| project UserId, WorkoutId, WorkoutStartLocal=StartLocal, "
            "WorkoutEndLocal=EndLocal, ActivityName, ActivityCode, DurationMin, "
            "AverageHeartRateBpm, ElevationGain, Distance, DistanceUnit, "
            "CaloriesKcal, Steps, ActiveZoneMinutes, Speed, SpeedUnit, "
            "SourcePlatform, SourceName, IngestionTime",
            "| where UserId == TargetUserId",
            "| where WorkoutStartLocal >= StartLocal and WorkoutStartLocal < EndLocal",
            "| where isnotnull(WorkoutStartLocal) and isnotnull(WorkoutEndLocal) "
            "and WorkoutEndLocal > WorkoutStartLocal",
            '| extend ActivityCode=iff(isnotempty(ActivityCode), '
            'toupper(trim("_", ActivityCode)), '
            'toupper(trim("_", replace_regex(ActivityName, @"[^A-Za-z0-9]+", "_"))))',
            "| extend EntityKey=iff(isnotempty(tostring(WorkoutId)), "
            'strcat("workout:", tostring(WorkoutId)), '
            'strcat("fallback:", tostring(SourcePlatform), "|", tostring(SourceName), "|", '
            'tostring(WorkoutStartLocal), "|", tostring(WorkoutEndLocal), "|", ActivityCode))',
            "| summarize arg_max(IngestionTime, *) by EntityKey",
            "| extend DateFrom=WorkoutStartLocal, DateTo=WorkoutEndLocal, "
            "WorkoutDurationMin=todouble(DurationMin), "
            "WorkoutSteps=todouble(Steps), "
            'WorkoutDistance=case(toupper(tostring(DistanceUnit)) in ("METER", "METRE"), todouble(Distance), '
            'toupper(tostring(DistanceUnit)) in ("KILOMETER", "KILOMETRE"), todouble(Distance) * 1000.0, '
            'toupper(tostring(DistanceUnit)) == "MILE", todouble(Distance) * 1609.344, real(null)), '
            "WorkoutCalories=todouble(CaloriesKcal), "
            "WorkoutAverageHeartRate=todouble(AverageHeartRateBpm), "
            "WorkoutElevationGain=todouble(ElevationGain), "
            "WorkoutActiveZoneMinutes=todouble(ActiveZoneMinutes), "
            'WorkoutSpeed=case(toupper(tostring(SpeedUnit)) in ("METER_PER_MINUTE", "METRE_PER_MINUTE"), todouble(Speed), '
            'toupper(tostring(SpeedUnit)) in ("METER_PER_SECOND", "METRE_PER_SECOND"), todouble(Speed) * 60.0, '
            'toupper(tostring(SpeedUnit)) in ("KILOMETER_PER_HOUR", "KILOMETRE_PER_HOUR", "KPH"), todouble(Speed) * 1000.0 / 60.0, '
            'toupper(tostring(SpeedUnit)) in ("MILE_PER_HOUR", "MPH"), todouble(Speed) * 1609.344 / 60.0, real(null)), '
            "WorkoutOccurrence=1.0",
        ]
        # The adapter owns payload parsing and released-duration semantics. The
        # compiler deliberately consumes only normalized contract columns.
        pipeline.extend(f"| where {predicate}" for predicate in workout_predicates)
        statements.append("\n".join(pipeline))
        source_tables.append(table)

    return statements, source_tables


def _daily_metric_relation(
    alias: str,
    column_alias: str,
    spec: _MetricSpec,
    metric: str,
    filters: Sequence[Mapping[str, Any]],
) -> Optional[str]:
    predicates = _dimension_predicates(
        filters,
        include_activity=spec.family == "workout",
        metric=metric,
    )
    if predicates is None:
        return None
    where = "".join(f"\n| where {predicate}" for predicate in predicates)
    if spec.family == "sleep":
        return (
            f"let {alias}=SleepRows\n"
            "| where IsMainSleep\n"
            f"{where}\n"
            f"| summarize {column_alias}=avg(todouble({spec.column})) by Period=SleepDay"
        )
    if spec.family == "activity":
        types = _string_list(spec.activity_types)
        unit_expressions = _ACTIVITY_UNIT_EXPRESSIONS.get(spec.unit)
        if not unit_expressions:
            return None
        accepted_units = _string_list(unit_expressions)
        normalized_value = "case(" + ", ".join(
            part
            for unit_name, expression in unit_expressions.items()
            for part in (f'ActivityUnit == {_literal(unit_name)}', expression)
        ) + ", real(null))"
        return (
            f"let {alias}=ActivityRows\n"
            f"| where Type in ({types})\n"
            f"{where}\n"
            '| extend ActivityUnit=toupper(trim(@"\\s+", tostring(Unit))), '
            "ActivityValue=todouble(ValueSumExact)\n"
            f"| where ActivityUnit in ({accepted_units}) and isnotnull(ActivityValue)\n"
            f"| extend CanonicalActivityValue={normalized_value}\n"
            "| where isnotnull(CanonicalActivityValue)\n"
            f"| summarize {column_alias}=sum(CanonicalActivityValue) by Period=LocalDate"
        )
    if spec.family == "health":
        types = _string_list(spec.activity_types)
        unit_expressions = _HEALTH_UNIT_EXPRESSIONS.get(spec.unit)
        if not unit_expressions:
            return None
        accepted_units = _string_list(unit_expressions)
        normalized_value = "case(" + ", ".join(
            part
            for unit_name, expression in unit_expressions.items()
            for part in (f"HealthUnit == {_literal(unit_name)}", expression)
        ) + ", real(null))"
        if spec.daily_reducer not in {"avg", "min", "max"}:
            return None
        return (
            f"let {alias}=HealthRows\n"
            f"| where Type in ({types})\n"
            f"{where}\n"
            '| extend HealthUnit=toupper(trim(@"\\s+", tostring(Unit))), '
            "HealthValue=todouble(ValueNumber)\n"
            f"| where HealthUnit in ({accepted_units}) and isnotnull(HealthValue)\n"
            f"| extend CanonicalHealthValue={normalized_value}\n"
            "| where isnotnull(CanonicalHealthValue)\n"
            f"| summarize {column_alias}={spec.daily_reducer}(CanonicalHealthValue) "
            "by Period=startofday(DateFrom)"
        )
    if spec.family == "workout":
        if spec.daily_reducer not in {"sum", "avg", "min", "max"}:
            return None
        return (
            f"let {alias}=WorkoutRowsRaw\n"
            f"{where}\n"
            f"| where isnotnull({spec.column})\n"
            f"| summarize {column_alias}={spec.daily_reducer}(todouble({spec.column})) "
            "by Period=startofday(DateFrom)"
        )
    return None


def _required_metrics(
    outputs: Sequence[Mapping[str, str]],
    conditions: Sequence[Mapping[str, Any]],
    selector: Mapping[str, Any],
) -> List[str]:
    metrics: List[str] = []
    for output in outputs:
        metric = output.get("metric")
        if metric and metric not in metrics:
            metrics.append(metric)
    for condition in conditions:
        metric = str(condition["metric"])
        if metric not in metrics:
            metrics.append(metric)
    by_metric = str(selector.get("by_metric") or "")
    if by_metric and by_metric not in metrics:
        metrics.append(by_metric)
    return metrics


def _condition_expression(alias: str, condition: Mapping[str, Any]) -> str:
    operator = str(condition["operator"])
    if operator == "between":
        low, high = condition["values"]
        predicate = f"{alias} >= {_number_literal(low)} and {alias} <= {_number_literal(high)}"
    else:
        symbol = {
            "eq": "==",
            "ne": "!=",
            "lt": "<",
            "lte": "<=",
            "gt": ">",
            "gte": ">=",
        }[operator]
        predicate = f"{alias} {symbol} {_number_literal(condition['value'])}"
    return f"isnotnull({alias}) and {predicate}"


def _condition_pack(
    condition: Mapping[str, Any],
    alias: str,
) -> str:
    fields = [
        _literal("metric"),
        _literal(str(condition["metric"])),
        _literal("operator"),
        _literal(str(condition["operator"])),
        _literal("observed"),
        f"todouble({alias})",
        _literal("unit"),
        _literal(str(condition["unit"])),
    ]
    if condition["operator"] == "between":
        low, high = condition["values"]
        fields.extend(
            [
                _literal("lower"),
                _number_literal(low),
                _literal("upper"),
                _number_literal(high),
            ]
        )
    else:
        fields.extend([_literal("threshold"), _number_literal(condition["value"])])
    return "pack(" + ", ".join(fields) + ")"


def _join_metric_relations(
    statements: List[str],
    base_alias: str,
    metric_aliases: Mapping[str, str],
    relation_aliases: Mapping[str, str],
    metrics_to_join: Sequence[str],
) -> str:
    current = base_alias
    for index, metric in enumerate(metrics_to_join):
        next_alias = f"EntityWide{index}"
        statements.append(
            f"let {next_alias}={current}\n"
            f"| join kind=leftouter ({relation_aliases[metric]}) on Period\n"
            "| project-away Period1"
        )
        current = next_alias
    return current


def _selector_pipeline(
    selector: Mapping[str, Any],
    metric_aliases: Mapping[str, str],
    *,
    has_none_output: bool,
) -> Optional[List[str]]:
    if not selector:
        if has_none_output:
            return ["| sort by EntityTime asc, EntityKey asc", "| take 500"]
        return []
    mode = str(selector["mode"])
    n = int(selector["n"])
    if mode == "first":
        ordering = "EntityTime asc, EntityKey asc"
    elif mode == "last":
        ordering = "EntityTime desc, EntityKey asc"
    else:
        by_metric = str(selector.get("by_metric") or "")
        alias = metric_aliases.get(by_metric)
        if alias is None:
            return None
        direction = "desc" if mode == "top" else "asc"
        ordering = f"{alias} {direction} nulls last, EntityTime asc, EntityKey asc"
        # A top/bottom selector must choose an observed metric value. Without
        # this guard an all-null day can win the final slot when fewer than N
        # observed entities exist, weakening the selector evidence.
        return [f"| where isnotnull({alias})", f"| sort by {ordering}", f"| take {n}"]
    return [f"| sort by {ordering}", f"| take {n}"]


def _aggregate_expression(aggregation: str, target: str, entity_output: bool) -> str:
    if aggregation == "sum":
        return f"sum({target})"
    if aggregation == "mean":
        return f"avg({target})"
    if aggregation == "min":
        return f"min({target})"
    if aggregation == "max":
        return f"max({target})"
    if aggregation == "median":
        # Median needs a short post-summarize pipeline to preserve pandas-style
        # even-cardinality semantics.  It is handled by
        # ``_build_output_statement`` instead of this scalar aggregate helper.
        raise ValueError("Median requires exact ordered-value aggregation.")
    if aggregation == "stddev_sample":
        return f"stdev({target})"
    if aggregation == "stddev_population":
        return f"stdevp({target})"
    if aggregation == "count":
        return "count()" if entity_output else f"countif(isnotnull({target}))"
    if aggregation == "count_distinct":
        return f"count_distinct({'EntityKey' if entity_output else target})"
    raise ValueError(f"Unsupported aggregation: {aggregation}")


def _build_output_statement(
    index: int,
    output: Mapping[str, str],
    entity: str,
    metric_aliases: Mapping[str, str],
    metric_specs: Mapping[str, _MetricSpec],
    filter_count: int,
    conditions: Sequence[Mapping[str, Any]],
    selector: Mapping[str, Any],
    source_label: str,
) -> str:
    metric = output.get("metric") or output.get("entity") or entity
    aggregation = str(output["aggregation"])
    entity_output = "entity" in output
    target = "1.0" if entity_output else f"todouble({metric_aliases[str(metric)]})"
    unit = "count" if entity_output or aggregation in {"count", "count_distinct"} else metric_specs[str(metric)].unit
    selector_mode = str(selector.get("mode") or "none")
    selector_n = int(selector.get("n") or 0)
    selector_by = str(selector.get("by_metric") or "")
    condition_packs = [
        _condition_pack(condition, metric_aliases[str(condition["metric"])])
        for condition in conditions
    ]
    conditions_value = "pack_array(" + ", ".join(condition_packs) + ")" if condition_packs else "dynamic([])"
    selection_fields = [
        _literal("period"),
        "tostring(Period)",
        _literal("entity_time"),
        "tostring(EntityTime)",
        _literal("entity_key"),
        "tostring(EntityKey)",
        _literal("target_value"),
        f"todouble({target})",
    ]
    if entity == "workout_session":
        selection_fields.extend(
            [
                _literal("activity_name"),
                "tostring(ActivityName)",
                _literal("activity_code"),
                "tostring(ActivityCode)",
            ]
        )
    if selector_by:
        selection_fields.extend(
            [
                _literal("selector_metric"),
                _literal(selector_by),
                _literal("selector_value"),
                f"todouble({metric_aliases[selector_by]})",
                _literal("selector_unit"),
                _literal(metric_specs[selector_by].unit),
            ]
        )
    selection_pack = "pack(" + ", ".join(selection_fields) + ")"

    if aggregation == "none":
        return (
            f"let Output{index}=SelectedEntities\n"
            f"| extend Metric={_literal(str(metric))}, Aggregation={_literal(aggregation)}, "
            f"Value=todouble({target}), Unit={_literal(unit)}, EvidenceRows=tolong(iif(isnotnull({target}), 1, 0)), "
            f"ConditionEvidence={conditions_value}, SelectionEvidence={selection_pack}\n"
            f"| extend BaseCoverageRows=tolong(BaseCoverageRowsScalar), MatchedRows=tolong(MatchedRowsScalar), "
            f"SelectedRows=tolong(SelectedRowsScalar), SourceTable={_literal(source_label)}, "
            f"SelectorMode={_literal(selector_mode)}, SelectorN=tolong({selector_n}), "
            f"SelectorByMetric={_literal(selector_by)}, FilterCount=tolong({filter_count}), "
            f"ConditionCount=tolong({len(conditions)}), "
            f"ResultStatus=iif(isnotnull({target}), \"ok\", \"no_metric_evidence\")\n"
            "| project Metric, Aggregation, Value, Unit, Period, EntityTime, EntityKey, "
            "BaseCoverageRows, MatchedRows, SelectedRows, EvidenceRows, SourceTable, "
            "SelectorMode, SelectorN, SelectorByMetric, FilterCount, ConditionCount, ResultStatus, ConditionEvidence, SelectionEvidence"
        )

    exact_median = aggregation == "median"
    value_expression = (
        ""
        if exact_median
        else _aggregate_expression(aggregation, "TargetValue", entity_output)
    )
    needs_metric_evidence = bool(
        not entity_output and aggregation not in {"count", "count_distinct"}
    )
    value_projection = (
        "iff(EvidenceRows > 0, todouble(RawValue), real(null))"
        if needs_metric_evidence
        else "todouble(RawValue)"
    )
    status_projection = (
        'iff(EvidenceRows > 0, "ok", "no_metric_evidence")'
        if needs_metric_evidence
        else '"ok"'
    )
    aggregate_value = (
        "TargetValues=make_list(TargetValue, 1048576), "
        if exact_median
        else f"RawValue=todouble({value_expression}), "
    )
    exact_median_pipeline = (
        "| extend SortedTargetValues=array_sort_asc(TargetValues), "
        "MedianUpperIndex=toint(floor(todouble(EvidenceRows) / 2.0))\n"
        "| extend RawValue=case("
        "EvidenceRows == 0, real(null), "
        "EvidenceRows % 2 == 1, todouble(SortedTargetValues[MedianUpperIndex]), "
        "(todouble(SortedTargetValues[MedianUpperIndex - 1]) + "
        "todouble(SortedTargetValues[MedianUpperIndex])) / 2.0)\n"
        if exact_median
        else ""
    )
    return (
        f"let Output{index}=SelectedEntities\n"
        f"| extend TargetValue=todouble({target}), RowConditionEvidence={conditions_value}, "
        f"RowSelectionEvidence={selection_pack}\n"
        f"| summarize {aggregate_value}"
        "EvidenceRows=countif(isnotnull(TargetValue)), "
        "SelectedFirstPeriod=min(Period), SelectedLastPeriod=max(Period), "
        "ConditionEvidence=make_list(RowConditionEvidence, 100), "
        "SelectionEvidence=make_list(RowSelectionEvidence, 100)\n"
        f"{exact_median_pipeline}"
        f"| extend Value={value_projection}, ResultStatus={status_projection}, "
        f"Metric={_literal(str(metric))}, Aggregation={_literal(aggregation)}, Unit={_literal(unit)}, "
        "BaseCoverageRows=tolong(BaseCoverageRowsScalar), MatchedRows=tolong(MatchedRowsScalar), "
        f"SelectedRows=tolong(SelectedRowsScalar), SourceTable={_literal(source_label)}, "
        f"SelectorMode={_literal(selector_mode)}, SelectorN=tolong({selector_n}), "
        f"SelectorByMetric={_literal(selector_by)}, FilterCount=tolong({filter_count}), "
        f"ConditionCount=tolong({len(conditions)})\n"
        "| project Metric, Aggregation, Value, Unit, SelectedFirstPeriod, SelectedLastPeriod, "
        "BaseCoverageRows, MatchedRows, SelectedRows, EvidenceRows, SourceTable, "
        "SelectorMode, SelectorN, SelectorByMetric, FilterCount, ConditionCount, ResultStatus, ConditionEvidence, SelectionEvidence"
    )


def validate_computation_support(computation: Any) -> List[Dict[str, str]]:
    """Return stable errors for typed computations this compiler cannot run.

    This is a capability check, not the public-plan sanitizer.  It lets the
    caller reject a semantically unsupported v2 plan before attempting query
    generation or falling through to a legacy builder with changed meaning.
    """

    errors: List[Dict[str, str]] = []

    def add(code: str, path: str, message: str) -> None:
        errors.append({"code": code, "path": path, "message": message})

    if not isinstance(computation, dict):
        add("invalid_computation", "computation", "Computation must be an object.")
        return errors
    entity = str(computation.get("entity") or "").strip().lower()
    if entity not in _ENTITIES:
        add(
            "unsupported_entity",
            "computation.entity",
            "This compiler supports day, night, workout_session, and sleep_session.",
        )
        return errors

    outputs = _normalize_outputs(computation.get("outputs"), entity)
    if outputs is None:
        add(
            "unsupported_output",
            "computation.outputs",
            "An output metric/entity or aggregation is not supported by this compiler.",
        )
    filters = _normalize_filters(computation.get("filters", []), entity)
    if filters is None:
        add(
            "unsupported_filter",
            "computation.filters",
            "A filter dimension, operator, value, or metric scope is unsupported.",
        )
    conditions = _normalize_conditions(computation.get("conditions", []), entity)
    if conditions is None:
        add(
            "unsupported_condition",
            "computation.conditions",
            "A condition metric, operator, value, or unit is unsupported.",
        )
    selector = _normalize_selector(computation.get("selector"), entity)
    if selector is None:
        add(
            "unsupported_selector",
            "computation.selector",
            "The selector cannot be represented safely by this compiler.",
        )
    if computation.get("group_by") not in (None, []):
        add(
            "unsupported_grouping",
            "computation.group_by",
            "Grouped outputs are not yet part of this compiler's result contract.",
        )

    if filters is not None and outputs is not None:
        scoped = {str(item.get("applies_to")) for item in filters if item.get("applies_to")}
        output_metrics = {str(item["metric"]) for item in outputs if "metric" in item}
        undeclared = scoped - output_metrics
        if undeclared:
            add(
                "unsupported_filter_scope",
                "computation.filters",
                "A scoped filter must apply to the metric output it restricts.",
            )
        if len(scoped) > 1 or (scoped and len(output_metrics) > 1):
            add(
                "ambiguous_filter_scope",
                "computation.filters",
                "Scoped filters currently require one metric output.",
            )
        aggregations = {str(item["aggregation"]) for item in outputs}
        if "none" in aggregations and len(aggregations) > 1 and not computation.get("selector"):
            add(
                "ambiguous_mixed_outputs",
                "computation.outputs",
                "Mixed row and aggregate outputs require one explicit shared selector.",
            )

    if outputs is not None and conditions is not None and selector is not None:
        dependencies = _required_metrics(outputs, conditions, selector)
        has_activity_filter = bool(
            filters
            and any(item["dimension"] == "activity_name" for item in filters)
        )
        if entity == "day" and not dependencies and not has_activity_filter:
            add(
                "missing_entity_source",
                "computation",
                "Counting days requires a metric, condition, selector metric, or activity filter.",
            )
    return errors


def compile_computation_query(user_id: str, plan: Mapping[str, Any]) -> Optional[str]:
    """Compile a sanitized schema-v2 computation into deterministic KQL.

    The function is intentionally fail-closed.  It returns ``None`` for an
    unsupported entity/metric combination, malformed typed value, incompatible
    unit, or grouping request that this compiler cannot preserve faithfully.
    """

    if not isinstance(plan, Mapping) or plan.get("schema_version") != 2:
        return None
    if not isinstance(user_id, str) or not user_id or len(user_id) > 256:
        return None
    computation = plan.get("computation")
    if not isinstance(computation, dict):
        return None
    if validate_computation_support(computation):
        return None
    entity = str(computation.get("entity") or "").strip().lower()
    if entity not in _ENTITIES:
        return None
    # Grouped aggregates require a separate result contract.  Never silently
    # drop a validated grouping request and return a scalar with changed meaning.
    group_by = computation.get("group_by")
    if group_by not in (None, []):
        return None

    outputs = _normalize_outputs(computation.get("outputs"), entity)
    filters = _normalize_filters(computation.get("filters", []), entity)
    conditions = _normalize_conditions(computation.get("conditions", []), entity)
    selector = _normalize_selector(computation.get("selector"), entity)
    if outputs is None or filters is None or conditions is None or selector is None:
        return None

    required_metrics = _required_metrics(outputs, conditions, selector)
    metric_specs: Dict[str, _MetricSpec] = {}
    for metric in required_metrics:
        spec = _metric_spec(metric, entity)
        if spec is None:
            return None
        metric_specs[metric] = spec

    time_range = plan.get("time_range")
    if not isinstance(time_range, dict):
        return None
    if validate_v2_time_range(time_range):
        return None
    try:
        start, end = _kql_phone_local_time_window(
            time_range, _timezone(plan, time_range)
        )
    except (TypeError, ValueError, OverflowError):
        return None

    has_activity_filter = any(item["dimension"] == "activity_name" for item in filters)
    needed_families = {spec.family for spec in metric_specs.values()}
    if entity in {"night", "sleep_session"}:
        needed_families.add("sleep")
    if entity == "workout_session" or has_activity_filter:
        needed_families.add("workout")

    base_result = _source_base_statements(
        user_id, start, end, filters, needed_families
    )
    if base_result is None:
        return None
    base_statements, source_tables = base_result
    statements: List[str] = [
        f"let TargetUserId={_literal(user_id)}",
        f"let StartLocal={start}",
        f"let EndLocal={end}",
        *base_statements,
    ]

    metric_aliases = {
        metric: f"MetricValue{index}" for index, metric in enumerate(required_metrics)
    }
    relation_aliases: Dict[str, str] = {}
    scoped_filter_metrics = [
        str(item["applies_to"]) for item in filters if item.get("applies_to")
    ]
    scoped_filter_metric = scoped_filter_metrics[0] if scoped_filter_metrics else ""

    # Session entities already carry their own-family metric columns.  Other
    # metrics are joined only after they have been reduced to one row per day.
    if entity == "workout_session":
        direct = [
            metric
            for metric, spec in metric_specs.items()
            if spec.family == "workout"
        ]
        projections = [
            f"{metric_aliases[metric]}=todouble({metric_specs[metric].column})"
            for metric in direct
        ]
        direct_filter_lines = _dimension_predicates(
            filters,
            include_activity=True,
            metric=scoped_filter_metric or None,
        )
        if direct_filter_lines is None:
            return None
        direct_where = "".join(
            f"\n| where {predicate}" for predicate in direct_filter_lines
        )
        statements.append(
            "let EntityBase=WorkoutRowsRaw"
            + direct_where
            + "\n"
            "| extend Period=startofday(DateFrom), EntityTime=DateFrom\n"
            "| project Period, EntityTime, EntityKey, ActivityName, ActivityCode, SourceName, SourcePlatform"
            + (", " + ", ".join(projections) if projections else "")
        )
        join_metrics = [metric for metric in required_metrics if metric not in direct]
    elif entity in {"night", "sleep_session"}:
        direct = [
            metric
            for metric, spec in metric_specs.items()
            if spec.family == "sleep"
        ]
        projections = [
            f"{metric_aliases[metric]}=todouble({metric_specs[metric].column})"
            for metric in direct
        ]
        main_filter = "\n| where IsMainSleep" if entity == "night" else ""
        scoped_sleep_filters = _dimension_predicates(
            filters,
            include_activity=False,
            metric=scoped_filter_metric or None,
        )
        if scoped_sleep_filters is None:
            return None
        main_filter += "".join(
            f"\n| where {predicate}" for predicate in scoped_sleep_filters
        )
        statements.append(
            "let EntityBase=SleepRows"
            + main_filter
            + "\n| extend Period=SleepDay, EntityTime=OnsetLocal, "
            'EntityKey=strcat(tostring(SleepDay), "|", tostring(OnsetLocal), "|", tostring(WakeLocal), "|", tostring(SourceName))\n'
            "| project Period, EntityTime, EntityKey, SourceName, SourcePlatform"
            + (", " + ", ".join(projections) if projections else "")
        )
        join_metrics = [metric for metric in required_metrics if metric not in direct]
    else:  # day
        direct = []
        join_metrics = list(required_metrics)

    for index, metric in enumerate(join_metrics):
        alias = f"MetricRelation{index}"
        relation = _daily_metric_relation(
            alias,
            metric_aliases[metric],
            metric_specs[metric],
            metric,
            filters,
        )
        if relation is None:
            return None
        statements.append(relation)
        relation_aliases[metric] = alias

    if entity == "day":
        period_sources = [
            f"({relation_aliases[metric]} | project Period)" for metric in join_metrics
        ]
        if has_activity_filter:
            eligibility_predicates = _dimension_predicates(
                filters,
                include_activity=True,
                metric=scoped_filter_metric or None,
            )
            if eligibility_predicates is None:
                return None
            eligibility_where = "".join(
                f"\n| where {predicate}" for predicate in eligibility_predicates
            )
            statements.append(
                "let ActivityEligibleDays=WorkoutRowsRaw"
                + eligibility_where
                + "\n"
                "| summarize by Period=startofday(DateFrom)"
            )
            period_sources.append("(ActivityEligibleDays | project Period)")
        if not period_sources:
            return None
        statements.append(
            "let EntityBase=union " + ", ".join(period_sources) + "\n"
            "| distinct Period\n"
            "| extend EntityTime=Period, EntityKey=tostring(Period)"
        )

    current = _join_metric_relations(
        statements, "EntityBase", metric_aliases, relation_aliases, join_metrics
    )

    # For a non-workout entity, activity-name filtering means "days/nights on
    # which at least one matching workout occurred".  Join only the distinct
    # eligible days, avoiding a one-to-many multiplication of target values.
    if has_activity_filter and entity != "workout_session":
        if entity != "day":
            eligibility_predicates = _dimension_predicates(
                filters,
                include_activity=True,
                metric=scoped_filter_metric or None,
            )
            if eligibility_predicates is None:
                return None
            eligibility_where = "".join(
                f"\n| where {predicate}" for predicate in eligibility_predicates
            )
            statements.append(
                "let ActivityEligibleDays=WorkoutRowsRaw"
                + eligibility_where
                + "\n"
                "| summarize by Period=startofday(DateFrom)"
            )
        filtered_alias = "ActivityFilteredEntities"
        statements.append(
            f"let {filtered_alias}={current}\n"
            "| join kind=inner (ActivityEligibleDays) on Period\n"
            "| project-away Period1"
        )
        current = filtered_alias

    # Preserve entities supported only by condition/selector evidence. Filtering
    # on output availability here would let a different date win a top/bottom
    # selector when the true selected date lacks the requested output (for
    # example, longest-sleep day with no step row). Output aggregations ignore
    # null targets later and expose EvidenceRows versus SelectedRows instead.
    statements.append(f"let BaseEntities={current}")
    statements.append(
        "let BaseCoverageRowsScalar=toscalar(BaseEntities | summarize count())"
    )

    matched_pipeline = ["let MatchedEntities=BaseEntities"]
    for condition in conditions:
        alias = metric_aliases[str(condition["metric"])]
        matched_pipeline.append(f"| where {_condition_expression(alias, condition)}")
    statements.append("\n".join(matched_pipeline))
    statements.append(
        "let MatchedRowsScalar=toscalar(MatchedEntities | summarize count())"
    )

    selector_pipeline = _selector_pipeline(
        selector,
        metric_aliases,
        has_none_output=any(output["aggregation"] == "none" for output in outputs),
    )
    if selector_pipeline is None:
        return None
    statements.append(
        "\n".join(["let SelectedEntities=MatchedEntities", *selector_pipeline])
    )
    statements.append(
        "let SelectedRowsScalar=toscalar(SelectedEntities | summarize count())"
    )

    # Preserve dependency order (activity, nightly sleep, workout samples) in a
    # compact evidence label while deduplicating repeated physical sources.
    unique_sources = list(dict.fromkeys(source_tables))
    source_label = "+".join(unique_sources)
    for index, output in enumerate(outputs):
        statements.append(
            _build_output_statement(
                index,
                output,
                entity,
                metric_aliases,
                metric_specs,
                len(filters),
                conditions,
                selector,
                source_label,
            )
        )

    final = "union " + ", ".join(f"Output{index}" for index in range(len(outputs)))
    final += "\n| order by Metric asc, Aggregation asc"
    return ";\n".join(statements + [final]).strip()


__all__ = ["compile_computation_query", "validate_computation_support"]
