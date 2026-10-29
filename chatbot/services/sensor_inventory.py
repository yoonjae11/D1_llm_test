"""Observe the shape and coverage of a bounded user's Sensor data.

This is discovery metadata for query planning, not answer evidence: no sensor
measurement values, expected answers, or backend identifiers leave this service.
An optional bounded workout probe inspects payloads locally and returns only
field paths/types/counts and activity/unit labels to the query model.
The same canonical functions and safety gate are used for every dataset.
"""

from __future__ import annotations

import json
import math
import re
from datetime import date, datetime
from typing import Any, Callable

from chatbot.policies.kql_safety import inspect_query_safety
from chatbot.policies.semantic_resources import (
    ACTIVITY_DAILY,
    HEALTH_SAMPLES,
    SLEEP_NIGHTLY,
    WORKOUT_SESSIONS,
    tabular_call,
)


_OBSERVATION_LIMIT = 256
_CODE_LIMIT = 50
_PAYLOAD_SAMPLE_LIMIT = 64
_PAYLOAD_FIELD_LIMIT = 256
_PAYLOAD_DEPTH_LIMIT = 5
_PAYLOAD_ARRAY_LIMIT = 8
_RESOURCES = (HEALTH_SAMPLES, ACTIVITY_DAILY, SLEEP_NIGHTLY, WORKOUT_SESSIONS)
_COUNT_FIELDS = (
    "RowCount", "NumericRows", "AsleepMinRows", "TimeInBedMinRows",
    "ActivityCodeRows", "DurationMinRows", "AverageHeartRateBpmRows",
    "ElevationGainRows", "DistanceRows", "CaloriesKcalRows", "StepsRows",
    "ActiveZoneMinutesRows", "SpeedRows",
    "DeepMinRows", "RemMinRows", "LightMinRows", "AwakeMinRows",
    "DeepPctRows", "RemPctRows", "LightPctRows", "AwakePctRows",
    "SleepEfficiencyPctRows", "ActivityNameRows",
    "HasAsleepTrueRows", "HasAsleepFalseRows", "HasAsleepNullRows",
    "HasStageBreakdownRows",
    "IntervalDurationMatchRows", "IntervalDurationMismatchRows",
    "RepeatedWindowRows", "DistinctRecordSameWindowRows", "MultiSourceSameWindowRows",
)


def _local_bound(value: Any) -> tuple[datetime, str]:
    """Accept finite local ISO timestamps, never arbitrary Kusto expressions."""

    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, datetime.min.time())
    else:
        text = str(value or "").strip()
        wrapper = re.fullmatch(r"datetime\(([^()]*)\)", text, flags=re.IGNORECASE)
        if wrapper:
            text = wrapper.group(1).strip()
        parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is not None:
        raise ValueError("Sensor inventory requires local, timezone-naive bounds.")
    # The shared guard recognizes Kusto's space-separated local timestamp;
    # an unquoted ISO 'T' would otherwise look like an unknown identifier.
    return parsed, f"datetime({parsed.isoformat(sep=' ')})"


