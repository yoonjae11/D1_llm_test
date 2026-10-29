"""Pure query-result parsing and normalisation policy."""

import datetime
import json
import math
import re
from collections.abc import Mapping
from typing import Any, Dict, Optional

from chatbot.policies.query_algebra import MAX_OUTPUTS

UNAVAILABLE_RESULT_STATUSES = frozenset({
    "no_metric_evidence", "no_condition_evidence", "insufficient_time_resolution",
    "insufficient_observations", "insufficient_unit_evidence",
    "invalid_numeric_result",
})


def _safe_json_loads(text: str, default: Any) -> Any:
    """Local equivalent of the legacy node helper, without a node import."""

    try:
        return json.loads(text)
    except Exception:
        return default


def _parse_query_result(result: Any) -> Any:
    if result is None:
        return []
    if isinstance(result, (list, dict)):
        return result
    if isinstance(result, str):
        return _safe_json_loads(result, default=[])
    return []


def _has_records(parsed: Any) -> bool:
    if isinstance(parsed, list):
        return len(parsed) > 0
    if isinstance(parsed, dict):
        return len(parsed) > 0
    return False


def trim_records(parsed: Any, max_rows: int = 20) -> Any:
    """Apply the bounded result limit used by graph nodes."""

    if isinstance(parsed, list):
        return parsed[:max_rows]
    return parsed


def trim_records_for_plan(parsed: Any, plan: Any, max_rows: int = 20) -> Any:
    """Apply a bounded result limit without starving typed output series.

    Schema-v2 ``aggregation: none`` outputs are long-form series: the compiler
    emits one row per selected entity *and* requested output.  Applying the
    legacy global slice means an earlier-sorted metric can consume the whole
    allowance (for example, two 30-day series with a 40-row limit).  For a
    valid typed plan, treat ``max_rows`` as the cap for each unaggregated
    output, including scalar aggregates. A scalar query that accidentally
    returns several rows must not silently become its first value. Keep those
    rows visible to the answer/evaluation interface. The public
    schema bounds outputs to ``MAX_OUTPUTS``, so the total remains bounded by
    ``MAX_OUTPUTS * max_rows``.

    Malformed plans or result rows deliberately fall back to the original
    global trim.  Version-1 behavior therefore remains byte-for-byte
    compatible and untrusted result labels cannot manufacture extra buckets.
    """

    configured_limit = max(1, int(max_rows))
    if not isinstance(parsed, list):
        return parsed
    if not isinstance(plan, Mapping) or plan.get("schema_version") != 2:
        return trim_records(parsed, max_rows=configured_limit)

    computation = plan.get("computation")
    outputs = computation.get("outputs") if isinstance(computation, Mapping) else None
    if (
        not isinstance(outputs, list)
        or not outputs
        or len(outputs) > MAX_OUTPUTS
    ):
        return trim_records(parsed, max_rows=configured_limit)

    output_limits: Dict[tuple[str, str], int] = {}
    for output in outputs:
        if not isinstance(output, Mapping):
            return trim_records(parsed, max_rows=configured_limit)
        label = str(output.get("metric") or output.get("entity") or "").strip()
        aggregation = str(output.get("aggregation") or "").strip()
        if not label or not aggregation:
            return trim_records(parsed, max_rows=configured_limit)
        key = (label, aggregation)
        if key in output_limits:
            return trim_records(parsed, max_rows=configured_limit)
        output_limits[key] = configured_limit

    # Only use the expanded typed bound when every returned row proves that it
    # belongs to one of the declared outputs.  Otherwise fail back to the
    # conservative legacy slice instead of trusting ADX-provided labels.
    row_keys = []
    for row in parsed:
        if not isinstance(row, Mapping):
            return trim_records(parsed, max_rows=configured_limit)
        key = (
            str(row.get("Metric") or "").strip(),
            str(row.get("Aggregation") or "").strip(),
        )
        if key not in output_limits:
            return trim_records(parsed, max_rows=configured_limit)
        row_keys.append(key)

    counts: Dict[tuple[str, str], int] = {}
    retained = []
    for row, key in zip(parsed, row_keys):
        count = counts.get(key, 0)
        if count >= output_limits[key]:
            continue
        retained.append(row)
        counts[key] = count + 1
    return retained


