"""Freeze a reactive query's time envelope; the LLM still writes its KQL.

The envelope covers primary/comparison periods, not a prescribed calculation.
Recurring clock windows must also be applied inside KQL on each local date.
"""
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from .time_ranges import (
    _normalize_time_of_day_range,
    _valid_timezone,
    active_evaluation_time,
    compute_window_bounds,
    evaluation_time_family,
)


def reference_clock_local(plan: dict) -> str:
    """A caller-frozen local clock, or an explicitly date-only evaluation clock."""
    frozen = plan.get("reference_clock_local")
    if frozen:
        datetime.fromisoformat(str(frozen))  # Reject malformed clock text.
        return str(frozen)
    evaluation = active_evaluation_time()
    if evaluation is not None:
        return evaluation.bounds(evaluation_time_family(plan))[1].isoformat()
    timezone = _valid_timezone(str(plan.get("timezone_iana") or ""))
    return datetime.now(ZoneInfo(timezone)).replace(tzinfo=None, microsecond=0).isoformat(sep=" ")


def reactive_logical_bounds(plan: dict) -> tuple[str, str]:
    """Return finite literal bounds for discovery and generation alike."""
    clock_text = reference_clock_local(plan)
    clock = datetime.fromisoformat(clock_text)
    ranges = [plan.get("time_range")]
    if plan.get("comparison_time_range"):
        ranges.append(plan["comparison_time_range"])
    windows = []
    clock_window = _normalize_time_of_day_range(plan.get("time_of_day"))
    for period in ranges:
        if not isinstance(period, dict):
            raise ValueError("Reactive queries require a validated time range")
        if str(period.get("unit", "day")).lower() == "hour":
            if len(clock_text) == 10:
                raise ValueError("A date-only evaluation clock cannot resolve rolling hours")
            hours = int(period.get("value", 1))
            if not 1 <= hours <= 24 * 3660:
                raise ValueError("Rolling-hour query exceeds the bounded horizon")
            start, end = clock - timedelta(hours=hours), clock
        else:
            first, last = compute_window_bounds(
                period, str(plan.get("timezone_iana") or ""), reference_date=clock.date(),
            )
            start = datetime.combine(first, time.min)
            end = datetime.combine(last + timedelta(days=1), time.min)
            if clock_window:
                # Envelope of EVERY requested date, not just its first day.
                start += timedelta(hours=clock_window["start_hour"],
                                   minutes=clock_window.get("start_minute", 0))
                end = datetime.combine(last, time.min) + timedelta(
                    hours=clock_window["end_hour"], minutes=clock_window.get("end_minute", 0),
                )
        windows.append((start, end))
    start, end = min(w[0] for w in windows), max(w[1] for w in windows)
    if not start < end or end - start > timedelta(days=3661):
        raise ValueError("Reactive query envelope is empty or exceeds the bounded horizon")
    return tuple(f"datetime({value.isoformat(sep=' ')})" for value in (start, end))


def query_time_scope(plan: dict) -> dict:
    """Separate requested date labels from the finite event retrieval envelope.

    This supplies time semantics, not KQL operators or benchmark date mappings.
    The LLM applies the requested logical date predicate after source retrieval.
    """
    first, last = reactive_logical_bounds(plan)
    start = datetime.fromisoformat(first[9:-1])
    end = datetime.fromisoformat(last[9:-1])
    basis = plan.get("sleep_date_basis")
    if basis == "night_day":
        # A noon-to-noon night can start on the following civil morning.
        end += timedelta(days=1)
    elif basis == "wake_day":
        start -= timedelta(days=1)
    return {"logical_start": first, "logical_end_exclusive": last,
            "scan_start": f"datetime({start.isoformat(sep=' ')})",
            "scan_end_exclusive": f"datetime({end.isoformat(sep=' ')})",
            "sleep_date_basis": basis,
            "night_boundary_local": "12:00" if basis == "night_day" else None}


def reactive_query_bounds(plan: dict) -> tuple[str, str]:
    scope = query_time_scope(plan)
    return scope["scan_start"], scope["scan_end_exclusive"]