def _query(user_id: str, start_literal: str, end_literal: str, resource: str | None = None) -> str:
    calls = {
        resource: tabular_call(resource, "TargetUserId", "StartLocal", "EndLocal")
        for resource in _RESOURCES
    }
    branches = {
        HEALTH_SAMPLES: f"""{calls[HEALTH_SAMPLES]}
     | where UserId == TargetUserId
     | where ((DateFrom >= StartLocal and DateFrom < EndLocal)
         or (DateFrom < EndLocal and DateTo > StartLocal))
     | summarize Copies=count(), NumericCopies=countif(isnotnull(ValueNumber)),
         RecordIdentities=dcount(strcat(Uuid, "|", SourcePlatform, "|", SourceId, "|", SourceName, "|", SourceDeviceId)),
         SourceIdentities=dcount(strcat(SourcePlatform, "|", SourceId, "|", SourceName, "|", SourceDeviceId)),
         DurationMatchCopies=countif(Type startswith "SLEEP_" and Unit == "MINUTE"
             and isnotnull(ValueNumber) and isnotnull(DateTo)
             and abs(todouble(ValueNumber) - datetime_diff('second', DateTo, DateFrom) / 60.0) <= 1.0),
         DurationMismatchCopies=countif(Type startswith "SLEEP_" and Unit == "MINUTE"
             and isnotnull(ValueNumber) and isnotnull(DateTo)
             and abs(todouble(ValueNumber) - datetime_diff('second', DateTo, DateFrom) / 60.0) > 1.0)
         by Type, Unit, DateFrom, DateTo
     | summarize RowCount=sum(Copies), NumericRows=sum(NumericCopies),
         IntervalDurationMatchRows=sum(DurationMatchCopies),
         IntervalDurationMismatchRows=sum(DurationMismatchCopies),
         RepeatedWindowRows=sumif(Copies, Copies > 1),
         DistinctRecordSameWindowRows=sumif(Copies, RecordIdentities > 1),
         MultiSourceSameWindowRows=sumif(Copies, SourceIdentities > 1),
         FirstEventLocal=min(DateFrom), LastEventLocal=max(DateFrom) by Type, Unit
     | extend Resource="{HEALTH_SAMPLES}"
""",
        ACTIVITY_DAILY: f"""{calls[ACTIVITY_DAILY]}
     | where UserId == TargetUserId and LocalDate >= startofday(StartLocal) and LocalDate < EndLocal
     | summarize RowCount=count(), NumericRows=countif(isnotnull(ValueSumExact)),
         FirstEventLocal=min(LocalDate), LastEventLocal=max(LocalDate) by Type, Unit
     | extend Resource="{ACTIVITY_DAILY}"
""",
        SLEEP_NIGHTLY: f"""{calls[SLEEP_NIGHTLY]}
     | where UserId == TargetUserId and SleepDay >= startofday(StartLocal) and SleepDay < EndLocal
     | summarize RowCount=count(), AsleepMinRows=countif(HasAsleepDuration == true and isnotnull(AsleepMin)),
         TimeInBedMinRows=countif(isnotnull(TimeInBedMin)),
         DeepMinRows=countif(HasStageBreakdown == true and isnotnull(DeepMin)),
         RemMinRows=countif(HasStageBreakdown == true and isnotnull(RemMin)),
         LightMinRows=countif(HasStageBreakdown == true and isnotnull(LightMin)),
         AwakeMinRows=countif(HasStageBreakdown == true and isnotnull(AwakeMin)),
         DeepPctRows=countif(HasStageBreakdown == true and isnotnull(DeepPct)),
         RemPctRows=countif(HasStageBreakdown == true and isnotnull(RemPct)),
         LightPctRows=countif(HasStageBreakdown == true and isnotnull(LightPct)),
         AwakePctRows=countif(HasStageBreakdown == true and isnotnull(AwakePct)),
         SleepEfficiencyPctRows=countif(HasAsleepDuration == true and isnotnull(SleepEfficiencyPct)),
         HasStageBreakdownRows=countif(HasStageBreakdown == true),
         HasAsleepTrueRows=countif(HasAsleepDuration == true),
         HasAsleepFalseRows=countif(HasAsleepDuration == false),
         HasAsleepNullRows=countif(isnull(HasAsleepDuration))
     | extend Resource="{SLEEP_NIGHTLY}"
""",
        WORKOUT_SESSIONS: f"""{calls[WORKOUT_SESSIONS]}
     | project-rename WorkoutStartLocal=StartLocal
     | where UserId == TargetUserId and WorkoutStartLocal >= StartLocal and WorkoutStartLocal < EndLocal
     | summarize RowCount=count(), ActivityCodes=make_set(ActivityCode, {_CODE_LIMIT}),
         ActivityNames=make_set(ActivityName, {_CODE_LIMIT}),
         ActivityNameRows=countif(isnotempty(ActivityName)),
         ActivityCodeRows=countif(isnotempty(ActivityCode)),
         DurationMinRows=countif(isnotnull(DurationMin)),
         AverageHeartRateBpmRows=countif(isnotnull(AverageHeartRateBpm)),
         ElevationGainRows=countif(isnotnull(ElevationGain)),
         DistanceRows=countif(isnotnull(Distance)),
         CaloriesKcalRows=countif(isnotnull(CaloriesKcal)),
         StepsRows=countif(isnotnull(Steps)),
         ActiveZoneMinutesRows=countif(isnotnull(ActiveZoneMinutes)),
         SpeedRows=countif(isnotnull(Speed))
     | extend Resource="{WORKOUT_SESSIONS}"
""",
    }
    body = branches[resource] if resource else "union\n" + ",\n".join(
        f"({branch})" for branch in branches.values()
    )
    return f"""let TargetUserId = {json.dumps(user_id, ensure_ascii=False)};
let StartLocal = {start_literal};
let EndLocal = {end_literal};
{body}
| take {_OBSERVATION_LIMIT}"""