def _normalize_datetime_like_string(text: str) -> str:
    value = str(text or "").strip()
    if not value:
        return value
    match = re.match(
        r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})(?::(\d{2}))?(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$",
        value,
    )
    if match:
        second = match.group(3) or "00"
        return f"{match.group(1)} {match.group(2)}:{second}"
    return value


def _epoch_seconds_to_date_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    try:
        seconds = int(float(value))
    except Exception:
        return None
    if seconds <= 0:
        return None
    try:
        date_value = datetime.datetime.utcfromtimestamp(seconds).date()
    except Exception:
        return None
    if date_value.year < 1900 or date_value.year > 2100:
        return None
    return date_value.isoformat()


def normalize_result_values(data: Any) -> Any:
    """Normalize ADX values into JSON-safe response data."""

    if isinstance(data, list):
        return [normalize_result_values(item) for item in data]
    if isinstance(data, dict):
        normalized: Dict[str, Any] = {}
        raw_value = data.get("Value")
        nonfinite_value = (isinstance(raw_value, float) and not math.isfinite(raw_value)) or (
            isinstance(raw_value, str) and raw_value.strip().lower()
            in {"nan", "+nan", "-nan", "inf", "+inf", "-inf", "infinity", "+infinity", "-infinity"}
        )
        for key, value in data.items():
            key_text = str(key)
            value_norm = normalize_result_values(value)
            if "birth_date" in key_text.lower():
                converted = _epoch_seconds_to_date_text(value_norm)
                if converted:
                    value_norm = converted
            normalized[key_text] = value_norm
        # ADX may serialize IEEE NaN/Infinity as strings. In the numeric
        # long-form Value slot they are undefined results, not answers or zero.
        # Free-text fields containing those words remain untouched.
        if "Metric" in normalized and nonfinite_value:
            normalized["Value"] = None
            normalized["ResultStatus"] = "invalid_numeric_result"
        # Kusto sum() can emit a default zero on an empty input. An explicit
        # no-evidence status is absence, not a measured zero, in the shared
        # long-form result contract. Counts with status ok remain real zeros.
        if (
            "Metric" in normalized and "Value" in normalized
            and str(normalized.get("ResultStatus") or "").lower() in UNAVAILABLE_RESULT_STATUSES
        ):
            normalized["Value"] = None
        return normalized
    if isinstance(data, float) and not math.isfinite(data):
        return None
    if isinstance(data, datetime.datetime):
        normalized = data.replace(tzinfo=None) if data.tzinfo else data
        return normalized.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(data, datetime.date):
        return data.isoformat()
    if isinstance(data, str):
        return _normalize_datetime_like_string(data)
    return data


def _is_meaningless_value(value) -> bool:
    """Return True if value is None, empty, NaN, or zero -- i.e. not worth mentioning."""
    if value is None:
        return True
    s = str(value).strip().lower()
    if s in ("", "nan", "none", "null"):
        return True
    try:
        if float(s) == 0.0:
            return True
    except (ValueError, TypeError):
        pass
    return False


def _filter_meaningless_metrics(data_rows: Any) -> Any:
    """Remove keys whose values are 0/NaN/None from each row, along with their _source companions.

    This prevents the LLM from mentioning metrics like 'smartphone sleep duration is 0 hours'
    or 'heart rate is NaN' in its response.
    """
    if not isinstance(data_rows, list):
        return data_rows
    cleaned = []
    for row in data_rows:
        if not isinstance(row, dict):
            cleaned.append(row)
            continue
        if "Metric" in row and "Value" in row and "ResultStatus" in row:
            # A long-form measurement row already distinguishes unavailable
            # values from recorded zero. Do not apply the legacy wide-row
            # display heuristic to Value or its zero-valued evidence counters.
            cleaned.append(normalize_result_values(row))
            continue
        new_row: Dict[str, Any] = {}
        for key, value in row.items():
            if str(key).endswith("_source"):
                continue  # handled together with main key below
            if _is_meaningless_value(value):
                continue  # drop this key and its _source
            new_row[key] = value
            source_key = f"{key}_source"
            if source_key in row:
                new_row[source_key] = row[source_key]
        cleaned.append(new_row)
    return cleaned
