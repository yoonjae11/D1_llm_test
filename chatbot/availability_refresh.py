"""Metric availability refresh for the unified HealthDataFlat data layer.

Direct Withings tables are an opt-in compatibility source for deployments that
still expose vendor-only fields. They are not scanned or used as the fallback
for generic health metrics by default.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Dict, List

from chatbot.services.adx import ADXQueryPort, default_adx_service
from chatbot.services.memory import OperationalStoragePort, create_operational_storage
from chatbot.operational_storage import MetricAvailability
from chatbot.policies.availability import (
    enabled_availability_items,
    optional_withings_tables_enabled,
    source_table_availability_key,
)
from chatbot.policies.domain_catalog import (
    HEALTH_DATA_FLAT_DEDUP_KEYS,
    PHONE_SLEEP_NIGHTS_AVAILABILITY_TYPES,
)
from chatbot.policies.semantic_resources import (
    ACTIVITY_DAILY,
    HEALTH_SAMPLES,
    SLEEP_NIGHTLY,
    WITHINGS_ACTIVITY,
    WITHINGS_INTRADAY,
    WITHINGS_NIGHT_EVENTS,
    WITHINGS_RESOURCE_IDS,
    WITHINGS_SLEEP,
    WITHINGS_SLEEP_DETAIL,
    WITHINGS_WORKOUTS,
    logical_name,
    physical_name,
    tabular_call,
)
from queue_contracts import METRIC_AVAILABILITY_QUEUE
from azure_storage_resources import create_queue_client

logger = logging.getLogger(__name__)

adx_manager: ADXQueryPort = default_adx_service()


def OperationalTableRepository(
    connection_string: str | None = None,
    *,
    fail_soft: bool = True,
) -> OperationalStoragePort:
    """Compatibility constructor backed by the operational-storage factory."""

    return create_operational_storage(
        connection_string=connection_string,
        fail_soft=fail_soft,
    )


CUMULATIVE_TYPES = {
    "STEPS",
    "DISTANCE_WALKING_RUNNING",
    "DISTANCE_DELTA",
    "FLIGHTS_CLIMBED",
    "ELEVATION_GAINED",
    "ACTIVE_ENERGY_BURNED",
    "BASAL_ENERGY_BURNED",
    "TOTAL_CALORIES_BURNED",
    "EXERCISE_TIME",
    "APPLE_MOVE_TIME",
    "APPLE_STAND_TIME",
    "ACTIVE_ZONE_MINUTES",
    "FATBURN_ACTIVE_ZONE_MINUTES",
    "CARDIO_ACTIVE_ZONE_MINUTES",
    "PEAK_ACTIVE_ZONE_MINUTES",
}

_IOS_PHONE_ONLY_CUMULATIVE_TYPES = {
    "STEPS",
    "DISTANCE_WALKING_RUNNING",
    "DISTANCE_DELTA",
    "FLIGHTS_CLIMBED",
    "ELEVATION_GAINED",
}

_SLEEP_TYPES = frozenset(
    metric_type
    for types in PHONE_SLEEP_NIGHTS_AVAILABILITY_TYPES.values()
    for metric_type in types
)

_ACTIVITY_SOURCE_POLICY_KQL = (
    "(SourcePlatform == 'googleHealthConnect' and "
    "((Type == 'STEPS' and (SourceName == 'android' or "
    "SourceName startswith 'com.android.healthconnect.phone.')) or "
    "(Type != 'STEPS' and (SourceName == 'com.sec.android.app.shealth' or "
    "SourceName startswith 'com.android.healthconnect.phone.')))) or "
    "(SourcePlatform == 'appleHealth' and SourceId startswith 'com.apple.health.' and "
    "((Type in ('STEPS','DISTANCE_DELTA','DISTANCE_WALKING_RUNNING',"
    "'FLIGHTS_CLIMBED','ELEVATION_GAINED') and SourceName !contains 'Watch') or "
    "Type in ('ACTIVE_ENERGY_BURNED','BASAL_ENERGY_BURNED',"
    "'TOTAL_CALORIES_BURNED','EXERCISE_TIME','APPLE_MOVE_TIME',"
    "'APPLE_STAND_TIME')))"
)

# Table-specific timestamps and natural keys are schema facts, not source
# selection policy.  The canonical metric-to-table relationship remains solely
# in policies.domain_catalog.
_WITHINGS_AVAILABILITY_SPECS = (
    (WITHINGS_ACTIVITY, "", "DateLocal", "DateLocal", "DateLocal"),
    (
        WITHINGS_SLEEP,
        " | extend SleepStartKey=bin(SleepStartLocal, 1m), SleepEndKey=bin(SleepEndLocal, 1m)",
        "SleepStartKey, SleepEndKey",
        "SleepStartLocal",
        "SleepEndLocal",
    ),
    (WITHINGS_INTRADAY, "", "TimestampLocal", "TimestampLocal", "TimestampLocal"),
    (WITHINGS_SLEEP_DETAIL, "", "SampleEpoch", "SampleLocal", "SampleLocal"),
    (
        WITHINGS_WORKOUTS,
        "",
        "StartLocal, EndLocal, WithingsId",
        "StartLocal",
        "EndLocal",
    ),
    (
        WITHINGS_NIGHT_EVENTS,
        "",
        "WithingsId, EventCode, PairIndex",
        "StartLocal",
        "EndLocal_Heuristic",
    ),
)

# A metric counts as "available" only if it has been collected/synced within this
# many days. A type that stopped syncing more than this is downgraded to
# is_available=False so "What data do I have?" reflects what is CURRENTLY tracked,
# not anything ever seen in the long history window. (Override via env.)
def _recent_days() -> int:
    import os
    try:
        return max(1, int(os.environ.get("CHATBOT_AVAILABILITY_RECENT_DAYS", "4")))
    except Exception:
        return 4


def _quote(value: str) -> str:
    return "'" + str(value or "").replace("'", "''") + "'"


def _unified_availability_branch() -> str:
    health_samples = physical_name(HEALTH_SAMPLES)
    cumulative_types = ",".join(
        _quote(metric_type) for metric_type in sorted(CUMULATIVE_TYPES)
    )
    return (
        f"({tabular_call(HEALTH_SAMPLES, 'TargetUserId', 'BackfillStartLocal', 'RefreshUtc')}"
        " | where UserId == TargetUserId"
        " | where isnotempty(Type)"
        " | where isnotnull(IngestionTime)"
        " | where IngestionTime <= RefreshUtc"
        f" | where Type !in ({cumulative_types}) or "
        f"(({_ACTIVITY_SOURCE_POLICY_KQL}) and isnotnull(todouble(ValueNumber)) "
        "and todouble(ValueNumber) >= 0 and DateTo > DateFrom and DateTo - DateFrom < 20h)"
        " | extend IngestedAt=IngestionTime"
        f" | summarize arg_max(IngestedAt, *) by {HEALTH_DATA_FLAT_DEDUP_KEYS}"
        " | summarize EarliestDateFromLocal=min(DateFrom), LatestDateFromLocal=max(DateFrom), LatestDateToLocal=max(DateTo), LatestIngestionTimeUtc=max(IngestedAt), FlatRowCount=count() by Type, SourcePlatform, SourceName, SourceId, Unit"
        " | project Type=toupper(tostring(Type)), SourcePlatform=tostring(SourcePlatform), SourceName=tostring(SourceName), SourceId=tostring(SourceId), Unit=tostring(Unit), EarliestDateFromLocal, LatestDateFromLocal, LatestDateToLocal, LatestIngestionTimeUtc, FlatRowCount=tolong(FlatRowCount), "
        f"SourceTable={_quote(health_samples)})"
    )


def _withings_availability_branch(
    resource_id: str,
    prepare_dedup: str,
    dedup_keys: str,
    date_from_column: str,
    date_to_column: str,
) -> str:
    table = physical_name(resource_id)
    availability_key = source_table_availability_key(table)
    return (
        f"({table}"
        " | where AppUserId == TargetAppUserId"
        " | where isnotnull(IngestedAt)"
        " | where IngestedAt >= FreshnessStartUtc and IngestedAt <= RefreshUtc"
        f"{prepare_dedup}"
        f" | summarize arg_max(IngestedAt, *) by {dedup_keys}"
        f" | summarize LatestDateFromLocal=max({date_from_column}), LatestDateToLocal=max({date_to_column}), LatestIngestionTimeUtc=max(IngestedAt), FlatRowCount=count()"
        f" | project Type={_quote(availability_key)}, SourcePlatform=\"withings\", SourceName={_quote(table)}, Unit=\"\", LatestDateFromLocal, LatestDateToLocal, LatestIngestionTimeUtc, FlatRowCount=tolong(FlatRowCount), SourceTable={_quote(table)})"
    )


def build_metric_availability_kql(app_user_id: str) -> str:
    """Build a bounded inventory query for unified and explicitly enabled data."""

    branches = [_unified_availability_branch()]
    if optional_withings_tables_enabled():
        branches.extend(
            _withings_availability_branch(*spec)
            for spec in _WITHINGS_AVAILABILITY_SPECS
        )
    return (
        f"let TargetUserId={_quote(app_user_id)};\n"
        f"let TargetAppUserId={_quote(app_user_id)};\n"
        "let RefreshUtc=now();\n"
        # The canonical history scan supplies the actual production
        # all-available boundary and remains capped by the same 3,660-day guard
        # used by conversational queries. Freshness is a separate 180-day
        # ingestion concern and must never redefine the historical minimum.
        "let BackfillStartLocal=RefreshUtc - 3660d;\n"
        "let FreshnessStartUtc=RefreshUtc - 180d;\n"
        "union isfuzzy=true\n  "
        + ",\n  ".join(branches)
    )


def _freshness_status(latest_ingestion: Any, now_utc: dt.datetime) -> str:
    value = latest_ingestion
    if isinstance(value, str):
        try:
            value = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except Exception:
            return "unknown"
    if not isinstance(value, dt.datetime):
        return "missing"
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    age = now_utc - value.astimezone(dt.timezone.utc)
    if age <= dt.timedelta(days=2):
        return "fresh"
    if age <= dt.timedelta(days=14):
        return "stale"
    return "stale"


def _parse_iso(value: Any) -> dt.datetime | None:
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)
    except Exception:
        return None


def _preferred_table(metric_type: str, source_table: str = "") -> str:
    if source_table:
        try:
            if logical_name(source_table) in WITHINGS_RESOURCE_IDS:
                return source_table
        except KeyError:
            pass
    metric = metric_type.upper()
    if metric in CUMULATIVE_TYPES:
        return physical_name(ACTIVITY_DAILY)
    if metric in _SLEEP_TYPES:
        return physical_name(SLEEP_NIGHTLY)
    return physical_name(HEALTH_SAMPLES)


def enqueue_metric_availability_refresh(app_user_id: str) -> bool:
    """Fire-and-forget enqueue of a per-user availability refresh.

    Called when the user sends /start or /new so their inventory is re-checked
    promptly (in addition to the once-daily ChatbotMetricAvailabilityRefreshTimer).
    Best-effort: never raises into the Telegram webhook path.
    """
    import json
    import os
    if not app_user_id:
        return False
    try:
        connection_string = os.environ.get("AzureWebJobsStorage", "")
        if not connection_string:
            return False
        qc = create_queue_client(connection_string, METRIC_AVAILABILITY_QUEUE)
        qc.send_message(json.dumps({"appUserId": app_user_id}, ensure_ascii=False))
        return True
    except Exception:
        logger.warning("enqueue_metric_availability_refresh failed app_user_id=%s", app_user_id, exc_info=True)
        return False


def _source_policy(
    metric_type: str,
    source_platform: str,
    source_name: str,
    source_id: str = "",
) -> bool:
    """Mirror the activity-source policy used by PhoneCumulativeIntervalDedupMV."""

    metric = metric_type.upper()
    if metric not in CUMULATIVE_TYPES:
        return True

    platform = str(source_platform or "").lower()
    source = str(source_name or "")
    source_lower = source.lower()
    source_id_lower = str(source_id or "").lower()
    hc_phone = source_lower.startswith("com.android.healthconnect.phone.")

    if platform == "googlehealthconnect":
        if metric == "STEPS":
            return source_lower == "android" or hc_phone
        return source_lower == "com.sec.android.app.shealth" or hc_phone

    if platform == "applehealth":
        if not source_id_lower.startswith("com.apple.health."):
            return False
        if metric in _IOS_PHONE_ONLY_CUMULATIVE_TYPES:
            return "watch" not in source_lower
        return True

    return False


def _merge_rows(app_user_id: str, rows: List[Dict[str, Any]]) -> List[MetricAvailability]:
    grouped: Dict[str, Dict[str, Any]] = {}
    now_utc = dt.datetime.now(dt.timezone.utc)
    include_optional_withings = optional_withings_tables_enabled()
    withings_tables = frozenset(
        physical_name(resource_id) for resource_id in WITHINGS_RESOURCE_IDS
    )
    for row in rows:
        metric_type = str(row.get("Type") or "").upper()
        if not metric_type:
            continue
        source_table = str(row.get("SourceTable") or "").strip()
        if not source_table:
            source_table = next(
                (
                    table
                    for table in withings_tables
                    if metric_type == source_table_availability_key(table).upper()
                ),
                "",
            )
        is_withings = source_table in withings_tables
        if is_withings and not include_optional_withings:
            continue
        if not is_withings and not _source_policy(
            metric_type,
            str(row.get("SourcePlatform") or ""),
            str(row.get("SourceName") or ""),
            str(row.get("SourceId") or ""),
        ):
            continue
        current = grouped.setdefault(
            metric_type,
            {"FlatRowCount": 0, "SourceTable": source_table},
        )
        current["FlatRowCount"] = int(current.get("FlatRowCount") or 0) + int(row.get("FlatRowCount") or 0)
        if not current.get("SourceTable") and source_table:
            current["SourceTable"] = source_table
        earliest = row.get("EarliestDateFromLocal")
        if earliest is not None and (
            current.get("EarliestDateFromLocal") is None
            or str(earliest) < str(current.get("EarliestDateFromLocal"))
        ):
            current["EarliestDateFromLocal"] = earliest
        for key in ("LatestDateFromLocal", "LatestDateToLocal", "LatestIngestionTimeUtc"):
            value = row.get(key)
            if value is not None and (current.get(key) is None or str(value) > str(current.get(key))):
                current[key] = value

    recent_days = _recent_days()
    out: List[MetricAvailability] = []
    for metric_type, row in grouped.items():
        source_table = str(row.get("SourceTable") or "")
        is_withings = source_table in withings_tables
        latest_collected = row.get("LatestDateToLocal") or row.get("LatestDateFromLocal")
        # Recency gate: only mark available if collected/synced within recent_days.
        # Use ingestion time (UTC) as the recency signal since data lands ~hourly.
        latest_ingestion = _parse_iso(row.get("LatestIngestionTimeUtc"))
        is_recent = bool(
            latest_ingestion is not None
            and (now_utc - latest_ingestion.astimezone(dt.timezone.utc)) <= dt.timedelta(days=recent_days)
        )
        out.append(
            MetricAvailability(
                app_user_id=app_user_id,
                metric_type=metric_type,
                is_available=(int(row.get("FlatRowCount") or 0) > 0) and is_recent,
                preferred_source_table=_preferred_table(metric_type, source_table),
                preferred_source_policy=(
                    "explicit_optional_withings"
                    if is_withings
                    else (
                        "curated_activity_mv_policy"
                        if metric_type in CUMULATIVE_TYPES
                        else (
                            "curated_sleep_function"
                            if metric_type in _SLEEP_TYPES
                            else "unified_flat_policy"
                        )
                    )
                ),
                earliest_date_from_local=row.get("EarliestDateFromLocal"),
                latest_date_from_local=row.get("LatestDateFromLocal"),
                latest_date_to_local=row.get("LatestDateToLocal"),
                latest_ingestion_time_utc=row.get("LatestIngestionTimeUtc"),
                last_collected_time_local=latest_collected,
                freshness_status=_freshness_status(row.get("LatestIngestionTimeUtc"), now_utc),
            )
        )

    # Upsert disabled/missing optional-source markers so a value from an older
    # Withings-primary refresh cannot remain advertised indefinitely.
    grouped_metric_types = set(grouped)
    for table in sorted(withings_tables):
        availability_key = source_table_availability_key(table).upper()
        if availability_key in grouped_metric_types:
            continue
        out.append(
            MetricAvailability(
                app_user_id=app_user_id,
                metric_type=availability_key,
                is_available=False,
                preferred_source_table=table,
                preferred_source_policy=(
                    "explicit_optional_withings"
                    if include_optional_withings
                    else "optional_withings_disabled"
                ),
                freshness_status="missing",
            )
        )
    return out


def refresh_and_context(app_user_id: str, repository: OperationalStoragePort | None = None) -> Dict[str, Any]:
    """Refresh availability from ADX, then return it in the availability_context
    shape used by the graph ({metric: {is_available, latest_date_to_local, ...}}).

    Used for the on-demand refresh when the user asks "What data do I have?" so the
    listed metrics reflect the very latest sync, not the last periodic refresh."""
    repository = repository or OperationalTableRepository(fail_soft=True)
    refresh_metric_availability(app_user_id, repository=repository)
    items = enabled_availability_items(
        repository.get_metric_availability_map(app_user_id)
    )
    return {
        metric: {
            "is_available": item.is_available,
            "preferred_source_table": item.preferred_source_table,
            "earliest_date_from_local": item.earliest_date_from_local,
            "latest_date_to_local": item.latest_date_to_local,
            "latest_ingestion_time_utc": item.latest_ingestion_time_utc,
            "refreshed_at_utc": item.refreshed_at_utc,
        }
        for metric, item in items.items()
    }


def refresh_metric_availability(app_user_id: str, repository: OperationalStoragePort | None = None) -> Dict[str, Any]:
    repository = repository or OperationalTableRepository(fail_soft=False)
    query = build_metric_availability_kql(app_user_id)
    result = adx_manager.execute_query(query, suppress_error_log=True)
    if isinstance(result, str) and result.lower().startswith("error"):
        raise RuntimeError(result)
    rows = result if isinstance(result, list) else []
    availability = _merge_rows(app_user_id, rows)
    saved = repository.save_metric_availability_rows(app_user_id, availability)
    logger.info("metric availability refreshed app_user_id=%s rows=%s saved=%s", app_user_id, len(rows), saved)
    return {
        "appUserId": app_user_id,
        "adxRows": len(rows),
        "saved": saved,
        "metricTypes": [item.metric_type for item in availability],
    }