def _observations(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Whitelist metadata fields even if an executor returns extra columns."""

    observations: list[dict[str, Any]] = []
    for row in rows[:_OBSERVATION_LIMIT]:
        resource = str(row.get("Resource") or "")
        if resource not in _RESOURCES:
            continue
        observation: dict[str, Any] = {"resource": resource}
        for name in ("Type", "Unit"):
            if isinstance(row.get(name), str) and row[name]:
                observation[name] = row[name][:100]
        for name in _COUNT_FIELDS:
            value = row.get(name)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                observation[name] = value
        for name in ("FirstEventLocal", "LastEventLocal"):
            value = row.get(name)
            if value is None:
                continue
            try:
                if isinstance(value, (datetime, date)):
                    timestamp = value.isoformat()
                else:
                    timestamp = datetime.fromisoformat(
                        str(value).replace("Z", "+00:00")
                    ).isoformat()
                observation[name] = timestamp
            except (TypeError, ValueError):
                continue
        for name in ("ActivityCodes", "ActivityNames"):
            categories = row.get(name)
            if isinstance(categories, str):
                try:
                    categories = json.loads(categories)
                except (TypeError, ValueError):
                    categories = None
            if isinstance(categories, list):
                observation[name] = [
                    category[:100] for category in categories[:_CODE_LIMIT]
                    if isinstance(category, str) and category
                ]
        # A valid count distinguishes measured absence from a malformed row.
        if "RowCount" in observation:
            observations.append(observation)
    return observations


def observe_sensor_schema(
    user_id: str,
    start_local: Any,
    end_local: Any,
    execute_query: Callable[[str], Any],
    *,
    include_workout_payload: bool = False,
) -> dict[str, Any]:
    """Return observed coverage metadata for an inclusive/exclusive window.

    Errors mean unavailable discovery, not zero sensor records. The service
    never widens the supplied window or derives a user's evaluation clock.
    """

    unavailable: dict[str, Any] = {"status": "unavailable", "observations": []}
    try:
        user = str(user_id or "").strip()
        start, start_literal = _local_bound(start_local)
        end, end_literal = _local_bound(end_local)
        if not user or start >= end:
            return {**unavailable, "reason": "invalid_scope"}
        query = _query(user, start_literal, end_literal)
        guard = inspect_query_safety(query, user)
        if not guard.get("is_safe"):
            return {**unavailable, "reason": "guard_rejected"}
    except (TypeError, ValueError):
        return {**unavailable, "reason": "invalid_scope"}

    def fetch(candidate: str) -> list[dict[str, Any]] | None:
        # Isolated retries have exactly the same user/window and safety gate.
        if not inspect_query_safety(candidate, user).get("is_safe"):
            return None
        try:
            rows = execute_query(candidate)
        except Exception:
            return None
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            return None
        if rows and not _observations(rows):
            return None
        return rows

    result = fetch(query)
    observations: list[dict[str, Any]] = []
    resource_status: dict[str, str] = {}
    limit_reached = False

    def record(resource: str, rows: list[dict[str, Any]] | None) -> None:
        nonlocal limit_reached
        if rows is None:
            resource_status[resource] = "unavailable"
            return
        truncated = len(rows) >= _OBSERVATION_LIMIT
        limit_reached |= truncated
        entries = [item for item in _observations(rows) if item["resource"] == resource]
        observations.extend(entries)
        # Truncation must not be mistaken for complete absence of a Type.
        resource_status[resource] = (
            "unknown" if truncated else
            "observed" if any(item["RowCount"] > 0 for item in entries) else "empty"
        )

    if result is not None:
        for resource in _RESOURCES:
            record(resource, result)
    else:
        # One missing/broken materialized view must not hide healthy flat data.
        # Keep the single-query fast path; isolate probes only after failure.
        for resource in _RESOURCES:
            record(resource, fetch(_query(user, start_literal, end_literal, resource)))
    failures = sum(status == "unavailable" for status in resource_status.values())
    result = {
        "status": "unavailable" if failures == len(_RESOURCES) else "partial" if failures else "observed",
        "window": {"start_local": start.isoformat(), "end_local_exclusive": end.isoformat()},
        "observations": observations,
        "resource_status": resource_status,
        "metadata_limit_reached": limit_reached,
    }
    if include_workout_payload:
        result["workout_payload"] = observe_workout_payload_schema(
            user, start, end, execute_query,
        )
    return result


def _payload_query(user_id: str, start_literal: str, end_literal: str) -> str:
    source = tabular_call(HEALTH_SAMPLES, "TargetUserId", "StartLocal", "EndLocal")
    return f"""let TargetUserId = {json.dumps(user_id, ensure_ascii=False)};
let StartLocal = {start_literal};
let EndLocal = {end_literal};
{source}
| where UserId == TargetUserId and DateFrom >= StartLocal and DateFrom < EndLocal
| where Type == "WORKOUT"
| extend RecordKey=iff(isnotempty(Uuid), strcat("uuid:", Uuid),
    strcat("fallback:", SourcePlatform, "|", SourceId, "|", SourceName, "|", SourceDeviceId))
| summarize arg_max(IngestionTime, *) by UserId, RecordKey, DateFrom, DateTo, Type, Unit
| project Payload=parse_json(tostring(Value))
| take {_PAYLOAD_SAMPLE_LIMIT + 1}"""


def _payload_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, dict):
        return "dictionary"
    if isinstance(value, list):
        return "array"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        try:
            if value.strip() and math.isfinite(float(value)):
                return "numeric_string"
        except (ValueError, OverflowError):
            pass
        return "string"
    return "unknown"


def _label_path(path: tuple[str, ...]) -> bool:
    """Only disclose category/unit labels, never arbitrary notes or IDs."""
    words = [re.sub(r"[^a-z0-9]", "", key.lower()) for key in path if key != "[]"]
    leaf = words[-1] if words else ""
    if leaf == "type" and len(words) == 1:  # Serialized __type, not a metric.
        return path[-1] == "__type"
    if leaf in {"unit", "units", "measurementtype", "metrictype", "metric"} or leaf.endswith("unit"):
        return True
    category = any(term in leaf for term in ("activity", "workout", "exercise", "sport"))
    if category and (leaf.endswith(("type", "name", "label")) or leaf in {
        "activity", "workout", "exercise", "sport",
    }):
        return True
    return leaf in {"type", "name", "label"} and any(
        any(term in parent for term in ("activity", "workout", "exercise", "sport"))
        for parent in words[:-1]
    )


def observe_workout_payload_schema(
    user_id: str,
    start_local: Any,
    end_local: Any,
    execute_query: Callable[[str], Any],
) -> dict[str, Any]:
    """Discover heterogeneous Value JSON without sending metric values to LLMs.

    Paths are literal key components; ``[]`` denotes an array element. Limits
    are explicit: a sampled/malformed payload never establishes metric absence.
    This probe cannot access a hidden import column through a stored function.
    """
    unavailable = {"status": "unavailable", "source": HEALTH_SAMPLES, "fields": []}
    try:
        user = str(user_id or "").strip()
        start, start_literal = _local_bound(start_local)
        end, end_literal = _local_bound(end_local)
        if not user or start >= end:
            return {**unavailable, "reason": "invalid_scope"}
        query = _payload_query(user, start_literal, end_literal)
    except (TypeError, ValueError):
        return {**unavailable, "reason": "invalid_scope"}
    try:
        if not inspect_query_safety(query, user).get("is_safe"):
            return {**unavailable, "reason": "guard_rejected"}
        rows = execute_query(query)
    except Exception:
        return {**unavailable, "reason": "discovery_failed"}
    if not isinstance(rows, list) or any(not isinstance(row, dict) or "Payload" not in row for row in rows):
        return {**unavailable, "reason": "invalid_discovery_result"}
    fields: dict[tuple[str, ...], dict[str, Any]] = {}
    shape_limited = False
    objects = 0

    def visit(value: Any, path: tuple[str, ...], seen: set, nonnull: set) -> None:
        nonlocal shape_limited
        if path:
            if any(len(key) > 128 for key in path):
                shape_limited = True
                return
            if path not in fields and len(fields) >= _PAYLOAD_FIELD_LIMIT:
                shape_limited = True
                return
            entry = fields.setdefault(path, {"types": set(), "decoded_types": set(), "labels": set(),
                                             "present_rows": 0, "nonnull_rows": 0})
            entry["types"].add(_payload_type(value))
            if path not in seen:
                entry["present_rows"] += 1
                seen.add(path)
            if value is not None and path not in nonnull:
                entry["nonnull_rows"] += 1
                nonnull.add(path)
            if isinstance(value, str) and value and _label_path(path):
                # Long/control-bearing strings are not useful category labels.
                if len(value) <= 100 and not any(ord(ch) < 32 for ch in value):
                    if value not in entry["labels"] and len(entry["labels"]) >= _CODE_LIMIT:
                        shape_limited = True
                    else:
                        entry["labels"].add(value)
            if isinstance(value, str) and value.lstrip().startswith(("{", "[")):
                try:
                    decoded = json.loads(value)
                except (ValueError, TypeError):
                    decoded = None
                if isinstance(decoded, (dict, list)):
                    entry["decoded_types"].add(_payload_type(decoded))
                    value = decoded
        if isinstance(value, (dict, list)) and len(path) >= _PAYLOAD_DEPTH_LIMIT:
            shape_limited |= bool(value)
            return
        if isinstance(value, dict):
            for key, child in value.items():
                if isinstance(key, str):
                    visit(child, (*path, key), seen, nonnull)
        elif isinstance(value, list):
            shape_limited |= len(value) > _PAYLOAD_ARRAY_LIMIT
            for child in value[:_PAYLOAD_ARRAY_LIMIT]:
                visit(child, (*path, "[]"), seen, nonnull)

    for row in rows[:_PAYLOAD_SAMPLE_LIMIT]:
        payload = row["Payload"]
        # SDKs/legacy functions can return a JSON string instead of a dict.
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (ValueError, TypeError):
                payload = None
        if isinstance(payload, dict):
            objects += 1
            visit(payload, (), set(), set())
    descriptions = []
    for path, entry in sorted(fields.items()):
        description = {"path": list(path), "types": sorted(entry["types"]),
                       "present_rows": entry["present_rows"], "nonnull_rows": entry["nonnull_rows"]}
        if entry["labels"]:
            description["observed_labels"] = sorted(entry["labels"])
        if entry["decoded_types"]:
            description["decoded_json_types"] = sorted(entry["decoded_types"])
        descriptions.append(description)
    return {"status": "empty" if not rows else "observed" if objects else "unusable",
            "source": HEALTH_SAMPLES, "sampled_rows": min(len(rows), _PAYLOAD_SAMPLE_LIMIT),
            "object_rows": objects, "sample_limit_reached": len(rows) > _PAYLOAD_SAMPLE_LIMIT,
            "schema_limit_reached": shape_limited, "fields": descriptions}


__all__ = ["observe_sensor_schema", "observe_workout_payload_schema"]
