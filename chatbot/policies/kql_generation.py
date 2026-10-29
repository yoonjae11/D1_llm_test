"""Deterministic KQL builders over the shared Sensor function contract.

Core D1 queries resolve logical resources to canonical tabular functions. The
backing production or evaluation storage layout is intentionally invisible to
this module. Optional direct-provider resources remain a legacy opt-in path.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .domain_catalog import (
    METRIC_DEFINITIONS,
    PHONE_PROFILE_METRICS,
    PHONE_SLEEP_NIGHTS_METRICS,
    WITHINGS_PHONE_SLEEP_FALLBACK_METRICS,
    WITHINGS_TO_PHONE_METRIC_PRIORITY,
)
from .semantic_resources import (
    ACTIVITY_DAILY,
    ACTIVITY_HOURLY,
    ACTIVITY_INTERVAL,
    CONVERSATION_HISTORY,
    HEALTH_SAMPLES,
    SENSEI_ADX_V1,
    SLEEP_NIGHTLY,
    WITHINGS_ACTIVITY,
    WITHINGS_INTRADAY,
    WITHINGS_NIGHT_EVENTS,
    WITHINGS_RESOURCE_IDS,
    WITHINGS_SLEEP,
    WITHINGS_SLEEP_DETAIL,
    WITHINGS_WORKOUTS,
    WORKOUT_SESSIONS,
    physical_name,
    tabular_call,
)
from .time_ranges import (
    _default_time_range,
    _kql_phone_local_time_window,
)
from .metric_normalization import _normalize_metrics
from .query_algebra_kql import compile_computation_query


# Deprecated physical-name aliases retained for callers that imported them.
# Runtime query construction resolves the active profile at the KQL boundary.
PHONE_CUMULATIVE_DAILY_MV = SENSEI_ADX_V1.physical_name(ACTIVITY_DAILY)
PHONE_CUMULATIVE_HOURLY_MV = SENSEI_ADX_V1.physical_name(ACTIVITY_HOURLY)
PHONE_CUMULATIVE_INTERVAL_MV = SENSEI_ADX_V1.physical_name(ACTIVITY_INTERVAL)
HEALTH_DATA_FLAT = SENSEI_ADX_V1.physical_name(HEALTH_SAMPLES)
TELEGRAM_CONVERSATION_LOG = SENSEI_ADX_V1.physical_name(CONVERSATION_HISTORY)
# ADX stored function that returns one row per sleep session (main sleep + naps)
# with dedup, midnight-spanning sessionization, multi-source resolution and stage
# aggregation done server-side over the PhoneSleepSessionDedupMV /
# PhoneSleepStageDedupMV materialized views. See to_do_sleep_kql_guide.md.
PHONE_SLEEP_NIGHTS_FN = SENSEI_ADX_V1.physical_name(SLEEP_NIGHTLY)

# Compatibility view of the default vendor tables. Optional-source routing
# below uses logical IDs, so a temporary evaluation profile never leaks these
# production names into generated KQL.
WITHINGS_TABLES = frozenset(
    SENSEI_ADX_V1.physical_name(resource_id)
    for resource_id in WITHINGS_RESOURCE_IDS
)

# For single-night sleep questions ("how did I sleep last night?") the current
# wake-day often has no sleep row yet (asked in the early morning before waking /
# syncing), so a strict today-only window wrongly reports "no records". Widen the
# lookback this many days and return the MOST RECENT night before the window end.
PHONE_SLEEP_LATEST_LOOKBACK_DAYS = 4


PHONE_CUMULATIVE_METRIC_TYPES: Dict[str, Tuple[str, ...]] = {
    "steps": ("STEPS",),
    "distance": ("DISTANCE_WALKING_RUNNING", "DISTANCE_DELTA"),
    "calories": ("ACTIVE_ENERGY_BURNED",),
    "total_calories": ("TOTAL_CALORIES_BURNED",),
    "active": ("EXERCISE_TIME", "APPLE_MOVE_TIME"),
    "elevation": ("ELEVATION_GAINED",),
    "active_zone_minutes": ("ACTIVE_ZONE_MINUTES",),
    "fatburn_active_zone_minutes": ("FATBURN_ACTIVE_ZONE_MINUTES",),
    "cardio_active_zone_minutes": ("CARDIO_ACTIVE_ZONE_MINUTES",),
    "peak_active_zone_minutes": ("PEAK_ACTIVE_ZONE_MINUTES",),
    "steps_intraday": ("STEPS",),
    "distance_intraday": ("DISTANCE_WALKING_RUNNING", "DISTANCE_DELTA"),
    "calories_intraday": ("ACTIVE_ENERGY_BURNED",),
    "phone_steps": ("STEPS",),
    "phone_distance": ("DISTANCE_WALKING_RUNNING", "DISTANCE_DELTA"),
    "phone_active_calories": ("ACTIVE_ENERGY_BURNED",),
    "phone_total_calories": ("TOTAL_CALORIES_BURNED",),
    "phone_workout_duration": ("EXERCISE_TIME", "APPLE_MOVE_TIME"),
}


def _bounded_function_call(
    logical_resource: str,
    user_id: str,
    start: str,
    end: str,
) -> str:
    """Render a canonical call from compiler-owned, already bounded values."""

    # Values are produced by this module's quote/time-window helpers, never by
    # model output. The stricter identifier-only renderer is used whenever a
    # query already declares the standard scalar identifiers.
    return (
        f"{physical_name(logical_resource)}("
        f"{_quote(user_id)}, ({start}), ({end}))"
    )


PHONE_SAMPLED_METRIC_TYPES: Dict[str, Tuple[str, ...]] = {
    "heart_rate": ("HEART_RATE",),
    "heart_rate_max": ("HEART_RATE",),
    "heart_rate_min": ("HEART_RATE",),
    "rmssd": ("HEART_RATE_VARIABILITY_RMSSD",),
    "sdnn1": ("HEART_RATE_VARIABILITY_SDNN",),
    "respiration_rate": ("RESPIRATORY_RATE",),
    "phone_heart_rate": ("HEART_RATE",),
    "phone_resting_heart_rate": ("RESTING_HEART_RATE",),
    "phone_hrv_rmssd": ("HEART_RATE_VARIABILITY_RMSSD",),
    "phone_hrv_sdnn": ("HEART_RATE_VARIABILITY_SDNN",),
    "phone_weight": ("WEIGHT",),
    "phone_bmi": ("BODY_MASS_INDEX",),
    "phone_blood_pressure_systolic": ("BLOOD_PRESSURE_SYSTOLIC",),
    "phone_blood_pressure_diastolic": ("BLOOD_PRESSURE_DIASTOLIC",),
    "phone_blood_glucose": ("BLOOD_GLUCOSE",),
    "phone_body_temperature": ("BODY_TEMPERATURE",),
}


# Legacy schema-v1 plans can still carry simple workout metrics.  Keep their
# aliases and aggregate semantics explicit while sourcing every value from the
# canonical workout-session contract.
WORKOUT_METRIC_AGGREGATES: Dict[str, Tuple[str, str]] = {
    "workout_count": ("workout_count", "count()"),
    "phone_workout_count": ("phone_workout_count", "count()"),
    "workout_duration": ("workout_duration_min", "sum(DurationMin)"),
    "phone_workout_duration": ("phone_workout_duration_min", "sum(DurationMin)"),
    "workout_steps": ("workout_steps", "sum(Steps)"),
    "workout_calories": ("workout_calories", "sum(CaloriesKcal)"),
    "workout_distance": ("workout_distance", "sum(Distance)"),
    "workout_average_heart_rate": (
        "workout_average_heart_rate",
        "avg(AverageHeartRateBpm)",
    ),
    "workout_elevation_gain": ("workout_elevation_gain", "sum(ElevationGain)"),
    "workout_active_zone_minutes": (
        "workout_active_zone_minutes",
        "sum(ActiveZoneMinutes)",
    ),
    "workout_speed": ("workout_speed", "avg(Speed)"),
    "speed": ("speed", "avg(Speed)"),
}


PHONE_SLEEP_TYPES = (
    "SLEEP_ASLEEP",
    "SLEEP_LIGHT",
    "SLEEP_DEEP",
    "SLEEP_REM",
    "SLEEP_SESSION",
)

# Sub-stages that count as actual asleep time (no double counting with SESSION).
PHONE_SLEEP_STAGE_TYPES = (
    "SLEEP_ASLEEP",
    "SLEEP_LIGHT",
    "SLEEP_DEEP",
    "SLEEP_REM",
)

# Records used to draw the sleep timeline for sessionization. SLEEP_AWAKE is kept
# ONLY so brief mid-night awakenings do not split one night into two sessions; it
# is excluded from the asleep total. The SLEEP_SESSION umbrella is intentionally
# excluded from the timeline because its long span would swallow the gaps between
# the main night and daytime naps.
PHONE_SLEEP_TIMELINE_TYPES = PHONE_SLEEP_STAGE_TYPES + ("SLEEP_AWAKE",)

# A gap longer than this between consecutive sleep records starts a new session,
# separating the overnight block from daytime/evening naps.
PHONE_SLEEP_GAP_MINUTES = 120


PHONE_PROFILE_TYPES = {
    "phone_birth_date": "BIRTH_DATE",
    "phone_gender": "GENDER",
    "phone_blood_type": "BLOOD_TYPE",
}

PHONE_SLEEP_DATETIME_METRICS = frozenset(
    {
        "sleep_start_local",
        "sleep_end_local",
        "phone_sleep_start_local",
        "phone_sleep_end_local",
    }
)


def _quote(value: str) -> str:
    return "'" + str(value or "").replace("'", "''") + "'"


def _double_quote(value: str) -> str:
    """Return a KQL double-quoted string literal.

    Physical names are validated as simple identifiers by ``DataProfile``;
    escaping here also keeps this helper safe for composite diagnostic labels.
    """

    return '"' + str(value or "").replace("\\", "\\\\").replace('"', '\\"') + '"'


def _metric_resource(metric: str) -> str:
    """Resolve a catalog metric to its logical resource contract.

    ``resource`` is the canonical field. The legacy ``table`` fallback keeps
    this compiler import-compatible while older catalogs are being migrated.
    It is deliberately resolved through the immutable production profile, not
    the active profile, because the legacy value is a production physical name.
    """

    definition = METRIC_DEFINITIONS.get(metric, {})
    resource_id = str(definition.get("resource") or "").strip()
    if resource_id:
        return resource_id
    legacy_name = str(definition.get("table") or "").strip()
    if not legacy_name:
        return ""
    try:
        return SENSEI_ADX_V1.logical_name(legacy_name)
    except KeyError:
        return ""


def _logical_resource(value: str) -> str:
    """Accept a logical ID or a deprecated default physical identifier."""

    candidate = str(value or "").strip()
    if candidate in SENSEI_ADX_V1.bindings:
        return candidate
    try:
        return SENSEI_ADX_V1.logical_name(candidate)
    except KeyError:
        return ""


def _string_list(values: Iterable[str]) -> str:
    unique: List[str] = []
    for value in values:
        item = str(value or "").strip()
        if item and item not in unique:
            unique.append(item)
    return ", ".join(_quote(item) for item in unique) or "''"


def _timezone(plan: Dict[str, Any]) -> str:
    time_range = plan.get("time_range", {}) if isinstance(plan.get("time_range"), dict) else {}
    for value in (
        plan.get("timezone_iana"),
        plan.get("timezone"),
        time_range.get("timezone_iana") if isinstance(time_range, dict) else "",
        time_range.get("timezone") if isinstance(time_range, dict) else "",
    ):
        if value:
            return str(value)
    return os.environ.get("CHATBOT_DEFAULT_TIMEZONE", "Asia/Seoul")


def _kql_phone_window_for_plan(plan: Dict[str, Any]) -> Tuple[str, str]:
    timezone = _timezone(plan)
    time_range = plan.get("time_range", _default_time_range("monitoring"))
    if not isinstance(time_range, dict):
        time_range = _default_time_range("monitoring")
    start, end = _kql_phone_local_time_window(time_range, timezone)
    time_of_day = plan.get("time_of_day") if isinstance(plan.get("time_of_day"), dict) else None
    if not time_of_day:
        return start, end
    try:
        start_hour = int(time_of_day.get("start_hour"))
        end_hour = int(time_of_day.get("end_hour"))
        start_minute = int(time_of_day.get("start_minute", 0))
        end_minute = int(time_of_day.get("end_minute", 0))
    except Exception:
        return start, end
    start_total = start_hour * 60 + start_minute
    end_total = end_hour * 60 + end_minute
    if not (
        0 <= start_hour < 24
        and 0 <= end_hour <= 24
        and 0 <= start_minute < 60
        and 0 <= end_minute < 60
        and (end_hour < 24 or end_minute == 0)
        and 0 <= start_total < end_total <= 24 * 60
    ):
        return start, end

    def offset(hour: int, minute: int) -> str:
        parts = []
        if hour:
            parts.append(f"{hour}h")
        if minute:
            parts.append(f"{minute}m")
        return " + ".join(parts) or "0m"

    # Phone MV/Flat timestamps are local wall-clock. Keep this as local clock
    # arithmetic and do not convert it to UTC.
    return (
        f"{start} + {offset(start_hour, start_minute)}",
        f"{start} + {offset(end_hour, end_minute)}",
    )


def metric_result_alias(expr: str) -> str:
    """Extract the projected result-column alias from a KQL assignment."""

    text = str(expr or "").strip()
    if "=" not in text:
        return ""
    return text.split("=", 1)[0].strip()


def _metric_types(metrics: List[str], source: Dict[str, Tuple[str, ...]]) -> List[str]:
    types: List[str] = []
    for metric in metrics:
        for type_name in source.get(metric, ()):
            if type_name not in types:
                types.append(type_name)
    return types


def _is_cumulative_query(metrics: List[str]) -> bool:
    return bool(_metric_types(metrics, PHONE_CUMULATIVE_METRIC_TYPES))


def _is_sampled_query(metrics: List[str]) -> bool:
    return bool(_metric_types(metrics, PHONE_SAMPLED_METRIC_TYPES))


def _is_workout_query(metrics: List[str]) -> bool:
    return bool(metrics) and all(
        _metric_resource(metric) == WORKOUT_SESSIONS for metric in metrics
    )


def _mentions_interval_level(question: str, plan: Dict[str, Any]) -> bool:
    q = (question or "").lower()
    granularity = str(plan.get("granularity", "")).lower()
    return granularity in {"interval", "raw"} or any(
        token in q
        for token in [
            "interval",
            "raw interval",
            "source row",
            "dedup",
            "overlap",
            "gap diagnostic",
            "freshness",
            "latest ingestion",
        ]
    )


def _mentions_hourly(question: str, plan: Dict[str, Any]) -> bool:
    q = (question or "").lower()
    granularity = str(plan.get("granularity", "")).lower()
    time_range = plan.get("time_range", {}) if isinstance(plan.get("time_range"), dict) else {}
    if isinstance(plan.get("time_of_day"), dict):
        return True
    return granularity in {"hourly", "intraday"} or any(
        token in q
        for token in [
            "hour",
            "hourly",
            "morning",
            "afternoon",
            "evening",
            "between",
            "from ",
            " to ",
        ]
    ) or str(time_range.get("unit", "")).lower() == "hour"


def _phone_source_policy_predicate() -> str:
    """Mirror the canonical activity-source policy deployed by KQL file 3.

    This predicate is used only by interval/freshness diagnostics. Aggregate
    answers come from the materialized views, so raw mirrored vendor rows are
    never blindly added together as a fallback.
    """
    return (
        "| where (SourcePlatform == \"googleHealthConnect\" and ((Type == \"STEPS\" and (SourceName == \"android\" or SourceName startswith \"com.android.healthconnect.phone.\")) "
        "or (Type != \"STEPS\" and (SourceName == \"com.sec.android.app.shealth\" or SourceName startswith \"com.android.healthconnect.phone.\")))) "
        "or (SourcePlatform == \"appleHealth\" and SourceId startswith \"com.apple.health.\" "
        "and ((Type in (\"STEPS\",\"DISTANCE_DELTA\",\"DISTANCE_WALKING_RUNNING\",\"FLIGHTS_CLIMBED\",\"ELEVATION_GAINED\") and SourceName !contains \"Watch\") "
        "or Type in (\"ACTIVE_ENERGY_BURNED\",\"BASAL_ENERGY_BURNED\",\"TOTAL_CALORIES_BURNED\",\"EXERCISE_TIME\",\"APPLE_MOVE_TIME\",\"APPLE_STAND_TIME\"))))"
    )


def _phone_flat_logical_dedup_base(user_id: str, start: str, end: str, type_names: List[str]) -> str:
    health_samples = physical_name(HEALTH_SAMPLES)
    return (
        f"{_bounded_function_call(HEALTH_SAMPLES, user_id, start, end)}"
        f" | where UserId == {_quote(user_id)}"
        f" | where Type in ({_string_list(type_names)})"
        f" {_phone_source_policy_predicate()}"
        " | where DateTo > DateFrom and DateTo - DateFrom < 20h"
        f" | where ((DateFrom >= {start} and DateFrom < {end}) or (DateTo > {start} and DateTo <= {end}) or (DateFrom < {start} and DateTo > {end}))"
        " | extend StableRecordId=iff(isempty(Uuid),strcat(\"fallback:\",SourceDeviceId,\"|\",SourceId,\"|\",tostring(DateFrom),\"|\",tostring(DateTo)),strcat(\"uuid:\",Uuid))"
        " | extend LogicalEventKey=strcat(UserId,\"|\",SourcePlatform,\"|\",SourceName,\"|\",Type,\"|\",Unit,\"|\",StableRecordId,\"|\",tostring(DateFrom),\"|\",tostring(DateTo))"
        " | summarize arg_max(IngestionTime, *) by LogicalEventKey"
    )


def _phone_sampled_base(user_id: str, start: str, end: str, type_names: List[str]) -> str:
    return (
        f"{_bounded_function_call(HEALTH_SAMPLES, user_id, start, end)}"
        f" | where UserId == {_quote(user_id)}"
        f" | where Type in ({_string_list(type_names)})"
        f" | where ((isnotnull(DateFrom) and DateFrom >= {start} and DateFrom < {end}) or (isnotnull(DateTo) and DateTo >= {start} and DateTo < {end}))"
        " | summarize arg_max(IngestionTime, *) by Uuid, Type, DateFrom, DateTo, ValueNumber, Unit, SourcePlatform, SourceName"
    )


def _profile_query(user_id: str, metrics: List[str]) -> str:
    type_names = [PHONE_PROFILE_TYPES[m] for m in metrics if m in PHONE_PROFILE_TYPES]
    if not type_names:
        type_names = list(PHONE_PROFILE_TYPES.values())
    profile_start = "ago(3650d)"
    profile_end = "now()"
    return (
        f"{_bounded_function_call(HEALTH_SAMPLES, user_id, profile_start, profile_end)}"
        f" | where UserId == {_quote(user_id)}"
        f" | where Type in ({_string_list(type_names)})"
        " | where IngestionTime >= ago(3650d) and IngestionTime < now()"
        " | summarize arg_max(IngestionTime, *) by Type"
        " | summarize "
        "phone_birth_date = anyif(Value, Type == \"BIRTH_DATE\"), "
        "phone_gender = anyif(Value, Type == \"GENDER\"), "
        "phone_blood_type = anyif(Value, Type == \"BLOOD_TYPE\"), "
        "latest_ingestion_utc = max(IngestionTime), "
        "row_count = count()"
        " | extend phone_birth_date_source = iif(isnotempty(tostring(phone_birth_date)), \"smartphone\", \"none\"), "
        "phone_gender_source = iif(isnotempty(tostring(phone_gender)), \"smartphone\", \"none\"), "
        "phone_blood_type_source = iif(isnotempty(tostring(phone_blood_type)), \"smartphone\", \"none\")"
    )


def _phone_daily_mv_query(user_id: str, start: str, end: str, type_names: List[str]) -> str:
    activity_daily = physical_name(ACTIVITY_DAILY)
    return (
        f"let TargetUserId={_quote(user_id)};\n"
        f"let StartLocal={start};\n"
        f"let EndLocal={end};\n"
        f"{tabular_call(ACTIVITY_DAILY, 'TargetUserId', 'StartLocal', 'EndLocal')}"
        " | where UserId == TargetUserId"
        " | where LocalDate >= StartLocal and LocalDate < EndLocal"
        f" | where Type in ({_string_list(type_names)})"
        " | summarize ValueExact=sum(ValueSumExact), RowCount=count(), ContributingIntervalCount=sum(ContributingIntervalCount), Sources=make_set(strcat(SourcePlatform,\":\",SourceName),10) by LocalDate, Type, Unit"
        f" | extend Value=tolong(round(ValueExact,0)), SourceTable={_double_quote(activity_daily)}"
        " | project Period=LocalDate, LocalDate, Type, Unit, Value, ValueExact, SourceTable, RowCount, ContributingIntervalCount, Sources"
        " | order by LocalDate asc, Type asc"
    )


def _phone_daily_mv_summary_query(user_id: str, start: str, end: str, type_names: List[str]) -> str:
    activity_daily = physical_name(ACTIVITY_DAILY)
    return (
        f"let TargetUserId={_quote(user_id)};\n"
        f"let StartLocal={start};\n"
        f"let EndLocal={end};\n"
        f"let daily={tabular_call(ACTIVITY_DAILY, 'TargetUserId', 'StartLocal', 'EndLocal')}"
        " | where UserId == TargetUserId"
        " | where LocalDate >= StartLocal and LocalDate < EndLocal"
        f" | where Type in ({_string_list(type_names)})"
        " | summarize ValueExact=sum(ValueSumExact), RowCount=count(), ObservedDays=dcount(LocalDate), LatestLocalDate=max(LocalDate), Sources=make_set(strcat(SourcePlatform,\":\",SourceName),10) by Type, Unit;\n"
        "daily"
        f" | extend Value=tolong(round(ValueExact,0)), SourceTable={_double_quote(activity_daily)}"
        " | extend phone_steps = iif(Type == \"STEPS\", todouble(Value), real(null)), phone_steps_source = iif(Type == \"STEPS\" and RowCount > 0, \"smartphone\", \"none\")"
        " | project Type, Unit, Value, ValueExact, phone_steps, phone_steps_source, SourceTable, RowCount, ObservedDays, LatestLocalDate, Sources"
        " | order by Type asc"
    )


def _phone_hourly_mv_query(user_id: str, start: str, end: str, type_names: List[str]) -> str:
    activity_hourly = physical_name(ACTIVITY_HOURLY)
    return (
        f"let TargetUserId={_quote(user_id)};\n"
        f"let StartLocal={start};\n"
        f"let EndLocal={end};\n"
        f"{tabular_call(ACTIVITY_HOURLY, 'TargetUserId', 'StartLocal', 'EndLocal')}"
        " | where UserId == TargetUserId"
        " | where HourStartLocal >= StartLocal and HourStartLocal < EndLocal"
        f" | where Type in ({_string_list(type_names)})"
        " | summarize ValueExact=sum(ValueSumExact), RowCount=count(), ContributingIntervalCount=sum(ContributingIntervalCount), BoundaryCrossingIntervalCount=sum(BoundaryCrossingIntervalCount), Sources=make_set(strcat(SourcePlatform,\":\",SourceName),10) by HourStartLocal, Type, Unit"
        f" | extend HourEndLocal=HourStartLocal + 1h, Value=tolong(round(ValueExact,0)), SourceTable={_double_quote(activity_hourly)}"
        " | project Period=HourStartLocal, HourStartLocal, HourEndLocal, Type, Unit, Value, ValueExact, SourceTable, RowCount, ContributingIntervalCount, BoundaryCrossingIntervalCount, Sources"
        " | order by HourStartLocal asc, Type asc"
    )


def _phone_interval_mv_query(user_id: str, start: str, end: str, type_names: List[str]) -> str:
    activity_interval = physical_name(ACTIVITY_INTERVAL)
    return (
        f"let TargetUserId={_quote(user_id)};\n"
        f"let StartLocal={start};\n"
        f"let EndLocal={end};\n"
        f"{tabular_call(ACTIVITY_INTERVAL, 'TargetUserId', 'StartLocal', 'EndLocal')}"
        " | where UserId == TargetUserId"
        f" | where Type in ({_string_list(type_names)})"
        " | where ((DateFrom >= StartLocal and DateFrom < EndLocal) or (DateTo > StartLocal and DateTo <= EndLocal) or (DateFrom < StartLocal and DateTo > EndLocal))"
        f" | extend Value=tolong(round(todouble(ValueNumber),0)), SourceTable={_double_quote(activity_interval)}"
        " | project DateFrom, DateTo, Type, Unit, Value, ValueNumber, SourcePlatform, SourceName, LogicalEventKey, IngestionTime, SourceTable"
        " | order by DateFrom asc, Type asc"
        " | take 500"
    )


def _phone_flat_freshness_query(user_id: str, start: str, end: str, type_names: List[str]) -> str:
    activity_interval = physical_name(ACTIVITY_INTERVAL)
    base = _bounded_function_call(ACTIVITY_INTERVAL, user_id, start, end)
    return (
        f"{base}"
        f" | where UserId == {_quote(user_id)}"
        f" | where Type in ({_string_list(type_names)})"
        f" | where DateFrom < {end} and DateTo > {start}"
        " | summarize DedupRowCount=count(), LatestIngestionTimeUtc=max(IngestionTime), LatestDateFromLocal=max(DateFrom), LatestDateToLocal=max(DateTo), ValueExact=sum(todouble(ValueNumber)), Sources=make_set(strcat(SourcePlatform,\":\",SourceName),10) by Type, Unit"
        f" | extend Value=tolong(round(ValueExact,0)), SourceTable={_double_quote(activity_interval)}"
        " | project Type, Unit, Value, ValueExact, SourceTable, DedupRowCount, LatestDateFromLocal, LatestDateToLocal, LatestIngestionTimeUtc, Sources"
        " | order by Type asc"
    )


def _phone_sampled_query(
    user_id: str,
    start: str,
    end: str,
    metrics: List[str],
    question: str,
    latest_sample: bool = False,
) -> str:
    type_names = _metric_types(metrics, PHONE_SAMPLED_METRIC_TYPES)
    if not type_names:
        type_names = ("HEART_RATE",)
    base = _phone_sampled_base(user_id, start, end, list(type_names))
    health_samples = physical_name(HEALTH_SAMPLES)
    q = (question or "").lower()
    latest_only = bool(latest_sample) or any(
        token in q for token in ["current", "right now", "now", "latest"]
    )
    if latest_only and len(metrics) == 1:
        latest_metric = next(
            (metric for metric in metrics if metric in PHONE_SAMPLED_METRIC_TYPES),
            "phone_heart_rate",
        )
        alias = metric_result_alias(METRIC_DEFINITIONS.get(latest_metric, {}).get("summary", ""))
        if not alias:
            alias = "phone_heart_rate"
        return (
            f"{base}"
            " | where isnotnull(ValueNumber) and todouble(ValueNumber) > 0"
            " | extend RecordedAtLocal=coalesce(DateFrom, DateTo)"
            " | sort by RecordedAtLocal desc, IngestionTime desc"
            " | take 1"
            f" | project {alias}=todouble(ValueNumber), {alias}_source=\"smartphone\","
            f" {alias}_recorded_at_local=RecordedAtLocal,"
            f" {alias}_latest_ingestion_utc=IngestionTime,"
            f" Type, Unit=tostring(Unit), SourcePlatform, SourceName, SourceTable={_double_quote(health_samples)}"
        )
    if latest_only:
        # Never project a multi-Type result under the first metric's alias. Keep
        # one correctly labelled latest row per source Type for the grounded LLM
        # response path (for example systolic + diastolic blood pressure).
        return (
            f"{base}"
            " | where isnotnull(ValueNumber)"
            " | extend RecordedAtLocal=coalesce(DateFrom, DateTo)"
            " | summarize arg_max(RecordedAtLocal, *) by Type"
            " | project DateFrom, DateTo, RecordedAtLocal, Type,"
            " Unit=tostring(Unit), Value=todouble(ValueNumber),"
            " SourcePlatform, SourceName, LatestIngestionTimeUtc=IngestionTime,"
            f" SourceTable={_double_quote(health_samples)}"
            " | order by Type asc"
        )
    return (
        f"{base}"
        " | summarize Value=avg(todouble(ValueNumber)), MinValue=min(todouble(ValueNumber)), MaxValue=max(todouble(ValueNumber)), RowCount=count(), LatestDateFromLocal=max(DateFrom), LatestIngestionTimeUtc=max(IngestionTime), Sources=make_set(strcat(SourcePlatform,\":\",SourceName),10) by Type, Unit"
        f" | extend SourceTable={_double_quote(health_samples)}"
        " | project Type, Unit, Value, MinValue, MaxValue, RowCount, LatestDateFromLocal, LatestIngestionTimeUtc, Sources, SourceTable"
        " | order by Type asc"
    )


def _phone_sleep_query(user_id: str, start: str, end: str, latest_only: bool = False, nights_back: int = 0) -> str:
    """One row per sleep NIGHT: the MAIN (longest-in-bed) session per wake-up day.

    The dedup of re-ingested rows, midnight-spanning night sessionization,
    main-vs-nap ranking, data-driven multi-source resolution and per-stage
    aggregation (Deep/REM/Light + efficiency) are all done server-side by the
    ``PhoneSleepNights`` stored function over the PhoneSleepSessionDedupMV /
    PhoneSleepStageDedupMV materialized views (see to_do_sleep_kql_guide.md). The
    materialized views auto-reflect newly ingested data, so this builder simply
    calls the function, keeps the MAIN sleep per night (``IsMainSleep``) and maps
    the columns onto the response contract used by the formatters and the analysis
    pack (``LocalDate`` / ``SleepStartLocal`` / ``SleepEndLocal`` /
    ``phone_sleep_duration_hr`` + ``*_source``), while also surfacing the richer
    minute/stage columns (``AsleepMin``/``AsleepHm``/``DeepPct``/``RemPct``/
    ``SleepEfficiencyPct``...) so the coach can reason about sleep quality.

    The function widens its own lower bound internally to capture a session that
    began before the window start, so no extra widening is needed here. The trailing
    wake-time filter re-bounds the result to the requested window (and satisfies the
    KQL guard's bounded-time-range check).

    ``latest_only`` (single-night questions): widen the lower bound and return the
    Nth most recent main-sleep night before ``end`` (``nights_back`` 0 = most
    recent = "last night", 1 = "the night before", 2 = "the night before that",
    ...). This avoids "no records" when the current wake-day has no sleep yet, and
    makes the multi-turn night step walk through ACTUAL nights regardless of empty
    calendar days; the answer states the night's date so it remains accurate.
    """
    sleep_nightly = physical_name(SLEEP_NIGHTLY)
    uid = _quote(user_id)
    nb = max(0, int(nights_back or 0))
    if latest_only:
        lookback = PHONE_SLEEP_LATEST_LOOKBACK_DAYS + nb
        lower = f"(({end}) - {lookback}d)"
    else:
        lower = f"({start})"
    base = (
        f"{_bounded_function_call(SLEEP_NIGHTLY, user_id, lower, end)}"
        f" | where UserId == {uid}"
        " | where IsMainSleep"
        " | extend HasAsleepDuration=coalesce(column_ifexists(\"HasAsleepDuration\", bool(null)), isnotnull(AsleepMin) and AsleepMin > 0)"
        " | project LocalDate=SleepDay, SleepStartLocal=OnsetLocal, SleepEndLocal=WakeLocal,"
        " sleep_duration_hr=iif(HasAsleepDuration,round(AsleepMin / 60.0, 2),real(null)),"
        " sleep_duration_hr_source=iif(HasAsleepDuration,\"curated\",\"none\"),"
        " sleep_start_local=OnsetLocal, sleep_start_local_source=\"curated\","
        " sleep_end_local=WakeLocal, sleep_end_local_source=\"curated\","
        " sleep_efficiency=iif(HasAsleepDuration,SleepEfficiencyPct,real(null)), sleep_efficiency_source=iif(HasAsleepDuration and isnotnull(SleepEfficiencyPct),\"curated\",\"none\"),"
        " time_in_bed_hr=round(TimeInBedMin / 60.0, 2), time_in_bed_hr_source=\"curated\","
        " awake_duration_min=AwakeMin, awake_duration_min_source=\"curated\","
        " deep_sleep_hr=round(DeepMin / 60.0, 2), deep_sleep_hr_source=\"curated\","
        " rem_sleep_hr=round(RemMin / 60.0, 2), rem_sleep_hr_source=\"curated\","
        " light_sleep_hr=round(LightMin / 60.0, 2), light_sleep_hr_source=\"curated\","
        " phone_sleep_duration_hr=iif(HasAsleepDuration,round(AsleepMin / 60.0, 2),real(null)),"
        " phone_sleep_duration_hr_source=iif(HasAsleepDuration,\"smartphone\",\"none\"),"
        " AsleepMin, AsleepHm, TimeInBedMin, TimeInBedHm, AwakeMin,"
        " DeepMin, RemMin, LightMin, UnstagedAsleepMin,"
        " DeepPct, RemPct, LightPct, AwakePct, SleepEfficiencyPct, HasStageBreakdown, HasAsleepDuration,"
        " SourceName, SourcePlatform,"
        " RowCount=StageRowCount, LatestIngestionTimeUtc=SessionIngestionTime,"
        f" SourceTable={_double_quote(sleep_nightly)}"
        f" | where SleepEndLocal >= {lower} and SleepEndLocal < ({end})"
    )
    if latest_only:
        # The (nights_back+1)th most recent night: take the N most recent, then the
        # oldest of those. Walks back through real nights even with calendar gaps.
        return base + f" | sort by LocalDate desc | take {nb + 1} | sort by LocalDate asc | take 1"
    return base + " | order by LocalDate asc"


def _withings_activity_base(user_id: str, start: str, end: str) -> str:
    return (
        f"{physical_name(WITHINGS_ACTIVITY)}"
        f" | where AppUserId == {_quote(user_id)}"
        f" | where DateLocal >= {start} and DateLocal < {end}"
        " | summarize arg_max(IngestedAt, *) by DateLocal"
    )


def _withings_sleep_base(user_id: str, start: str, end: str) -> str:
    return (
        f"{physical_name(WITHINGS_SLEEP)}"
        f" | where AppUserId == {_quote(user_id)}"
        f" | where SleepEndLocal >= {start} and SleepEndLocal < {end}"
        " | extend SleepStartKey=bin(SleepStartLocal, 1m), SleepEndKey=bin(SleepEndLocal, 1m)"
        " | summarize arg_max(IngestedAt, *) by SleepStartKey, SleepEndKey"
    )


def _withings_intraday_base(user_id: str, start: str, end: str) -> str:
    return (
        f"{physical_name(WITHINGS_INTRADAY)}"
        f" | where AppUserId == {_quote(user_id)}"
        f" | where TimestampLocal >= {start} and TimestampLocal < {end}"
        " | summarize arg_max(IngestedAt, *) by TimestampLocal"
    )


def _withings_sleep_detail_base(user_id: str, start: str, end: str) -> str:
    return (
        f"{physical_name(WITHINGS_SLEEP_DETAIL)}"
        f" | where AppUserId == {_quote(user_id)}"
        f" | where SampleLocal >= {start} and SampleLocal < {end}"
        " | summarize arg_max(IngestedAt, *) by SampleEpoch"
    )


def _withings_workouts_base(user_id: str, start: str, end: str) -> str:
    return (
        f"{physical_name(WITHINGS_WORKOUTS)}"
        f" | where AppUserId == {_quote(user_id)}"
        f" | where StartLocal >= {start} and StartLocal < {end}"
        " | summarize arg_max(IngestedAt, *) by StartLocal, EndLocal, WithingsId"
    )


def _withings_night_event_base(user_id: str, start: str, end: str) -> str:
    return (
        f"{physical_name(WITHINGS_NIGHT_EVENTS)}"
        f" | where AppUserId == {_quote(user_id)}"
        f" | where StartLocal >= {start} and StartLocal < {end}"
        " | summarize arg_max(IngestedAt, *) by WithingsId, EventCode, PairIndex"
    )


def _withings_base_query(resource: str, user_id: str, start: str, end: str) -> Optional[str]:
    builders = {
        WITHINGS_ACTIVITY: _withings_activity_base,
        WITHINGS_SLEEP: _withings_sleep_base,
        WITHINGS_INTRADAY: _withings_intraday_base,
        WITHINGS_SLEEP_DETAIL: _withings_sleep_detail_base,
        WITHINGS_WORKOUTS: _withings_workouts_base,
        WITHINGS_NIGHT_EVENTS: _withings_night_event_base,
    }
    builder = builders.get(_logical_resource(resource))
    return builder(user_id, start, end) if builder else None


def _optional_withings_enabled() -> bool:
    return str(os.environ.get("CHATBOT_ENABLE_OPTIONAL_WITHINGS_TABLES", "false")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _catalog_withings_metrics(metrics: List[str]) -> List[str]:
    return [
        metric
        for metric in metrics
        if _metric_resource(metric) in WITHINGS_RESOURCE_IDS
    ]


def _withings_metrics(metrics: List[str]) -> List[str]:
    if not _optional_withings_enabled():
        return []
    return _catalog_withings_metrics(metrics)


def _primary_withings_table(metrics: List[str]) -> str:
    counts: Dict[str, int] = {}
    for metric in _withings_metrics(metrics):
        resource = _metric_resource(metric)
        if resource:
            counts[resource] = counts.get(resource, 0) + 1
    if counts:
        return max(counts, key=counts.get)
    return WITHINGS_SLEEP


def _withings_summary_exprs(resource: str, metrics: List[str], mode: str = "summary") -> List[str]:
    resource_id = _logical_resource(resource)
    exprs: List[str] = []
    for metric in metrics:
        definition = METRIC_DEFINITIONS.get(metric, {})
        if _metric_resource(metric) != resource_id:
            continue
        expr = str(definition.get(mode) or "").strip()
        if expr and expr not in exprs:
            exprs.append(expr)
    return exprs


def _withings_metric_alias(metric: str) -> str:
    return metric_result_alias(METRIC_DEFINITIONS.get(metric, {}).get("summary", ""))


def _withings_metric_value_column(metric: str) -> str:
    """Return the vendor column used by optional direct-source helpers.

    Kusto's ``sum`` over an empty input is zero, which must not be treated as a
    real observation. The column map lets the explicitly enabled compatibility
    path carry a per-metric row count alongside the aggregate. Generic requests
    do not enter this path.
    """

    columns = {
        "steps": "Steps",
        "distance": "Distance",
        "calories": "Calories",
        "total_calories": "TotalCalories",
        "active": "Active",
        "heart_rate": "HeartRate",
        "total_sleep_time": "TotalSleepTime",
        "sleep_start_local": "SleepStartLocal",
        "sleep_end_local": "SleepEndLocal",
        "sleep_efficiency": "SleepEfficiency",
        "deep_sleep": "DeepSleep",
        "rem_sleep": "RemSleep",
        "light_sleep": "LightSleep",
        "workout_count": "WithingsId",
    }
    return columns.get(metric, "AppUserId")


def _phone_fallback_alias(metric: str) -> str:
    aliases = {
        "total_sleep_time": "phone_sleep_duration_hr",
        "sleep_start_local": "phone_sleep_start_local",
        "sleep_end_local": "phone_sleep_end_local",
        "sleep_efficiency": "phone_sleep_efficiency",
        "deep_sleep": "phone_deep_sleep_hr",
        "rem_sleep": "phone_rem_sleep_hr",
        "light_sleep": "phone_light_sleep_hr",
        "time_in_bed": "phone_time_in_bed_hr",
        "awake_duration": "phone_awake_duration_min",
    }
    if metric in aliases:
        return aliases[metric]
    return "phone_" + metric_result_alias(METRIC_DEFINITIONS.get(metric, {}).get("summary", ""))


def _phone_metric_for_query(metric: str) -> str:
    """Resolve either a generic fallback metric or an explicit Phone metric."""

    if metric in PHONE_CUMULATIVE_METRIC_TYPES or metric in PHONE_SAMPLED_METRIC_TYPES:
        return metric
    if metric in PHONE_SLEEP_NIGHTS_METRICS or metric == "phone_workout_count":
        return metric
    if metric in PHONE_PROFILE_METRICS:
        return metric
    if metric in WITHINGS_PHONE_SLEEP_FALLBACK_METRICS:
        return WITHINGS_TO_PHONE_METRIC_PRIORITY.get(metric, "phone_sleep_duration")
    return WITHINGS_TO_PHONE_METRIC_PRIORITY.get(metric, metric)


def _phone_scalar_alias(metric: str) -> str:
    """Return the aggregate column name for a generic fallback or Phone metric."""

    if str(metric).startswith("phone_"):
        return metric_result_alias(METRIC_DEFINITIONS.get(metric, {}).get("summary", ""))
    return _phone_fallback_alias(metric)


def _withings_has_phone_fallback(metric: str) -> bool:
    return metric in WITHINGS_TO_PHONE_METRIC_PRIORITY or metric in WITHINGS_PHONE_SLEEP_FALLBACK_METRICS


def _explicit_phone_comparison_metric(metric: str, metrics: List[str]) -> str:
    """Return the curated counterpart in an explicit provider comparison.

    This helper is limited to the optional legacy comparison path. Ordinary
    source-neutral metrics are resolved directly from the curated layer and do
    not use this pairing logic.
    """

    phone_metric = WITHINGS_TO_PHONE_METRIC_PRIORITY.get(metric)
    if not phone_metric and metric in WITHINGS_PHONE_SLEEP_FALLBACK_METRICS:
        phone_metric = "phone_sleep_duration"
    return phone_metric if phone_metric in metrics else ""


def _phone_activity_fallback_summary(
    user_id: str,
    start: str,
    end: str,
    metrics: List[str],
    period_column: str = "",
    variable_name: str = "smartphone",
) -> str:
    expressions: List[str] = []
    types: List[str] = []
    for metric in metrics:
        phone_metric = _phone_metric_for_query(metric)
        for type_name in PHONE_CUMULATIVE_METRIC_TYPES.get(phone_metric or "", ()):
            if type_name not in types:
                types.append(type_name)
        phone_alias = _phone_scalar_alias(metric)
        phone_types = PHONE_CUMULATIVE_METRIC_TYPES.get(phone_metric or "", ())
        if not phone_alias or not phone_types:
            continue
        if len(phone_types) == 1:
            predicate = f'Type == "{phone_types[0]}"'
        else:
            predicate = f"Type in ({_string_list(phone_types)})"
        expressions.append(f"{phone_alias}=sumif(todouble(ValueSumExact), {predicate})")
        expressions.append(f"{phone_alias}_rows=countif({predicate})")
    if not expressions:
        expressions.append("phone_steps=sumif(todouble(ValueSumExact), Type == \"STEPS\")")
        types = ["STEPS"]
    grouping = f" by PhonePeriod={period_column}" if period_column else ""
    activity_daily = physical_name(ACTIVITY_DAILY)
    return (
        f"let {variable_name}={_bounded_function_call(ACTIVITY_DAILY, user_id, start, end)}"
        f" | where UserId == {_quote(user_id)}"
        f" | where LocalDate >= {start} and LocalDate < {end}"
        f" | where Type in ({_string_list(types)})"
        f" | summarize {', '.join(expressions)}, smartphone_rows=count(){grouping};\n"
    )


def _phone_sampled_fallback_summary(
    user_id: str,
    start: str,
    end: str,
    metrics: List[str],
    period_column: str = "",
    variable_name: str = "smartphone",
) -> str:
    type_names: List[str] = []
    expressions: List[str] = []
    for metric in metrics:
        phone_metric = _phone_metric_for_query(metric)
        metric_types = PHONE_SAMPLED_METRIC_TYPES.get(phone_metric or "", ())
        for type_name in metric_types:
            if type_name not in type_names:
                type_names.append(type_name)
        phone_alias = _phone_scalar_alias(metric)
        if phone_alias and metric_types:
            if len(metric_types) == 1:
                predicate = f'Type == "{metric_types[0]}"'
            else:
                predicate = f"Type in ({_string_list(metric_types)})"
            aggregate = "avgif"
            if metric == "heart_rate_max":
                aggregate = "maxif"
            elif metric == "heart_rate_min":
                aggregate = "minif"
            expressions.append(f"{phone_alias}={aggregate}(todouble(ValueNumber), {predicate})")
            expressions.append(
                f"{phone_alias}_rows=countif({predicate} and isnotnull(ValueNumber))"
            )
    if not type_names:
        type_names = ["HEART_RATE"]
    if not expressions:
        expressions = ["phone_heart_rate_avg=avgif(todouble(ValueNumber), Type == \"HEART_RATE\")"]
    grouping = f" by PhonePeriod={period_column}" if period_column else ""
    return (
        f"let {variable_name}={_phone_sampled_base(user_id, start, end, type_names)}"
        f" | summarize {', '.join(expressions)}, smartphone_rows=count(){grouping};\n"
    )


def _phone_sleep_fallback_summary(
    user_id: str,
    start: str,
    end: str,
    latest_only: bool,
    period_column: str = "",
    variable_name: str = "smartphone",
) -> str:
    sleep_rows = _phone_sleep_query(user_id, start, end, latest_only=latest_only)
    grouping = f" by PhonePeriod={period_column}" if period_column else ""
    return (
        f"let {variable_name}=("
        f"{sleep_rows}"
        ") | summarize "
        "phone_sleep_duration_hr=avg(phone_sleep_duration_hr), "
        "phone_sleep_start_local=min(SleepStartLocal), "
        "phone_sleep_end_local=max(SleepEndLocal), "
        "phone_sleep_efficiency=avg(SleepEfficiencyPct), "
        "phone_deep_sleep_hr=avg(DeepMin) / 60.0, "
        "phone_rem_sleep_hr=avg(RemMin) / 60.0, "
        "phone_light_sleep_hr=avg(LightMin) / 60.0, "
        "phone_time_in_bed_hr=avg(TimeInBedMin) / 60.0, "
        "phone_awake_duration_min=avg(AwakeMin), "
        "phone_sleep_sessions=count(), "
        "phone_sleep_duration_hr_rows=countif(isnotnull(AsleepMin)), "
        "phone_sleep_start_local_rows=countif(isnotnull(SleepStartLocal)), "
        "phone_sleep_end_local_rows=countif(isnotnull(SleepEndLocal)), "
        "phone_sleep_efficiency_rows=countif(isnotnull(SleepEfficiencyPct)), "
        "phone_deep_sleep_hr_rows=countif(isnotnull(DeepMin)), "
        "phone_rem_sleep_hr_rows=countif(isnotnull(RemMin)), "
        "phone_light_sleep_hr_rows=countif(isnotnull(LightMin)), "
        "phone_time_in_bed_hr_rows=countif(isnotnull(TimeInBedMin)), "
        "phone_awake_duration_min_rows=countif(isnotnull(AwakeMin)), "
        "phone_sleep_sessions_rows=count(), "
        f"smartphone_rows=count(){grouping};\n"
    )


def _phone_workout_fallback_summary(
    user_id: str,
    start: str,
    end: str,
    period_column: str = "",
    variable_name: str = "smartphone",
) -> str:
    grouping = f" by PhonePeriod={period_column}" if period_column else ""
    base = _bounded_function_call(WORKOUT_SESSIONS, user_id, start, end)
    return (
        f"let {variable_name}={base}"
        f" | where UserId == {_quote(user_id)}"
        f" | where StartLocal >= {start} and StartLocal < {end}"
        " | extend DateFrom=StartLocal"
        f" | summarize phone_workout_count=count(), phone_workout_count_rows=count(), smartphone_rows=count(){grouping};\n"
    )


def _workout_sessions_query(
    user_id: str,
    start: str,
    end: str,
    metrics: List[str],
    *,
    trend: bool = False,
) -> Optional[str]:
    """Compile a simple schema-v1 workout query over the shared contract.

    Rich filters, cross-resource conditions, ordered selectors, and arbitrary
    aggregations belong to the schema-v2 algebra.  This compatibility path
    preserves the production behavior of straightforward workout summaries and
    daily trends without exposing or reparsing a backend payload.
    """

    selected: List[Tuple[str, str]] = []
    for metric in metrics:
        aggregate = WORKOUT_METRIC_AGGREGATES.get(metric)
        if aggregate and aggregate not in selected:
            selected.append(aggregate)
    if not selected:
        return None

    workout_sessions = physical_name(WORKOUT_SESSIONS)
    aggregate_parts = ["RowCount=count()"] + [
        f"{alias}={expression}" for alias, expression in selected
    ]
    grouping = " by LocalDate=startofday(WorkoutStartLocal)" if trend else ""
    source_parts = [
        f'{alias}_source=iif(RowCount > 0, "smartphone", "none")'
        for alias, _ in selected
    ]
    query = (
        f"{_bounded_function_call(WORKOUT_SESSIONS, user_id, start, end)}"
        " | project UserId, WorkoutId, WorkoutStartLocal=StartLocal, "
        "WorkoutEndLocal=EndLocal, ActivityName, ActivityCode, DurationMin, "
        "AverageHeartRateBpm, ElevationGain, Distance, DistanceUnit, "
        "CaloriesKcal, Steps, ActiveZoneMinutes, Speed, SpeedUnit, "
        "SourcePlatform, SourceName, IngestionTime"
        f" | where UserId == {_quote(user_id)}"
        f" | where WorkoutStartLocal >= {start} and WorkoutStartLocal < {end}"
        f" | summarize {', '.join(aggregate_parts)}{grouping}"
        f" | extend {', '.join(source_parts)}, SourceTable={_double_quote(workout_sessions)}"
    )
    if trend:
        query += " | order by LocalDate asc"
    return query


def _phone_scalar_value_projections(variable_name: str, metrics: List[str]) -> List[str]:
    """Project scalar Phone values with a per-metric source label.

    The fallback summary helpers already produce ``<alias>`` plus
    ``<alias>_rows`` for sampled, cumulative, sleep, and workout values.  This
    shared projection layer keeps mixed-source monitoring queries from
    reimplementing the same null-versus-zero handling for every Phone table.
    """

    projections: List[str] = []
    for metric in metrics:
        alias = _phone_scalar_alias(metric)
        if not alias:
            continue
        value = f"toscalar({variable_name} | project {alias})"
        if metric in PHONE_PROFILE_METRICS:
            source = f"toscalar({variable_name} | project {alias}_source)"
            projections.append(f"{alias}={value}")
            projections.append(f"{alias}_source={source}")
            continue
        rows = f"toscalar({variable_name} | project {alias}_rows)"
        if metric in PHONE_SLEEP_DATETIME_METRICS:
            usable = f"iif({rows} > 0, todatetime({value}), datetime(null))"
            projections.append(f"{alias}={usable}")
            projections.append(
                f"{alias}_source=iif(isnotnull({usable}), \"smartphone\", \"none\")"
            )
            continue
        usable = f"iif({rows} > 0, todouble({value}), real(null))"
        projections.append(f"{alias}={usable}")
        projections.append(
            f"{alias}_source=iif(isnotnull({usable}), \"smartphone\", \"none\")"
        )
    return projections


def _phone_explicit_scalar_parts(
    user_id: str,
    start: str,
    end: str,
    metrics: List[str],
    latest_sleep: bool = False,
    variable_prefix: str = "explicit_phone",
) -> Tuple[str, List[str]]:
    """Return reusable Phone scalar fragments for explicit ``phone_*`` metrics.

    This deliberately composes the same MV, sampled, sleep, workout, and
    profile summaries used by the dedicated Phone routes.  It is used only
    when a Withings-primary query also needs an independent Phone-only metric,
    which prevents that explicit metric from disappearing behind the generic
    source-priority branch.
    """

    selected: List[str] = []
    for metric in metrics:
        if str(metric).startswith("phone_") and metric in METRIC_DEFINITIONS and metric not in selected:
            selected.append(metric)

    fragments: List[str] = []
    projections: List[str] = []
    groups = (
        (
            [metric for metric in selected if metric in PHONE_CUMULATIVE_METRIC_TYPES],
            _phone_activity_fallback_summary,
            {"variable_name": f"{variable_prefix}_cumulative"},
        ),
        (
            [metric for metric in selected if metric in PHONE_SAMPLED_METRIC_TYPES],
            _phone_sampled_fallback_summary,
            {"variable_name": f"{variable_prefix}_sampled"},
        ),
    )
    for group_metrics, builder, kwargs in groups:
        if not group_metrics:
            continue
        variable_name = str(kwargs["variable_name"])
        fragments.append(builder(user_id, start, end, group_metrics, **kwargs))
        projections.extend(_phone_scalar_value_projections(variable_name, group_metrics))

    sleep_metrics = [metric for metric in selected if metric in PHONE_SLEEP_NIGHTS_METRICS]
    if sleep_metrics:
        variable_name = f"{variable_prefix}_sleep"
        fragments.append(
            _phone_sleep_fallback_summary(
                user_id,
                start,
                end,
                latest_only=latest_sleep,
                variable_name=variable_name,
            )
        )
        projections.extend(_phone_scalar_value_projections(variable_name, sleep_metrics))

    if "phone_workout_count" in selected:
        variable_name = f"{variable_prefix}_workout"
        fragments.append(_phone_workout_fallback_summary(user_id, start, end, variable_name=variable_name))
        projections.extend(_phone_scalar_value_projections(variable_name, ["phone_workout_count"]))

    profile_metrics = [metric for metric in selected if metric in PHONE_PROFILE_METRICS]
    if profile_metrics:
        variable_name = f"{variable_prefix}_profile"
        fragments.append(f"let {variable_name}=({_profile_query(user_id, profile_metrics)});\n")
        projections.extend(_phone_scalar_value_projections(variable_name, profile_metrics))

    return "".join(fragments), projections


def _unpaired_explicit_phone_metrics(metrics: List[str]) -> List[str]:
    """Keep Phone metrics that a generic scalar builder does not already emit."""

    paired = {
        phone_metric
        for metric in _withings_metrics(metrics)
        for phone_metric in [WITHINGS_TO_PHONE_METRIC_PRIORITY.get(metric)]
        if phone_metric and phone_metric in metrics
    }
    return [
        metric
        for metric in metrics
        if str(metric).startswith("phone_") and metric not in paired
    ]


def _extend_unique_projections(target: List[str], incoming: List[str]) -> None:
    """Append projections once per output column, preserving request order."""

    aliases = {
        projection.split("=", 1)[0].strip()
        for projection in target
        if "=" in projection
    }
    for projection in incoming:
        alias = projection.split("=", 1)[0].strip()
        if alias and alias not in aliases:
            target.append(projection)
            aliases.add(alias)


def _withings_phone_scalar_query(
    user_id: str,
    resource: str,
    start: str,
    end: str,
    metrics: List[str],
    latest_sleep: bool = False,
    withings_name: str = "withings",
    smartphone_name: str = "smartphone",
    return_parts: bool = False,
) -> Any:
    """Prefer one Withings scalar result, then coalesce a current Phone result.

    This is intentionally a single source-aware KQL query rather than two graph
    branches. It avoids duplicate generic values while preserving explicit
    ``phone_*`` output only for a deliberate two-source request.
    """

    resource_id = _logical_resource(resource)
    base = _withings_base_query(resource_id, user_id, start, end)
    if not base:
        return None
    table_metrics = [m for m in metrics if _metric_resource(m) == resource_id]
    if not table_metrics:
        return None

    # Start/end questions expect one sleep session, not a min/max assembled
    # from several nights (or from a night plus a nap). Match PhoneSleepNights'
    # main/latest-night behavior by reducing the Withings side first.
    if resource_id == WITHINGS_SLEEP and (
        latest_sleep or any(metric in {"sleep_start_local", "sleep_end_local"} for metric in table_metrics)
    ):
        base = f"({base} | sort by SleepEndLocal desc | take 1)"

    summary_exprs = _withings_summary_exprs(resource_id, table_metrics)
    if not summary_exprs:
        return None
    presence_exprs: List[str] = []
    for metric in table_metrics:
        alias = _withings_metric_alias(metric)
        if alias:
            presence_expr = (
                f"withings_{alias}_rows=countif(isnotnull({_withings_metric_value_column(metric)}))"
            )
            if presence_expr not in presence_exprs:
                presence_exprs.append(presence_expr)
    withings = (
        f"let {withings_name}={base} | summarize {', '.join(summary_exprs + presence_exprs)}, "
        "withings_rows=count();\n"
    )

    fallback_metrics = [metric for metric in table_metrics if _withings_has_phone_fallback(metric)]
    smartphone = ""
    if fallback_metrics:
        if resource_id == WITHINGS_ACTIVITY:
            smartphone = _phone_activity_fallback_summary(
                user_id, start, end, fallback_metrics, variable_name=smartphone_name
            )
        elif resource_id == WITHINGS_SLEEP:
            smartphone = _phone_sleep_fallback_summary(
                user_id, start, end, latest_sleep, variable_name=smartphone_name
            )
        elif resource_id in {WITHINGS_INTRADAY, WITHINGS_SLEEP_DETAIL}:
            smartphone = _phone_sampled_fallback_summary(
                user_id, start, end, fallback_metrics, variable_name=smartphone_name
            )
        elif resource_id == WITHINGS_WORKOUTS:
            smartphone = _phone_workout_fallback_summary(
                user_id, start, end, variable_name=smartphone_name
            )

    projections: List[str] = []
    for metric in table_metrics:
        alias = _withings_metric_alias(metric)
        if not alias:
            continue
        withings_value = f"toscalar({withings_name} | project {alias})"
        withings_presence = f"toscalar({withings_name} | project withings_{alias}_rows)"
        if metric in fallback_metrics and smartphone:
            phone_alias = _phone_fallback_alias(metric)
            phone_value = f"toscalar({smartphone_name} | project {phone_alias})"
            phone_presence = f"toscalar({smartphone_name} | project {phone_alias}_rows)"
            explicit_phone_metric = _explicit_phone_comparison_metric(metric, metrics)
            if metric in {"sleep_start_local", "sleep_end_local"}:
                usable_withings = (
                    f"iif({withings_presence} > 0, "
                    f"todatetime({withings_value}), datetime(null))"
                )
                usable_phone = (
                    f"iif({phone_presence} > 0, "
                    f"todatetime({phone_value}), datetime(null))"
                )
                if explicit_phone_metric:
                    projections.append(f"{metric}={usable_withings}")
                    projections.append(
                        f"{metric}_source=iif(isnotnull({usable_withings}), \"withings\", \"none\")"
                    )
                else:
                    projections.append(f"{metric}=coalesce({usable_withings}, {usable_phone})")
                    projections.append(
                        f"{metric}_source=iif(isnotnull({usable_withings}), \"withings\", iif(isnotnull({usable_phone}), \"smartphone\", \"none\"))"
                    )
            else:
                usable_withings = (
                    f"iif({withings_presence} > 0, "
                    f"todouble({withings_value}), real(null))"
                )
                usable_phone = (
                    f"iif({phone_presence} > 0, "
                    f"todouble({phone_value}), real(null))"
                )
                if explicit_phone_metric:
                    projections.append(f"{alias}={usable_withings}")
                    projections.append(
                        f"{alias}_source=iif(isnotnull({usable_withings}), \"withings\", \"none\")"
                    )
                else:
                    projections.append(f"{alias}=coalesce({usable_withings}, {usable_phone})")
                    projections.append(
                        f"{alias}_source=iif(isnotnull({usable_withings}), \"withings\", iif(isnotnull({usable_phone}), \"smartphone\", \"none\"))"
                    )
            phone_metric = WITHINGS_TO_PHONE_METRIC_PRIORITY.get(metric)
            if phone_metric and phone_metric in metrics:
                phone_metric_alias = metric_result_alias(METRIC_DEFINITIONS.get(phone_metric, {}).get("summary", ""))
                if phone_metric_alias:
                    projections.append(f"{phone_metric_alias}={usable_phone}")
                    projections.append(
                        f"{phone_metric_alias}_source=iif(isnotnull({usable_phone}), \"smartphone\", \"none\")"
                    )
        else:
            projections.append(f"{alias}={withings_value}")
            projections.append(
                f"{alias}_source=iif({withings_presence} > 0 and isnotnull({withings_value}), \"withings\", \"none\")"
            )

    if return_parts:
        return withings + smartphone, projections
    return withings + smartphone + "print " + ", ".join(projections)


def _withings_standard_summary_query(user_id: str, resource: str, start: str, end: str, metrics: List[str]) -> Optional[str]:
    resource_id = _logical_resource(resource)
    base = _withings_base_query(resource_id, user_id, start, end)
    if not base:
        return None
    expressions = _withings_summary_exprs(resource_id, metrics)
    if not expressions:
        return None
    aliases = [metric_result_alias(expr) for expr in expressions]
    source_fields = [
        f'{alias}_source=iif(withings_rows > 0 and isnotnull({alias}), \"withings\", \"none\")'
        for alias in aliases
        if alias
    ]
    suffix = f" | extend {', '.join(source_fields)}" if source_fields else ""
    return f"{base} | summarize {', '.join(expressions)}, withings_rows=count(){suffix}"


def _withings_phone_latest_heart_rate_query(
    user_id: str,
    start: str,
    end: str,
) -> str:
    """Return one latest HR observation for the legacy provider comparison.

    The current generic ``heart_rate`` catalog route reads HealthDataFlat and
    does not call this helper. It remains only for an explicitly enabled legacy
    direct-Withings workflow. Measurement and ingestion timestamps stay
    separate so sync time cannot be presented as record time.
    """

    withings_base = _withings_intraday_base(user_id, start, end)
    smartphone_base = _phone_sampled_base(user_id, start, end, ["HEART_RATE"])
    withings_intraday = physical_name(WITHINGS_INTRADAY)
    health_samples = physical_name(HEALTH_SAMPLES)
    return (
        f"let withings_latest={withings_base}"
        " | where isnotnull(HeartRate) and todouble(HeartRate) > 0"
        " | sort by TimestampLocal desc, IngestedAt desc"
        " | take 1"
        " | project heart_rate_avg=todouble(HeartRate), heart_rate_avg_source=\"withings\","
        " heart_rate_avg_recorded_at_local=TimestampLocal,"
        " heart_rate_avg_latest_ingestion_utc=IngestedAt,"
        f" Unit=\"bpm\", SourceTable={_double_quote(withings_intraday)}, SourcePriority=0;\n"
        f"let smartphone_latest={smartphone_base}"
        " | where isnotnull(ValueNumber) and todouble(ValueNumber) > 0"
        " | extend RecordedAtLocal=coalesce(DateFrom, DateTo)"
        " | sort by RecordedAtLocal desc, IngestionTime desc"
        " | take 1"
        " | project heart_rate_avg=todouble(ValueNumber), heart_rate_avg_source=\"smartphone\","
        " heart_rate_avg_recorded_at_local=RecordedAtLocal,"
        " heart_rate_avg_latest_ingestion_utc=IngestionTime,"
        f" Unit=tostring(Unit), SourceTable={_double_quote(health_samples)}, SourcePriority=1;\n"
        "union withings_latest, smartphone_latest"
        " | sort by SourcePriority asc, heart_rate_avg_recorded_at_local desc"
        " | take 1"
        " | project-away SourcePriority"
    )


def _build_withings_monitoring_kql(user_id: str, question: str, plan: Dict[str, Any]) -> Optional[str]:
    metrics = _normalize_metrics(plan.get("metrics", []) if isinstance(plan.get("metrics"), list) else [])
    withings_metrics = _withings_metrics(metrics)
    if not withings_metrics:
        return None
    explicit_phone_metrics = _unpaired_explicit_phone_metrics(metrics)
    table = _primary_withings_table(withings_metrics)
    start, end = _kql_phone_window_for_plan(plan)
    start = _widen_start_for_comparison(plan, start)

    if bool(plan.get("latest_sample")) and metrics == ["heart_rate"]:
        return _withings_phone_latest_heart_rate_query(user_id, start, end)

    tables: List[str] = []
    for metric in withings_metrics:
        metric_table = _metric_resource(metric)
        if metric_table and metric_table not in tables:
            tables.append(metric_table)

    # A combined request such as "steps, sleep, and heart rate" crosses three
    # Withings tables. Build one bounded KQL program with one scalar result per
    # table rather than silently dropping all but the primary table.
    if len(tables) > 1:
        fragments: List[str] = []
        projections: List[str] = []
        for index, metric_table in enumerate(tables, start=1):
            table_metrics = [
                metric
                for metric in withings_metrics
                if _metric_resource(metric) == metric_table
            ]
            latest_sleep = metric_table == WITHINGS_SLEEP and (
                _is_single_night_sleep(plan)
                or any(metric in {"sleep_start_local", "sleep_end_local"} for metric in table_metrics)
            )
            parts = _withings_phone_scalar_query(
                user_id,
                metric_table,
                start,
                end,
                metrics,
                latest_sleep=latest_sleep,
                withings_name=f"withings_{index}",
                smartphone_name=f"smartphone_{index}",
                return_parts=True,
            )
            if not isinstance(parts, tuple) or len(parts) != 2:
                continue
            fragment, table_projections = parts
            if isinstance(fragment, str):
                fragments.append(fragment)
            if isinstance(table_projections, list):
                _extend_unique_projections(projections, table_projections)
        if explicit_phone_metrics:
            phone_fragment, phone_projections = _phone_explicit_scalar_parts(
                user_id,
                start,
                end,
                explicit_phone_metrics,
                latest_sleep=_is_single_night_sleep(plan),
            )
            if phone_fragment:
                fragments.append(phone_fragment)
            _extend_unique_projections(projections, phone_projections)
        if fragments and projections:
            return "".join(fragments) + "print " + ", ".join(projections)

    table_metrics = [m for m in withings_metrics if _metric_resource(m) == table]

    latest_sleep = table == WITHINGS_SLEEP and (
        _is_single_night_sleep(plan)
        or any(metric in {"sleep_start_local", "sleep_end_local"} for metric in table_metrics)
    )
    if any(_withings_has_phone_fallback(metric) for metric in table_metrics) or explicit_phone_metrics:
        parts = _withings_phone_scalar_query(
            user_id,
            table,
            start,
            end,
            metrics,
            latest_sleep=latest_sleep,
            return_parts=True,
        )
        if isinstance(parts, tuple) and len(parts) == 2:
            fragment, projections = parts
            if explicit_phone_metrics:
                phone_fragment, phone_projections = _phone_explicit_scalar_parts(
                    user_id,
                    start,
                    end,
                    explicit_phone_metrics,
                    latest_sleep=latest_sleep,
                )
                fragment += phone_fragment
                _extend_unique_projections(projections, phone_projections)
            return fragment + "print " + ", ".join(projections)
    return _withings_standard_summary_query(user_id, table, start, end, table_metrics)


def _withings_trend_time_key(resource: str) -> str:
    columns = {
        WITHINGS_ACTIVITY: "DateLocal",
        WITHINGS_SLEEP: "SleepEndLocal",
        WITHINGS_INTRADAY: "TimestampLocal",
        WITHINGS_SLEEP_DETAIL: "SampleLocal",
        WITHINGS_WORKOUTS: "StartLocal",
        WITHINGS_NIGHT_EVENTS: "StartLocal",
    }
    return f"startofday({columns.get(_logical_resource(resource), 'TimestampLocal')})"


def _withings_trend_fallback_query(
    user_id: str,
    table: str,
    start: str,
    end: str,
    metrics: List[str],
    variable_name: str = "smartphone",
) -> str:
    resource_id = _logical_resource(table)
    if resource_id == WITHINGS_ACTIVITY:
        return _phone_activity_fallback_summary(
            user_id, start, end, metrics, period_column="LocalDate", variable_name=variable_name
        )
    if resource_id == WITHINGS_SLEEP:
        return _phone_sleep_fallback_summary(
            user_id, start, end, latest_only=False, period_column="LocalDate", variable_name=variable_name
        )
    if resource_id in {WITHINGS_INTRADAY, WITHINGS_SLEEP_DETAIL}:
        return _phone_sampled_fallback_summary(
            user_id,
            start,
            end,
            metrics,
            period_column="startofday(DateFrom)",
            variable_name=variable_name,
        )
    if resource_id == WITHINGS_WORKOUTS:
        return _phone_workout_fallback_summary(
            user_id,
            start,
            end,
            period_column="startofday(DateFrom)",
            variable_name=variable_name,
        )
    return ""


def _withings_metric_trend_series_parts(
    user_id: str,
    metric: str,
    start: str,
    end: str,
    index: int,
    include_value_text: bool = False,
    allow_phone_fallback: bool = True,
) -> Optional[Tuple[str, str]]:
    """Return KQL lets plus a normalized long-format series name for one metric."""

    definition = METRIC_DEFINITIONS.get(metric, {})
    table = _metric_resource(metric)
    expression = str(definition.get("trend") or "").strip()
    alias = metric_result_alias(expression)
    base = _withings_base_query(table, user_id, start, end)
    if not table or not expression or not alias or not base:
        return None

    series_name = f"withings_phone_series_{index}"
    time_key = _withings_trend_time_key(table)
    datetime_metric = metric in {"sleep_start_local", "sleep_end_local"}
    projection = "Period, Metric, Value, Source"
    value_text = ""
    if include_value_text or datetime_metric:
        value_text = ', ValueText=""'
        projection = "Period, Metric, Value, ValueText, Source"
    if not allow_phone_fallback or not _withings_has_phone_fallback(metric):
        if datetime_metric:
            value_projection = (
                f"Value=real(null), ValueText=tostring({alias}), "
                f"Source=iif(isnotnull({alias}), \"withings\", \"none\")"
            )
        else:
            value_projection = f"Value=todouble({alias}), Source=\"withings\"{value_text}"
        return (
            f"let {series_name}={base}"
            f" | summarize {expression} by Period={time_key}"
            f" | extend Metric={_quote(metric)}, {value_projection}"
            f" | project {projection};\n",
            series_name,
        )

    withings_name = f"withings_trend_{index}"
    smartphone_name = f"smartphone_trend_{index}"
    phone_alias = _phone_fallback_alias(metric)
    smartphone = _withings_trend_fallback_query(
        user_id, table, start, end, [metric], variable_name=smartphone_name
    )
    if not smartphone:
        return None
    if datetime_metric:
        resolved_projection = (
            f"Value=real(null), "
            f"ValueText=tostring(coalesce(todatetime(WithingsValue), todatetime({phone_alias}))), "
            f"Source=iif(isnotnull(WithingsValue), \"withings\", "
            f"iif(isnotnull({phone_alias}), \"smartphone\", \"none\"))"
        )
    else:
        resolved_projection = (
            f"Value=coalesce(todouble(WithingsValue), todouble({phone_alias})), "
            f"Source=iif(isnotnull(WithingsValue), \"withings\", "
            f"iif(isnotnull({phone_alias}), \"smartphone\", \"none\")){value_text}"
        )
    fragment = (
        f"let {withings_name}={base}"
        f" | summarize {expression} by WithingsPeriod={time_key}"
        f" | project WithingsPeriod, WithingsValue={alias};\n"
        + smartphone
        + f"let {series_name}={withings_name}"
        + f" | join kind=fullouter {smartphone_name} on $left.WithingsPeriod == $right.PhonePeriod"
        + f" | extend Period=coalesce(WithingsPeriod, PhonePeriod), Metric={_quote(metric)}, "
        + resolved_projection
        + f" | project {projection};\n"
    )
    return fragment, series_name


def _phone_explicit_trend_series_parts(
    user_id: str,
    metric: str,
    start: str,
    end: str,
    index: int,
) -> Optional[Tuple[str, str]]:
    """Return one normalized curated trend series using shared summaries.

    Despite the historical function name, ``metric`` may be either a semantic
    metric (``steps``) or its older explicit ``phone_steps`` alias.
    """

    alias = _phone_scalar_alias(metric)
    if not alias:
        return None
    source_label = "smartphone" if str(metric).startswith("phone_") else "curated"
    source_name = f"explicit_phone_trend_{index}"
    series_name = f"explicit_phone_series_{index}"
    source_table = physical_name(HEALTH_SAMPLES)

    query_metric = _phone_metric_for_query(metric)
    if query_metric in PHONE_CUMULATIVE_METRIC_TYPES:
        source_table = physical_name(ACTIVITY_DAILY)
        fragment = _phone_activity_fallback_summary(
            user_id,
            start,
            end,
            [metric],
            period_column="LocalDate",
            variable_name=source_name,
        )
    elif query_metric in PHONE_SAMPLED_METRIC_TYPES:
        fragment = _phone_sampled_fallback_summary(
            user_id,
            start,
            end,
            [metric],
            period_column="startofday(DateFrom)",
            variable_name=source_name,
        )
    elif query_metric in PHONE_SLEEP_NIGHTS_METRICS:
        source_table = physical_name(SLEEP_NIGHTLY)
        fragment = _phone_sleep_fallback_summary(
            user_id,
            start,
            end,
            latest_only=False,
            period_column="LocalDate",
            variable_name=source_name,
        )
    elif query_metric == "phone_workout_count":
        fragment = _phone_workout_fallback_summary(
            user_id,
            start,
            end,
            period_column="startofday(DateFrom)",
            variable_name=source_name,
        )
    elif query_metric in PHONE_PROFILE_METRICS:
        profile = f"let {source_name}=({_profile_query(user_id, [metric])});\n"
        series = (
            f"let {series_name}={source_name}"
            f" | extend Period={start}, Metric={_quote(metric)}, Value=real(null), "
            f"ValueText=tostring({alias}), Source={alias}_source, "
            f"SourceTable={_quote(source_table)}, EvidenceRows=tolong(1)"
            " | project Period, Metric, Value, ValueText, Source, SourceTable, EvidenceRows;\n"
        )
        return profile + series, series_name
    else:
        return None

    if metric in PHONE_SLEEP_DATETIME_METRICS:
        value_projection = (
            f"Value=real(null), ValueText=tostring({alias}), "
            f"Source=iif({alias}_rows > 0 and isnotnull({alias}), {_quote(source_label)}, \"none\")"
        )
    else:
        value_projection = (
            f"Value=todouble({alias}), ValueText=\"\", "
            f"Source=iif({alias}_rows > 0, {_quote(source_label)}, \"none\")"
        )
    series = (
        f"let {series_name}={source_name}"
        f" | extend Period=PhonePeriod, Metric={_quote(metric)}, {value_projection}, "
        f"SourceTable={_quote(source_table)}, EvidenceRows=tolong({alias}_rows)"
        " | project Period, Metric, Value, ValueText, Source, SourceTable, EvidenceRows;\n"
    )
    return fragment + series, series_name


def _is_curated_metric(metric: str) -> bool:
    """Return whether a semantic metric has a deterministic curated resource."""

    resolved = _phone_metric_for_query(metric)
    return bool(
        resolved in PHONE_CUMULATIVE_METRIC_TYPES
        or resolved in PHONE_SAMPLED_METRIC_TYPES
        or resolved in PHONE_SLEEP_NIGHTS_METRICS
        or resolved == "phone_workout_count"
        or resolved in PHONE_PROFILE_METRICS
    )


def _semantic_resource_family(metric: str) -> str:
    """Return the logical resource family for one canonical metric."""

    resolved = _phone_metric_for_query(metric)
    if resolved in PHONE_CUMULATIVE_METRIC_TYPES:
        return "activity"
    if resolved in PHONE_SAMPLED_METRIC_TYPES:
        return "sampled"
    if resolved in PHONE_SLEEP_NIGHTS_METRICS:
        return "sleep"
    if resolved == "phone_workout_count":
        return "workout"
    if resolved in PHONE_PROFILE_METRICS:
        return "profile"
    table = _metric_resource(metric)
    if table in WITHINGS_RESOURCE_IDS:
        state = "enabled" if _optional_withings_enabled() else "disabled"
        return f"optional-{state}:{table}"
    return ""


def _disabled_optional_metric_series_parts(
    metric: str,
    start: str,
    index: int,
) -> Tuple[str, str]:
    """Return an explicit missing row for a disabled vendor-only metric.

    A mixed core/vendor request must retain every requested metric. Emitting a
    null diagnostic row lets the response report the unavailable vendor field
    without querying its table or silently dropping it from the answer.
    """

    series_name = f"metric_series_{index}"
    return (
        f"let {series_name}=print Period={start}, Metric={_quote(metric)}, "
        "Value=real(null), ValueText=\"\", Source=\"none\", "
        "SourceTable=\"optional-direct-vendor-disabled\", EvidenceRows=long(0), "
        "DiagnosticReason=\"optional_direct_withings_disabled\";\n",
        series_name,
    )


def _semantic_metric_series_parts(
    user_id: str,
    metric: str,
    start: str,
    end: str,
    index: int,
) -> Optional[Tuple[str, str]]:
    """Build one normalized metric series from its approved resource."""

    if _is_curated_metric(metric):
        return _phone_explicit_trend_series_parts(
            user_id,
            metric,
            start,
            end,
            index,
        )
    if metric in _withings_metrics([metric]):
        return _withings_metric_trend_series_parts(
            user_id,
            metric,
            start,
            end,
            index,
            include_value_text=True,
            allow_phone_fallback=False,
        )
    if metric in _catalog_withings_metrics([metric]):
        return _disabled_optional_metric_series_parts(metric, start, index)
    return None


def _build_semantic_multi_metric_kql(
    user_id: str,
    metrics: List[str],
    start: str,
    end: str,
) -> Optional[str]:
    """Compose one Period-aligned series for every requested metric.

    The LLM decides the semantic metrics and time grain. This deterministic
    resolver then binds each metric to its approved resource. Curated and
    optional vendor rows remain separate series: values from mirrored sources
    are never added or coalesced across pipelines.
    """

    selected: List[str] = []
    for metric in metrics:
        if metric not in selected:
            selected.append(metric)
    if len(selected) < 2:
        return None
    families = {_semantic_resource_family(metric) for metric in selected}
    if "" in families or len(families) < 2:
        # Same-family requests retain their optimized builder. In particular,
        # current systolic+diastolic queries must keep one latest row per Type.
        return None

    fragments: List[str] = []
    series_names: List[str] = []
    for index, metric in enumerate(selected, start=1):
        parts = _semantic_metric_series_parts(
            user_id,
            metric,
            start,
            end,
            index,
        )
        if not parts:
            return None
        fragment, series_name = parts
        fragments.append(fragment)
        series_names.append(series_name)

    return (
        "".join(fragments)
        + f"union {', '.join(series_names)}"
        + " | extend SourceLayer=case(SourceTable == 'optional-direct-vendor-disabled', "
        + "'optional-direct-vendor-disabled', Source == 'withings', "
        + "'optional-direct-vendor', 'healthdataflat-derived')"
        + " | order by Period asc, Metric asc"
    )


def build_independent_metric_queries(
    user_id: str,
    plan: Dict[str, Any],
) -> List[Tuple[str, str]]:
    """Return equivalent per-metric queries for partial schema-v1 recovery.

    A cross-resource ``union`` fails as a unit when one stored function is
    temporarily unavailable or has a deployment mismatch. These queries keep
    the same normalized row contract while allowing the executor to preserve
    evidence returned by the healthy resources. Typed computations are
    intentionally excluded because independent execution could change their
    joins, conditions, or selector semantics.
    """

    if plan.get("schema_version") == 2:
        return []
    if str(plan.get("case_type") or "monitoring").lower() not in {
        "monitoring",
        "trend",
        "personal_data_supported",
    }:
        return []
    metrics = _normalize_metrics(
        plan.get("metrics", []) if isinstance(plan.get("metrics"), list) else []
    )
    metrics = list(dict.fromkeys(metrics))
    if len(metrics) < 2:
        return []

    start, end = _kql_phone_window_for_plan(plan)
    start = _widen_start_for_comparison(plan, start)
    queries: List[Tuple[str, str]] = []
    for index, metric in enumerate(metrics, start=1):
        parts = _semantic_metric_series_parts(
            user_id,
            metric,
            start,
            end,
            index,
        )
        if not parts:
            continue
        fragment, series_name = parts
        query = (
            fragment
            + series_name
            + " | extend SourceLayer=case("
            + "SourceTable == 'optional-direct-vendor-disabled', "
            + "'optional-direct-vendor-disabled', Source == 'withings', "
            + "'optional-direct-vendor', 'healthdataflat-derived')"
            + " | order by Period asc, Metric asc"
        )
        queries.append((metric, query))
    return queries


def _build_withings_mixed_source_trend_kql(
    user_id: str,
    metrics: List[str],
    start: str,
    end: str,
) -> Optional[str]:
    """Return an optional, explicitly requested provider-comparison trend.

    This legacy helper is not part of generic metric routing. Each requested
    provider series remains separate so the comparison neither merges sources
    nor drops an unrelated curated measurement.
    """

    fragments: List[str] = []
    series_names: List[str] = []
    index = 1
    for metric in _withings_metrics(metrics):
        explicit_phone_metric = _explicit_phone_comparison_metric(metric, metrics)
        parts = _withings_metric_trend_series_parts(
            user_id,
            metric,
            start,
            end,
            index,
            include_value_text=True,
            allow_phone_fallback=not bool(explicit_phone_metric),
        )
        index += 1
        if not parts:
            continue
        fragment, series_name = parts
        fragments.append(fragment)
        series_names.append(series_name)
    for metric in metrics:
        if not str(metric).startswith("phone_"):
            continue
        parts = _phone_explicit_trend_series_parts(user_id, metric, start, end, index)
        index += 1
        if not parts:
            continue
        fragment, series_name = parts
        fragments.append(fragment)
        series_names.append(series_name)
    if not fragments or not series_names:
        return None
    return "".join(fragments) + f"union {', '.join(series_names)} | order by Period asc, Metric asc"


def _build_withings_multi_table_trend_kql(
    user_id: str,
    metrics: List[str],
    start: str,
    end: str,
) -> Optional[str]:
    """Return all requested cross-table generic metrics without losing series."""

    fragments: List[str] = []
    series_names: List[str] = []
    for index, metric in enumerate(_withings_metrics(metrics), start=1):
        parts = _withings_metric_trend_series_parts(user_id, metric, start, end, index)
        if not parts:
            continue
        fragment, series_name = parts
        fragments.append(fragment)
        series_names.append(series_name)
    if not fragments or not series_names:
        return None
    return "".join(fragments) + f"union {', '.join(series_names)} | order by Period asc, Metric asc"


def _build_withings_trend_kql(user_id: str, question: str, plan: Dict[str, Any]) -> Optional[str]:
    """Return a daily trend for an enabled optional direct-Withings request.

    Generic metrics never enter this function. An explicit provider comparison
    uses separate normalized series instead of merging vendor and curated
    values.
    """

    metrics = _normalize_metrics(plan.get("metrics", []) if isinstance(plan.get("metrics"), list) else [])
    withings_metrics = _withings_metrics(metrics)
    if not withings_metrics:
        return None

    table = _primary_withings_table(withings_metrics)
    table_metrics = [m for m in withings_metrics if _metric_resource(m) == table]
    expressions = _withings_summary_exprs(table, table_metrics, mode="trend")
    if not expressions:
        return None

    start, end = _kql_phone_window_for_plan(plan)
    start = _widen_start_for_comparison(plan, start)
    if any(str(metric).startswith("phone_") for metric in metrics):
        return _build_withings_mixed_source_trend_kql(user_id, metrics, start, end)
    tables = {
        _metric_resource(metric)
        for metric in withings_metrics
    }
    if len(tables) > 1:
        return _build_withings_multi_table_trend_kql(user_id, metrics, start, end)
    time_key = _withings_trend_time_key(table)
    fallback_metrics = [metric for metric in table_metrics if _withings_has_phone_fallback(metric)]

    if not fallback_metrics:
        aliases = [metric_result_alias(expr) for expr in expressions]
        source_fields = [f'{alias}_source="withings"' for alias in aliases if alias]
        suffix = f" | extend {', '.join(source_fields)}" if source_fields else ""
        return (
            f"{_withings_base_query(table, user_id, start, end)}"
            f" | summarize {', '.join(expressions)} by Period={time_key}"
            f"{suffix} | order by Period asc"
        )

    # Keep primary and fallback column names distinct before the full outer
    # join. Reusing the generic alias directly would make a missing Withings
    # row indistinguishable from an actual zero-valued smartphone row.
    withings_aliases = [metric_result_alias(expr) for expr in expressions]
    withings_projection = ["WithingsPeriod"] + [
        f"withings_{alias}={alias}" for alias in withings_aliases if alias
    ]
    withings = (
        f"let withings={_withings_base_query(table, user_id, start, end)}"
        f" | summarize {', '.join(expressions)} by WithingsPeriod={time_key}"
        f" | project {', '.join(withings_projection)};\n"
    )
    smartphone = _withings_trend_fallback_query(user_id, table, start, end, fallback_metrics)
    if not smartphone:
        return None

    resolved_values: List[str] = ["Period=coalesce(WithingsPeriod, PhonePeriod)"]
    output_fields: List[str] = ["Period"]
    for metric in table_metrics:
        alias = _withings_metric_alias(metric)
        if not alias or alias not in withings_aliases:
            continue
        withings_alias = f"withings_{alias}"
        if metric in fallback_metrics:
            phone_alias = _phone_fallback_alias(metric)
            if metric in {"sleep_start_local", "sleep_end_local"}:
                resolved_values.append(
                    f"{alias}=coalesce(todatetime({withings_alias}), todatetime({phone_alias}))"
                )
            else:
                resolved_values.append(
                    f"{alias}=coalesce(todouble({withings_alias}), todouble({phone_alias}))"
                )
            resolved_values.append(
                f"{alias}_source=iif(isnotnull({withings_alias}), \"withings\", iif(isnotnull({phone_alias}), \"smartphone\", \"none\"))"
            )
        else:
            resolved_values.append(f"{alias}={withings_alias}")
            resolved_values.append(f"{alias}_source=iif(isnotnull({withings_alias}), \"withings\", \"none\")")
        if alias not in output_fields:
            output_fields.extend([alias, f"{alias}_source"])

    return (
        withings
        + smartphone
        + "withings"
        + " | join kind=fullouter smartphone on $left.WithingsPeriod == $right.PhonePeriod"
        + f" | extend {', '.join(resolved_values)}"
        + f" | project {', '.join(output_fields)}"
        + " | order by Period asc"
    )


def _conversation_history_query(user_id: str, start_local: str, end_local: str, timezone: str) -> str:
    conversation_history = physical_name(CONVERSATION_HISTORY)
    return (
        f"let TargetAppUserId={_quote(user_id)};\n"
        f"let TargetTimezoneIana={_quote(timezone)};\n"
        f"let StartLocal={start_local};\n"
        f"let EndExclusiveLocal={end_local};\n"
        "let StartUtc=datetime_local_to_utc(StartLocal,TargetTimezoneIana);\n"
        "let EndExclusiveUtc=datetime_local_to_utc(EndExclusiveLocal,TargetTimezoneIana);\n"
        f"{conversation_history}"
        " | where AppUserId == TargetAppUserId"
        " | where EventTimeUtc >= StartUtc and EventTimeUtc < EndExclusiveUtc"
        " | where EventType in (\"user_message\",\"bot_reply\")"
        " | extend EventTimeLocal=datetime_utc_to_local(EventTimeUtc,TargetTimezoneIana)"
        " | project EventTimeUtc, EventTimeLocal, Role, MessageText, Intent, ExecutionStatus"
        " | order by EventTimeUtc desc"
        " | take 50"
    )


def is_conversation_history_request(question: str, plan: Dict[str, Any]) -> bool:
    """Return whether a plan asks for the user's stored conversation history."""

    case_type = str(plan.get("case_type", "")).lower()
    q = (question or "").lower()
    return case_type == "conversation_history_lookup" or any(
        token in q
        for token in [
            "previous conversation",
            "past conversation",
            "what did i ask",
            "what did you answer",
            "earlier chat",
            "conversation history",
        ]
    )


def _widen_start_for_comparison(plan: Dict[str, Any], start_expr: str) -> str:
    """When a period comparison is requested, extend the query window back to the
    BASELINE period start so both the current and previous periods are fetched in
    one query (the response then splits current vs baseline). Without this, a
    precise window like "this week" returns only the current week and the model
    has no last-week data to compare against."""
    if not plan.get("comparison_requested"):
        return start_expr
    tr = plan.get("time_range") if isinstance(plan.get("time_range"), dict) else {}
    try:
        value = max(1, int(tr.get("value", 7) or 7))
    except Exception:
        value = 7
    unit = str(tr.get("unit", "day")).lower()
    if unit == "week":
        return f"({start_expr} - {value * 7}d)"
    if unit == "month":
        return f"datetime_add('month', -{value}, {start_expr})"
    baseline_days = value if value >= 3 else 7
    return f"({start_expr} - {baseline_days}d)"


def _is_single_night_sleep(plan: Dict[str, Any]) -> bool:
    """A single-night sleep request ("last night", "today", "yesterday") — as
    opposed to a multi-day list/trend. These resolve to the most recent night so
    an empty current wake-day does not produce a false "no records"."""
    if bool(plan.get("comparison_requested", False)):
        return False
    tr = plan.get("time_range", {}) if isinstance(plan.get("time_range"), dict) else {}
    anchor = str(tr.get("anchor", "")).lower()
    if anchor == "absolute":
        return False
    if anchor in {"today", "yesterday", "day_before_yesterday"}:
        return True
    unit = str(tr.get("unit", "day")).lower()
    try:
        value = int(tr.get("value", 1) or 1)
    except Exception:
        value = 1
    return unit == "day" and value <= 1


def _build_monitoring_kql(user_id: str, question: str, plan: Dict[str, Any]) -> Optional[str]:
    metrics = _normalize_metrics(plan.get("metrics", []) if isinstance(plan.get("metrics"), list) else [])
    if not metrics:
        return None

    timezone = _timezone(plan)
    time_range = plan.get("time_range", _default_time_range("monitoring"))
    if not isinstance(time_range, dict):
        time_range = _default_time_range("monitoring")
    start, end = _kql_phone_window_for_plan(plan)
    start = _widen_start_for_comparison(plan, start)

    if is_conversation_history_request(question, plan):
        start_local, end_local = _kql_phone_local_time_window(time_range, timezone)
        return _conversation_history_query(user_id, start_local, end_local, timezone)

    if metrics and all(metric in PHONE_PROFILE_METRICS for metric in metrics):
        return _profile_query(user_id, metrics)

    # Cross-domain questions need every metric on a shared daily Period axis.
    # Resolve each semantic metric independently instead of letting the first
    # matching table branch silently drop the remaining families.
    if len(metrics) > 1:
        multi_metric_kql = _build_semantic_multi_metric_kql(user_id, metrics, start, end)
        if multi_metric_kql:
            return multi_metric_kql

    # Only catalog metrics that are genuinely optional/vendor-specific reach
    # this branch. Generic activity, sleep and sampled metrics are curated below.
    if _withings_metrics(metrics):
        withings_kql = _build_withings_monitoring_kql(user_id, question, plan)
        if withings_kql:
            return withings_kql

    if _is_workout_query(metrics):
        return _workout_sessions_query(user_id, start, end, metrics)

    if any(metric in PHONE_SLEEP_NIGHTS_METRICS for metric in metrics):
        try:
            nights_back = int(plan.get("sleep_nights_back", 0) or 0)
        except Exception:
            nights_back = 0
        return _phone_sleep_query(user_id, start, end, latest_only=_is_single_night_sleep(plan), nights_back=nights_back)

    cumulative_types = _metric_types(metrics, PHONE_CUMULATIVE_METRIC_TYPES)
    if cumulative_types:
        if _mentions_interval_level(question, plan):
            if any(
                token in (question or "").lower()
                for token in [
                    "freshness",
                    "latest ingestion",
                    "gap diagnostic",
                    "raw row",
                ]
            ):
                return _phone_flat_freshness_query(user_id, start, end, cumulative_types)
            return _phone_interval_mv_query(user_id, start, end, cumulative_types)
        if _mentions_hourly(question, plan):
            return _phone_hourly_mv_query(user_id, start, end, cumulative_types)
        value = time_range.get("value", 1)
        anchor = str(time_range.get("anchor", "")).lower()
        if isinstance(value, (int, float)) and int(value) <= 1 and anchor in {"today", "yesterday", "day_before_yesterday"}:
            return _phone_daily_mv_summary_query(user_id, start, end, cumulative_types)
        return _phone_daily_mv_query(user_id, start, end, cumulative_types)

    if _is_sampled_query(metrics):
        return _phone_sampled_query(
            user_id,
            start,
            end,
            metrics,
            question,
            latest_sample=bool(plan.get("latest_sample")),
        )

    return None


def _build_trend_kql(user_id: str, question: str, plan: Dict[str, Any]) -> Optional[str]:
    metrics = _normalize_metrics(plan.get("metrics", []) if isinstance(plan.get("metrics"), list) else [])
    if not metrics:
        return None
    time_range = plan.get("time_range", _default_time_range("trend"))
    if not isinstance(time_range, dict):
        time_range = _default_time_range("trend")
    start, end = _kql_phone_window_for_plan(plan)
    start = _widen_start_for_comparison(plan, start)
    if len(metrics) > 1:
        multi_metric_kql = _build_semantic_multi_metric_kql(user_id, metrics, start, end)
        if multi_metric_kql:
            return multi_metric_kql
    if _withings_metrics(metrics):
        withings_kql = _build_withings_trend_kql(user_id, question, plan)
        if withings_kql:
            return withings_kql
    if _is_workout_query(metrics):
        return _workout_sessions_query(user_id, start, end, metrics, trend=True)
    cumulative_types = _metric_types(metrics, PHONE_CUMULATIVE_METRIC_TYPES)
    if cumulative_types:
        if _mentions_interval_level(question, plan):
            if any(
                token in (question or "").lower()
                for token in [
                    "freshness",
                    "latest ingestion",
                    "gap diagnostic",
                    "raw row",
                ]
            ):
                return _phone_flat_freshness_query(user_id, start, end, cumulative_types)
            return _phone_interval_mv_query(user_id, start, end, cumulative_types)
        if _mentions_hourly(question, plan):
            return _phone_hourly_mv_query(user_id, start, end, cumulative_types)
        return _phone_daily_mv_query(user_id, start, end, cumulative_types)
    if any(metric in PHONE_SLEEP_NIGHTS_METRICS for metric in metrics):
        return _phone_sleep_query(user_id, start, end)
    if _is_sampled_query(metrics):
        # Trend/comparison semantics are already decided by the sanitized plan.
        # Do not let words such as "current" in a comparison question switch
        # this aggregate path back to a one-row latest-sample query.
        return _phone_sampled_query(user_id, start, end, metrics, "")
    return None


def _unavailable_metric_diagnostic_query(
    user_id: str,
    start: str,
    end: str,
    metrics: List[str],
    reason: str,
) -> str:
    """Return bounded metadata only; never substitute an unrelated value."""

    requested = _string_list(metrics or ["unspecified"])
    health_samples = physical_name(HEALTH_SAMPLES)
    return (
        f"{_bounded_function_call(HEALTH_SAMPLES, user_id, start, end)}"
        f" | where UserId == {_quote(user_id)}"
        f" | where ((isnotnull(DateFrom) and DateFrom >= {start} and DateFrom < {end}) "
        f"or (isnotnull(DateTo) and DateTo >= {start} and DateTo < {end}))"
        " | summarize AvailableTypes=make_set(Type, 100), "
        "LatestIngestionTimeUtc=max(IngestionTime), SourceRowCount=count()"
        f" | extend RequestedMetrics=pack_array({requested}), "
        f"CuratedValueAvailable=false, DiagnosticReason={_quote(reason)}, "
        f"SourceTable={_double_quote(health_samples + '-diagnostic')}"
    )


def _build_analysis_pack_kql(user_id: str, plan: Dict[str, Any]) -> str:
    metrics = _normalize_metrics(plan.get("metrics", []) if isinstance(plan.get("metrics"), list) else [])
    catalog_withings_metrics = _catalog_withings_metrics(metrics)
    timezone = _timezone(plan)
    time_range = plan.get("time_range", _default_time_range("monitoring"))
    if not isinstance(time_range, dict):
        time_range = _default_time_range("monitoring")
    start, end = _kql_phone_local_time_window(time_range, timezone)

    # Preserve every requested series when an interpretation/action plan mixes
    # core evidence with an optional vendor-only field. Ordinary all-core
    # analysis still uses the baseline pack below.
    if catalog_withings_metrics and len(metrics) > 1:
        mixed_metric_kql = _build_semantic_multi_metric_kql(
            user_id,
            metrics,
            start,
            end,
        )
        if mixed_metric_kql:
            return mixed_metric_kql

    if catalog_withings_metrics and _optional_withings_enabled():
        direct = _build_withings_trend_kql(user_id, "", plan)
        if direct:
            return direct

    if catalog_withings_metrics and not _optional_withings_enabled():
        return _unavailable_metric_diagnostic_query(
            user_id,
            start,
            end,
            catalog_withings_metrics,
            "optional_direct_withings_disabled",
        )
    baseline_start = f"{start} - 14d"
    baseline_end = start
    activity_daily = physical_name(ACTIVITY_DAILY)
    health_samples = physical_name(HEALTH_SAMPLES)
    return (
        f"let recent_steps={_bounded_function_call(ACTIVITY_DAILY, user_id, start, end)}"
        f" | where UserId == {_quote(user_id)}"
        " | where LocalDate >= "
        f"{start} and LocalDate < {end}"
        " | where Type == \"STEPS\""
        " | summarize recent_steps=sum(ValueSumExact), recent_step_days=dcount(LocalDate);\n"
        f"let baseline_step_daily={_bounded_function_call(ACTIVITY_DAILY, user_id, baseline_start, baseline_end)}"
        f" | where UserId == {_quote(user_id)}"
        f" | where LocalDate >= {baseline_start} and LocalDate < {baseline_end}"
        " | where Type == \"STEPS\""
        " | summarize Value=sum(ValueSumExact) by LocalDate;\n"
        "let baseline_steps=baseline_step_daily | summarize baseline_steps=avg(Value), baseline_step_days=dcount(LocalDate);\n"
        f"let recent_hr={_phone_sampled_base(user_id, start, end, ['HEART_RATE', 'RESTING_HEART_RATE'])}"
        " | summarize recent_hr_avg=avgif(todouble(ValueNumber), Type == \"HEART_RATE\"), recent_resting_hr_avg=avgif(todouble(ValueNumber), Type == \"RESTING_HEART_RATE\"), recent_hr_rows=count();\n"
        # recent_sleep: most recent main-sleep night (latest_only widens the
        # lookback so "why am I tired" still has last night's sleep even when the
        # current wake-day has no row yet in the early morning).
        f"let recent_sleep={_phone_sleep_query(user_id, start, end, latest_only=True)} | order by LocalDate desc;\n"
        "print "
        "recent_steps=tolong(round(todouble(toscalar(recent_steps | project recent_steps)),0)), "
        "recent_step_days=tolong(toscalar(recent_steps | project recent_step_days)), "
        "baseline_steps=todouble(toscalar(baseline_steps | project baseline_steps)), "
        "baseline_step_days=tolong(toscalar(baseline_steps | project baseline_step_days)), "
        "recent_hr_avg=todouble(toscalar(recent_hr | project recent_hr_avg)), "
        "recent_resting_hr_avg=todouble(toscalar(recent_hr | project recent_resting_hr_avg)), "
        "recent_hr_rows=tolong(toscalar(recent_hr | project recent_hr_rows)), "
        "phone_sleep_duration_hr=todouble(toscalar(recent_sleep | project phone_sleep_duration_hr)), "
        "sleep_start_local=todatetime(toscalar(recent_sleep | project SleepStartLocal)), "
        "sleep_end_local=todatetime(toscalar(recent_sleep | project SleepEndLocal)), "
        # In-bed span vs actual asleep time vs awake time, so the coach never
        # equates "in bed 02:24-07:10" with the asleep duration (3h39m).
        "sleep_asleep_hm=tostring(toscalar(recent_sleep | project AsleepHm)), "
        "sleep_in_bed_hm=tostring(toscalar(recent_sleep | project TimeInBedHm)), "
        "sleep_time_in_bed_min=tolong(toscalar(recent_sleep | project TimeInBedMin)), "
        "sleep_awake_min=tolong(toscalar(recent_sleep | project AwakeMin)), "
        "sleep_efficiency_pct=todouble(toscalar(recent_sleep | project SleepEfficiencyPct)), "
        "sleep_deep_pct=todouble(toscalar(recent_sleep | project DeepPct)), "
        "sleep_rem_pct=todouble(toscalar(recent_sleep | project RemPct)), "
        "sleep_has_stage_breakdown=tostring(toscalar(recent_sleep | project HasStageBreakdown)), "
        f"source_table={_double_quote(activity_daily + '+' + health_samples)}"
    )


def build_deterministic_query(
    user_id: str,
    question: str,
    plan: Dict[str, Any],
) -> Optional[str]:
    """Build the deterministic source-aware KQL query for a validated plan."""

    # Schema-v2 computations own their complete aggregation/filter/selection
    # semantics.  Compile them before any legacy metric dispatch and fail
    # closed if validation was not completed; falling through would change the
    # user's requested calculation.
    if plan.get("schema_version") == 2:
        if plan.get("execution_ready") is not True:
            return None
        return compile_computation_query(user_id, plan)

    normalized_metrics = _normalize_metrics(
        plan.get("metrics", []) if isinstance(plan.get("metrics"), list) else []
    )
    unsupported_metrics = [
        str(metric).strip()
        for metric in (
            plan.get("unsupported_metrics", [])
            if isinstance(plan.get("unsupported_metrics"), list)
            else []
        )
        if str(metric).strip()
    ][:10]
    if not normalized_metrics and unsupported_metrics:
        timezone = _timezone(plan)
        time_range = plan.get(
            "time_range",
            _default_time_range(str(plan.get("case_type") or "monitoring")),
        )
        if not isinstance(time_range, dict):
            time_range = _default_time_range("monitoring")
        start, end = _kql_phone_local_time_window(time_range, timezone)
        return _unavailable_metric_diagnostic_query(
            user_id,
            start,
            end,
            unsupported_metrics,
            "unsupported_or_unavailable_metric",
        )

    case_type = str(plan.get("case_type", "monitoring")).lower()
    if is_conversation_history_request(question, plan):
        timezone = _timezone(plan)
        time_range = plan.get("time_range", _default_time_range("monitoring"))
        if not isinstance(time_range, dict):
            time_range = _default_time_range("monitoring")
        start_local, end_local = _kql_phone_local_time_window(time_range, timezone)
        return _conversation_history_query(user_id, start_local, end_local, timezone)
    if case_type in {"monitoring", "personal_data_supported"}:
        if bool(plan.get("comparison_requested", False)):
            return _build_trend_kql(user_id, question, plan)
        if bool(plan.get("latest_sample")):
            return _build_monitoring_kql(user_id, question, plan)
        time_range = plan.get("time_range", {}) if isinstance(plan.get("time_range"), dict) else {}
        if int(time_range.get("value", 1) or 1) >= 7:
            return _build_trend_kql(user_id, question, plan)
        return _build_monitoring_kql(user_id, question, plan)
    if case_type == "trend":
        return _build_trend_kql(user_id, question, plan)
    return _build_analysis_pack_kql(user_id, plan)


def build_fallback_query(user_id: str, plan: Optional[Dict[str, Any]] = None) -> str:
    """Build a bounded fallback without cross-source aggregate substitution."""

    safe_plan = plan if isinstance(plan, dict) else {}
    metrics = _normalize_metrics(safe_plan.get("metrics", []) if isinstance(safe_plan.get("metrics"), list) else [])
    unsupported_metrics = [
        str(metric).strip()
        for metric in (
            safe_plan.get("unsupported_metrics", [])
            if isinstance(safe_plan.get("unsupported_metrics"), list)
            else []
        )
        if str(metric).strip()
    ][:10]
    timezone = _timezone(safe_plan)
    time_range = safe_plan.get("time_range", _default_time_range("monitoring"))
    if not isinstance(time_range, dict):
        time_range = _default_time_range("monitoring")
    start, end = _kql_phone_local_time_window(time_range, timezone)
    catalog_withings_metrics = _catalog_withings_metrics(metrics)
    if catalog_withings_metrics and not _optional_withings_enabled():
        return _unavailable_metric_diagnostic_query(
            user_id,
            start,
            end,
            catalog_withings_metrics,
            "optional_direct_withings_disabled",
        )
    if len(metrics) > 1:
        multi_metric_kql = _build_semantic_multi_metric_kql(user_id, metrics, start, end)
        if multi_metric_kql:
            return multi_metric_kql
    phone_metrics: List[str] = []
    for metric in metrics:
        mapped = WITHINGS_TO_PHONE_METRIC_PRIORITY.get(
            metric,
            "phone_sleep_duration" if metric in WITHINGS_PHONE_SLEEP_FALLBACK_METRICS else metric,
        )
        if _is_curated_metric(mapped) and mapped not in phone_metrics:
            phone_metrics.append(mapped)

    if any(metric in PHONE_SLEEP_NIGHTS_METRICS for metric in phone_metrics):
        return _phone_sleep_query(user_id, start, end, latest_only=_is_single_night_sleep(safe_plan))

    cumulative_types = _metric_types(phone_metrics, PHONE_CUMULATIVE_METRIC_TYPES)
    if cumulative_types:
        return _phone_daily_mv_query(user_id, start, end, cumulative_types)
    if "phone_workout_count" in phone_metrics:
        workout_sessions = physical_name(WORKOUT_SESSIONS)
        return (
            f"{_bounded_function_call(WORKOUT_SESSIONS, user_id, start, end)}"
            f" | where UserId == {_quote(user_id)}"
            f" | where StartLocal >= {start} and StartLocal < {end}"
            f" | summarize phone_workout_count=count(), RowCount=count(), SourceTable={_double_quote(workout_sessions)}"
            " | extend phone_workout_count_source=iif(RowCount > 0, \"smartphone\", \"none\")"
        )
    if phone_metrics and all(metric in PHONE_PROFILE_METRICS for metric in phone_metrics):
        return _profile_query(user_id, phone_metrics)
    if _metric_types(phone_metrics, PHONE_SAMPLED_METRIC_TYPES):
        return _phone_sampled_query(
            user_id,
            start,
            end,
            phone_metrics,
            "",
            latest_sample=bool(safe_plan.get("latest_sample")),
        )

    # Metrics with no compatible Phone representation (for example Withings
    # respiratory detail) retain a bounded Withings-only fallback instead of
    # silently answering with unrelated heart-rate data.
    withings_metrics = _withings_metrics(metrics)
    if withings_metrics:
        table = _primary_withings_table(withings_metrics)
        table_metrics = [m for m in withings_metrics if _metric_resource(m) == table]
        withings_kql = _withings_standard_summary_query(user_id, table, start, end, table_metrics)
        if withings_kql:
            return withings_kql
    # Do not answer an unsupported metric with an unrelated heart-rate value.
    # This bounded diagnostic lets the response distinguish "no curated value"
    # from a total ingestion gap without aggregating mirrored source rows.
    return _unavailable_metric_diagnostic_query(
        user_id,
        start,
        end,
        metrics or unsupported_metrics,
        "unsupported_or_unavailable_metric",
    )
