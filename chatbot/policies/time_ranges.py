"""Time range parsing and KQL time window helpers."""

import calendar
import datetime
import math
import re
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from .domain_catalog import VALID_TIME_UNITS


# Typed computations can fan out into several daily/session relations before
# reducing them. These deliberately generous limits preserve ordinary
# historical/evaluation questions while preventing accidental multi-century
# scans. Legacy schema-v1 behavior is intentionally unchanged.
_V2_MAX_RELATIVE_TIME_VALUES = {
    "day": 3660,
    "week": 523,
    "month": 120,
}
_V2_MAX_ABSOLUTE_DAYS = 3660

SUPPORTED_IANA_TIMEZONES = {
    "Asia/Seoul",
    "Europe/London",
    "Europe/Madrid",
    "Atlantic/Canary",
    "Africa/Ceuta",
}


@dataclass(frozen=True, init=False)
class EvaluationTimeContext:
    """Finite, request-local clock and coverage bounds for evaluation."""

    summary_reference_date: datetime.date
    summary_min_date: datetime.date
    workout_reference_date: Optional[datetime.date]
    workout_min_date: Optional[datetime.date] = None

    def __init__(
        self,
        summary_reference_date: Optional[datetime.date] = None,
        summary_min_date: Optional[datetime.date] = None,
        workout_reference_date: Optional[datetime.date] = None,
        workout_min_date: Optional[datetime.date] = None,
        *,
        reference_date: Optional[datetime.date] = None,
    ) -> None:
        # `reference_date` remains only as an input compatibility shim. New
        # evaluation callers must name the summary/workout clocks explicitly.
        summary_reference = summary_reference_date or reference_date
        if summary_reference is None or summary_min_date is None:
            raise ValueError("summary reference and minimum dates are required")
        object.__setattr__(self, "summary_reference_date", summary_reference)
        object.__setattr__(self, "summary_min_date", summary_min_date)
        object.__setattr__(self, "workout_reference_date", workout_reference_date)
        object.__setattr__(self, "workout_min_date", workout_min_date)

    @property
    def reference_date(self) -> datetime.date:
        """Deprecated compatibility view of the summary reference date."""

        return self.summary_reference_date

    def bounds(self, family: str) -> Tuple[datetime.date, datetime.date]:
        if family == "workout":
            return (
                self.workout_min_date or self.summary_min_date,
                self.workout_reference_date or self.summary_reference_date,
            )
        if family == "combined":
            workout_min, workout_reference = self.bounds("workout")
            return (min(self.summary_min_date, workout_min),
                    max(self.summary_reference_date, workout_reference))
        return self.summary_min_date, self.summary_reference_date


_EVALUATION_TIME: ContextVar[Optional[EvaluationTimeContext]] = ContextVar(
    "sensei_evaluation_time", default=None
)


@contextmanager
def use_evaluation_time(context: EvaluationTimeContext):
    """Bind an evaluation clock without changing concurrent production calls."""

    if not isinstance(context, EvaluationTimeContext):
        raise TypeError("context must be an EvaluationTimeContext")
    for family in ("summary", "workout"):
        minimum, reference = context.bounds(family)
        if minimum > reference:
            raise ValueError(f"evaluation {family} minimum date cannot follow reference date")
    token = _EVALUATION_TIME.set(context)
    try:
        yield context
    finally:
        _EVALUATION_TIME.reset(token)


def active_evaluation_time() -> Optional[EvaluationTimeContext]:
    return _EVALUATION_TIME.get()


def evaluation_time_family(plan: Any) -> str:
    """Select caller clocks from the LLM's semantic dependencies, not data IDs.

    Include condition/selection streams, not just the output entity. Resource
    priorities may include optional fallbacks and do not define dependencies.
    """
    if not isinstance(plan, dict):
        return "summary"
    from .domain_catalog import METRIC_DEFINITIONS
    from .semantic_resources import WORKOUT_SESSIONS, WITHINGS_WORKOUTS

    computation = plan.get("computation") or {}
    metrics = set(str(metric).lower() for metric in plan.get("metrics", []))
    for member in ("outputs", "conditions"):
        metrics.update(str(item.get("metric") or "").lower()
                       for item in computation.get(member, []) if isinstance(item, dict))
    selector = computation.get("selector") or {}
    if selector.get("by_metric"):
        metrics.add(str(selector["by_metric"]).lower())
    families = set()
    for metric in metrics - {""}:
        resource = METRIC_DEFINITIONS.get(metric, {}).get("resource")
        if resource:
            families.add("workout" if resource in {WORKOUT_SESSIONS, WITHINGS_WORKOUTS} else "summary")
    entity = str(computation.get("entity") or "").lower()
    if entity == "workout_session" or any(
        item.get("dimension") == "activity_name"
        for item in computation.get("filters", []) if isinstance(item, dict)
    ):
        families.add("workout")
    if entity in {"night", "sleep_session"}:
        families.add("summary")
    if len(families) > 1:
        return "combined"
    if families:
        return next(iter(families))
    resources = set(plan.get("resources_priority") or [])
    return "workout" if resources and resources <= {WORKOUT_SESSIONS, WITHINGS_WORKOUTS} else "summary"


def resolve_all_available_time_range(time_range: Any, plan: Any = None) -> Any:
    """Resolve all-available semantics to a finite, request-specific window.

    Evaluation binds the exact fixture min/max dates through a context variable.
    Production can pass its hydrated metric-availability map in ``plan`` under
    ``_availability_context``.  Existing availability rows that predate the
    earliest-date field remain safe: they fall back to the bounded production
    backfill horizon instead of producing an unbounded scan.
    """

    if not isinstance(time_range, dict):
        return time_range
    mode = str(time_range.get("mode") or "").strip().lower()
    anchor = str(time_range.get("anchor") or "").strip().lower()
    if mode != "all_available" and anchor != "all_available":
        return time_range
    context = active_evaluation_time()
    if context is not None:
        minimum, reference = context.bounds(evaluation_time_family(plan))
        return _absolute_range(minimum, reference)

    timezone_iana = ""
    availability_context: Any = None
    metrics: List[str] = []
    if isinstance(plan, dict):
        timezone_iana = str(plan.get("timezone_iana") or "")
        availability_context = plan.get("_availability_context")
        raw_metrics = plan.get("metrics")
        if isinstance(raw_metrics, list):
            metrics = [str(metric) for metric in raw_metrics if str(metric).strip()]

    today = _local_today(timezone_iana)
    reference = today
    minimum: Optional[datetime.date] = None
    if isinstance(availability_context, dict):
        selected_keys: set[str] = set()
        if metrics:
            # Local import avoids coupling the time parser to availability
            # persistence while still selecting only the resources in this plan.
            from .availability import metric_availability_keys

            for metric in metrics:
                selected_keys.update(metric_availability_keys(metric))
        selected_items = [
            item
            for key, item in availability_context.items()
            if not selected_keys or str(key).upper() in selected_keys
        ]
        starts: List[datetime.date] = []
        ends: List[datetime.date] = []
        for item in selected_items:
            if not isinstance(item, dict) or not item.get("is_available"):
                continue
            start = _date_value(item.get("earliest_date_from_local"))
            end = _date_value(
                item.get("latest_date_to_local")
                or item.get("latest_date_from_local")
            )
            if start is not None:
                starts.append(start)
            if end is not None:
                ends.append(end)
        if ends:
            reference = min(max(ends), today)
        if starts:
            minimum = min(starts)

    bounded_minimum = reference - datetime.timedelta(
        days=_V2_MAX_ABSOLUTE_DAYS - 1
    )
    if minimum is None or minimum < bounded_minimum:
        minimum = bounded_minimum
    return _absolute_range(minimum, reference)


def _date_value(value: Any) -> Optional[datetime.date]:
    """Return the local calendar date encoded by a storage/ADX value."""

    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return datetime.date.fromisoformat(text[:10])
        except ValueError:
            return None


_MONTH_NAMES = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}


def _safe_date(year: int, month: int, day: int) -> datetime.date:
    last_day = calendar.monthrange(year, month)[1]
    return datetime.date(year, month, min(day, last_day))


def _shift_months(base: datetime.date, months: int) -> datetime.date:
    month_index = (base.month - 1) + months
    year = base.year + (month_index // 12)
    month = (month_index % 12) + 1
    return _safe_date(year, month, base.day)


def _absolute_range(start: datetime.date, end: datetime.date) -> Dict[str, Any]:
    if end < start:
        start, end = end, start
    return {
        "mode": "absolute",
        "value": max(1, (end - start).days + 1),
        "unit": "day",
        "anchor": "absolute",
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
    }


def _strict_schema_integer(value: Any) -> Optional[int]:
    """Parse a JSON integer without accepting booleans or fractional numbers."""

    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            return None
        return int(value)
    if isinstance(value, str):
        token = value.strip()
        if not re.fullmatch(r"\+?\d+", token):
            return None
        # All supported values are at most four digits. Avoid Python's integer
        # conversion limit and classify a model-produced giant digit string as
        # an oversized range instead of raising out of sanitization.
        raw_digits = token.lstrip("+")
        if len(raw_digits) > 10:
            raise OverflowError("integer is outside the supported range")
        digits = raw_digits.lstrip("0") or "0"
        return int(digits)
    return None


def validate_v2_time_range(time_range: Any) -> List[Dict[str, str]]:
    """Return fail-closed errors for one typed-computation time window.

    The sanitizer and deterministic compiler both call this function so an
    invalid anchor cannot acquire different semantics through a direct compiler
    call. Every accepted mode/unit/anchor/value combination maps to exactly one
    window in ``_kql_phone_local_time_window``.
    """

    if not isinstance(time_range, dict):
        return [
            {
                "code": "invalid_time_range",
                "path": "time_range",
                "message": "Schema v2 requires a normalized time-range object.",
            }
        ]

    mode = str(time_range.get("mode") or "").strip().lower()
    anchor = str(time_range.get("anchor") or "").strip().lower()

    if mode == "all_available" or anchor == "all_available":
        return [{
            "code": "unresolved_all_available_time_range",
            "path": "time_range",
            "message": (
                "all_available must resolve to finite absolute start and end "
                "dates before execution."
            ),
        }]

    if anchor == "absolute" or mode == "absolute":
        if anchor != "absolute" or mode != "absolute":
            return [
                {
                    "code": "incompatible_time_anchor",
                    "path": "time_range",
                    "message": (
                        "Schema v2 absolute ranges require both mode and anchor "
                        "to be absolute."
                    ),
                }
            ]
        if str(time_range.get("unit") or "").strip().lower() != "day":
            return [
                {
                    "code": "invalid_time_range",
                    "path": "time_range.unit",
                    "message": "Schema v2 absolute ranges use calendar-day units.",
                }
            ]
        start_text = str(time_range.get("start_date") or "").strip()
        end_text = str(time_range.get("end_date") or "").strip()
        if not start_text or not end_text:
            return [
                {
                    "code": "invalid_time_range",
                    "path": "time_range",
                    "message": "Schema v2 absolute ranges require valid ISO start and end dates.",
                }
            ]
        try:
            start = datetime.date.fromisoformat(start_text)
            end = datetime.date.fromisoformat(end_text)
            span_days = abs((end - start).days) + 1
        except (TypeError, ValueError, OverflowError):
            return [
                {
                    "code": "invalid_time_range",
                    "path": "time_range",
                    "message": "Schema v2 absolute ranges require valid ISO start and end dates.",
                }
            ]
        try:
            declared_days = _strict_schema_integer(time_range.get("value"))
        except OverflowError:
            declared_days = None
        if declared_days != span_days:
            return [
                {
                    "code": "invalid_time_range",
                    "path": "time_range.value",
                    "message": (
                        "Schema v2 absolute-range value must equal the inclusive "
                        "number of calendar days."
                    ),
                }
            ]
        if span_days > _V2_MAX_ABSOLUTE_DAYS:
            return [
                {
                    "code": "time_range_too_large",
                    "path": "time_range",
                    "message": (
                        "Schema v2 time ranges are limited to 3660 inclusive "
                        "calendar days."
                    ),
                }
            ]
        return []

    if mode != "relative":
        return [
            {
                "code": "unsupported_time_mode",
                "path": "time_range.mode",
                "message": "Schema v2 time ranges support relative or absolute mode.",
            }
        ]

    unit = str(time_range.get("unit") or "").strip().lower()
    maximum = _V2_MAX_RELATIVE_TIME_VALUES.get(unit)
    if maximum is None:
        return [
            {
                "code": "unsupported_time_unit",
                "path": "time_range.unit",
                "message": "Schema v2 time ranges support day, week, or month units.",
            }
        ]

    allowed_anchors = {
        "day": {"rolling", "today", "yesterday", "day_before_yesterday"},
        "week": {"rolling", "this_week", "last_week"},
        "month": {"rolling", "this_month", "last_month"},
    }
    if anchor not in allowed_anchors[unit]:
        return [
            {
                "code": "incompatible_time_anchor",
                "path": "time_range.anchor",
                "message": (
                    f"Schema v2 anchor '{anchor or '<empty>'}' is not valid for "
                    f"unit '{unit}'."
                ),
            }
        ]

    raw_value = time_range.get("value")
    if "include_today" in time_range and not isinstance(time_range["include_today"], bool):
        return [{"code": "invalid_time_range", "path": "time_range.include_today",
                 "message": "include_today must be a JSON boolean interpreted from the request."}]
    try:
        value = _strict_schema_integer(raw_value)
    except OverflowError:
        return [
            {
                "code": "time_range_too_large",
                "path": "time_range.value",
                "message": (
                    f"Schema v2 {unit} ranges are limited to {maximum} {unit}s "
                    "(approximately ten years)."
                ),
            }
        ]
    if value is None or value < 1:
        return [
            {
                "code": "invalid_time_range",
                "path": "time_range.value",
                "message": "Schema v2 time-range value must be a positive integer.",
            }
        ]
    if value > maximum:
        return [
            {
                "code": "time_range_too_large",
                "path": "time_range.value",
                "message": (
                    f"Schema v2 {unit} ranges are limited to {maximum} {unit}s "
                    "(approximately ten years)."
                ),
            }
        ]
    # ``last N weeks`` is a sequence of the N complete calendar weeks before
    # the current week.  Other fixed calendar anchors name one period only.
    if (
        anchor != "rolling"
        and value != 1
        and not (unit == "week" and anchor == "last_week")
    ):
        return [
            {
                "code": "incompatible_time_anchor",
                "path": "time_range.value",
                "message": (
                    "Schema v2 fixed calendar anchors require value 1; use "
                    "anchor 'rolling' for multi-period windows."
                ),
            }
        ]
    return []


def _parse_absolute_date_range_from_question(question: str) -> Optional[Dict[str, Any]]:
    """Parse explicit local calendar dates such as ``June 30, 2026``.

    The planner schema is mostly relative-date based. This parser lets explicit
    user dates override an LLM-produced relative anchor, preventing "June 30"
    from being treated as today/yesterday during KQL window construction.
    """
    q = str(question or "")
    today = _local_today()
    dates: List[datetime.date] = []

    for match in re.finditer(r"\b(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})\b", q):
        try:
            dates.append(_safe_date(int(match.group(1)), int(match.group(2)), int(match.group(3))))
        except Exception:
            pass

    month_pattern = "|".join(sorted(_MONTH_NAMES, key=len, reverse=True))
    month_matches = list(
        re.finditer(
            rf"\b({month_pattern})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s*(20\d{{2}}))?\b",
            q,
            flags=re.IGNORECASE,
        )
    )
    inferred_year: Optional[int] = None
    for match in month_matches:
        if match.group(3):
            inferred_year = int(match.group(3))
    if inferred_year is None:
        inferred_year = today.year
    for match in month_matches:
        month = _MONTH_NAMES.get(match.group(1).lower().rstrip("."))
        if not month:
            continue
        year = int(match.group(3)) if match.group(3) else inferred_year
        try:
            dates.append(_safe_date(year, month, int(match.group(2))))
        except Exception:
            pass

    if not dates:
        return None
    unique = sorted({item for item in dates})
    return _absolute_range(unique[0], unique[-1])


def _local_today(timezone_iana: str = "") -> datetime.date:
    """Current calendar date in the user's timezone (NOT the UTC server date).

    Date labels, coverage windows and "today/yesterday" must match the
    timezone-aware KQL window (_kql_phone_local_time_window). Using the server's
    UTC date here shifts every label by up to a day around midnight.
    """
    evaluation_time = active_evaluation_time()
    if evaluation_time is not None:
        return evaluation_time.summary_reference_date
    tz = _valid_timezone(timezone_iana)
    try:
        return datetime.datetime.now(ZoneInfo(tz)).date()
    except Exception:
        # tzdata missing on the host: fall back to UTC date rather than crash.
        return datetime.datetime.now(datetime.timezone.utc).date()


def compute_window_bounds(
    time_range: Dict[str, Any], timezone_iana: str = "", *,
    reference_date: Optional[datetime.date] = None,
) -> Tuple[datetime.date, datetime.date]:
    """Return inclusive local-date bounds for a validated time-range policy."""

    if str(time_range.get("anchor", "")).lower() == "absolute":
        try:
            start = datetime.date.fromisoformat(str(time_range.get("start_date") or ""))
            end = datetime.date.fromisoformat(str(time_range.get("end_date") or time_range.get("start_date") or ""))
            if end < start:
                start, end = end, start
            return start, end
        except Exception:
            pass

    today = reference_date or _local_today(timezone_iana)
    value = max(1, int(time_range.get("value", 7)))
    unit = str(time_range.get("unit", "day")).lower()
    anchor = str(time_range.get("anchor", "rolling")).lower()

    if unit == "hour":
        # This API returns calendar dates, so use a conservative inclusive
        # date envelope around the exact rolling-hour KQL window.
        return today - datetime.timedelta(days=math.ceil(value / 24)), today

    if unit == "day":
        if anchor == "today":
            return today, today
        if anchor == "yesterday":
            d = today - datetime.timedelta(days=1)
            return d, d
        if anchor == "day_before_yesterday":
            d = today - datetime.timedelta(days=2)
            return d, d
        # Research convention: "last/past N days" means the N completed local
        # calendar days before today.  Today is deliberately excluded.
        if time_range.get("include_today") is True:
            return today - datetime.timedelta(days=value - 1), today
        return (
            today - datetime.timedelta(days=value),
            today - datetime.timedelta(days=1),
        )

    if unit == "week":
        # Calendar weeks are Sunday through Saturday.
        this_week_start = today - datetime.timedelta(
            days=(today.weekday() + 1) % 7
        )
        if anchor == "this_week":
            return this_week_start, today
        if anchor == "last_week":
            last_week_start = this_week_start - datetime.timedelta(days=7 * value)
            return last_week_start, this_week_start - datetime.timedelta(days=1)
        if time_range.get("include_today") is True:
            return today - datetime.timedelta(days=value * 7 - 1), today
        return (
            today - datetime.timedelta(days=value * 7),
            today - datetime.timedelta(days=1),
        )

    this_month_start = today.replace(day=1)
    if anchor == "this_month":
        return this_month_start, today
    if anchor == "last_month":
        last_month_any = _shift_months(this_month_start, -1)
        last_month_start = last_month_any.replace(day=1)
        return last_month_start, this_month_start - datetime.timedelta(days=1)
    return _shift_months(today, -value), today


def resolve_evaluation_plan_time_range(
    plan: Any, timezone_iana: str = ""
) -> Dict[str, Any]:
    """Freeze a sanitized plan window against its semantic evaluation clock."""

    if not isinstance(plan, dict) or not isinstance(plan.get("time_range"), dict):
        raise ValueError("a sanitized plan with time_range is required")
    context = active_evaluation_time()
    if context is None:
        raise RuntimeError("evaluation time context is not active")
    time_range = resolve_all_available_time_range(plan["time_range"], plan)
    if str(time_range.get("anchor") or "").lower() == "absolute":
        start = datetime.date.fromisoformat(str(time_range.get("start_date") or ""))
        end = datetime.date.fromisoformat(str(time_range.get("end_date") or ""))
        return _absolute_range(start, end)
    minimum, reference = context.bounds(evaluation_time_family(plan))
    shadow = EvaluationTimeContext(
        summary_reference_date=reference,
        summary_min_date=minimum,
        workout_reference_date=reference,
        workout_min_date=minimum,
    )
    with use_evaluation_time(shadow):
        start, end = compute_window_bounds(time_range, timezone_iana)
    return _absolute_range(start, end)


def _compute_baseline_bounds(
    time_range: Dict[str, Any], timezone_iana: str = ""
) -> Tuple[datetime.date, datetime.date]:
    start, _ = compute_window_bounds(time_range, timezone_iana)
    value = max(1, int(time_range.get("value", 7)))
    unit = str(time_range.get("unit", "day")).lower()
    anchor = str(time_range.get("anchor", "rolling")).lower()

    if unit == "hour":
        baseline_days = max(1, math.ceil(value / 24))
        baseline_end = start - datetime.timedelta(days=1)
        baseline_start = baseline_end - datetime.timedelta(days=baseline_days - 1)
        return baseline_start, baseline_end

    if unit == "day":
        baseline_days = value if value >= 3 else 7
        baseline_end = start - datetime.timedelta(days=1)
        baseline_start = baseline_end - datetime.timedelta(days=baseline_days - 1)
        return baseline_start, baseline_end

    if unit == "week":
        baseline_days = value * 7
        baseline_end = start - datetime.timedelta(days=1)
        baseline_start = baseline_end - datetime.timedelta(days=baseline_days - 1)
        return baseline_start, baseline_end

    if anchor == "last_month":
        this_month_start = _local_today(timezone_iana).replace(day=1)
        current_start = _shift_months(this_month_start, -1).replace(day=1)
        baseline_start = _shift_months(current_start, -1).replace(day=1)
        baseline_end = current_start - datetime.timedelta(days=1)
        return baseline_start, baseline_end

    if anchor == "this_month":
        this_month_start = _local_today(timezone_iana).replace(day=1)
        baseline_start = _shift_months(this_month_start, -1).replace(day=1)
        baseline_end = this_month_start - datetime.timedelta(days=1)
        return baseline_start, baseline_end

    baseline_end = start - datetime.timedelta(days=1)
    baseline_start = _shift_months(start, -value)
    return baseline_start, baseline_end


def _default_time_range(case_type: str) -> Dict[str, Any]:
    if case_type == "trend":
        return {"mode": "relative", "value": 14, "unit": "day", "anchor": "rolling"}
    return {"mode": "relative", "value": 14, "unit": "day", "anchor": "rolling"}


def _has_policy_defined_time_expression(question: str) -> bool:
    """Return whether text names a time phrase with fixed research semantics.

    These are the small set of expressions for which product behavior must not
    drift with a model's interpretation: completed rolling days, calendar
    weeks/months, and named local dates. Other temporal language remains under
    the semantic planner's control.
    """

    q = " ".join(str(question or "").lower().split())
    english = re.search(
        r"\b(?:last|past|previous)\s+(?:(?:\d+|one|a)\s+)?"
        r"(?:days?|weeks?|months?|nights?)\b",
        q,
    ) or re.search(r"\b(?:today|yesterday|this week|this month)\b", q)
    korean = re.search(
        r"(?:지난|최근|과거|저번|이번)\s*(?:\d+\s*)?"
        r"(?:일간?|주간?|달|개월|시간|밤)|오늘|어제|그저께",
        q,
    )
    return bool(english or korean)


def _parse_time_range_from_question(question: str) -> Tuple[Optional[Dict[str, Any]], bool, bool]:
    q = (question or "").lower()
    if re.search(r"\b(?:all available|entire history|full history)\b", q):
        return {"mode": "all_available", "anchor": "all_available"}, False, True
    ambiguous_recent = any(word in q for word in ["recent", "lately", "nowadays", "these days"])
    absolute_range = _parse_absolute_date_range_from_question(question)
    if absolute_range:
        return absolute_range, ambiguous_recent, True

    # The benchmark/research policy distinguishes complete calendar weeks from
    # rolling durations.  Recognize that distinction before the generic unit
    # patterns below.  Korean variants are included because the product accepts
    # Korean questions even though the semantic plan itself uses English IDs.
    calendar_week_match = re.search(
        r"\b(?:last|past|previous)\s+(\d+)\s+weeks?\b", q
    ) or re.search(r"(?:지난|과거)\s*(\d+)\s*주(?:간)?", q)
    if calendar_week_match:
        return {
            "mode": "relative",
            "value": int(calendar_week_match.group(1)),
            "unit": "week",
            "anchor": "last_week",
        }, ambiguous_recent, True

    # A number counts as a period only after a period word ("past 7 days",
    # "last 3 hours"). Otherwise it is part of a condition ("slept less than
    # 9 hours", "at least 5 days") and must not become the retrieval window.
    period = r"\b(?:last|past|previous)\s+"
    hour_match = re.search(period + r"(\d+)\s*(hour|hours|hr|hrs)\b", q) or re.search(
        r"(?:지난|최근|과거)\s*(\d+)\s*시간", q
    )
    day_match = re.search(period + r"(\d+)\s*(day|days|night|nights)\b", q) or re.search(
        r"(?:지난|최근|과거)\s*(\d+)\s*일(?:간)?", q
    )
    week_match = re.search(period + r"(\d+)\s*(week|weeks)\b", q)
    month_match = re.search(period + r"(\d+)\s*(month|months)\b", q)
    if hour_match:
        return {
            "mode": "relative",
            "value": int(hour_match.group(1)),
            "unit": "hour",
            "anchor": "rolling",
        }, ambiguous_recent, True
    if day_match:
        return {"mode": "relative", "value": int(day_match.group(1)), "unit": "day", "anchor": "rolling"}, ambiguous_recent, True
    if week_match:
        return {
            "mode": "relative",
            "value": int(week_match.group(1)),
            "unit": "week",
            "anchor": "rolling",
        }, ambiguous_recent, True
    if month_match:
        return {
            "mode": "relative",
            "value": int(month_match.group(1)),
            "unit": "month",
            "anchor": "rolling",
        }, ambiguous_recent, True
    # "the night before" / "previous night" = one sleep night earlier than "last
    # night". Treated as an explicit time (yesterday's wake-day) so the follow-up
    # carry-forward in sanitize_plan does not silently reset it to last night.
    if "day before yesterday" in q or "그저께" in q:
        return {"mode": "relative", "value": 1, "unit": "day", "anchor": "day_before_yesterday"}, ambiguous_recent, True
    if "night before" in q or "previous night" in q:
        return {"mode": "relative", "value": 1, "unit": "day", "anchor": "yesterday"}, ambiguous_recent, True
    if "today" in q or "오늘" in q:
        return {"mode": "relative", "value": 1, "unit": "day", "anchor": "today"}, ambiguous_recent, True
    if any(word in q for word in ["yesterday", "last night", "어제", "지난밤"]):
        return {"mode": "relative", "value": 1, "unit": "day", "anchor": "yesterday"}, ambiguous_recent, True
    if "this week" in q or re.search(r"이번\s*주", q):
        return {"mode": "relative", "value": 1, "unit": "week", "anchor": "this_week"}, ambiguous_recent, True
    if re.search(r"\b(?:last|past|previous)\s+week\b", q) or re.search(
        r"(?:지난|저번)\s*주", q
    ):
        return {"mode": "relative", "value": 1, "unit": "week", "anchor": "last_week"}, ambiguous_recent, True
    if "this month" in q or re.search(r"이번\s*달", q):
        return {"mode": "relative", "value": 1, "unit": "month", "anchor": "this_month"}, ambiguous_recent, True
    if re.search(r"\b(?:last|past|previous)\s+month\b", q) or re.search(
        r"(?:지난|저번)\s*달", q
    ):
        return {"mode": "relative", "value": 1, "unit": "month", "anchor": "last_month"}, ambiguous_recent, True

    # A bare "one/a month" remains a rolling duration.  Explicit last/past
    # month wording above always means the previous calendar month.
    if re.search(r"\b(?:one|a)\s+month\b", q):
        return {"mode": "relative", "value": 1, "unit": "month", "anchor": "rolling"}, ambiguous_recent, True

    # "week by week", "weekly", "each week", "every week" → 1-week comparison
    if re.search(r"\bweek\s*by\s*week\b|\bweekly\b|\beach\s+week\b|\bevery\s+week\b", q):
        return {"mode": "relative", "value": 1, "unit": "week", "anchor": "rolling"}, ambiguous_recent, True

    # "month by month", "monthly", "each month", "every month" → 1-month comparison
    if re.search(r"\bmonth\s*by\s*month\b|\bmonthly\b|\beach\s+month\b|\bevery\s+month\b", q):
        return {"mode": "relative", "value": 1, "unit": "month", "anchor": "rolling"}, ambiguous_recent, True

    # "day by day", "daily", "each day", "every day" → 7-day rolling
    if re.search(r"\bday\s*by\s*day\b|\bdaily\b|\beach\s+day\b|\bevery\s+day\b", q):
        return {"mode": "relative", "value": 7, "unit": "day", "anchor": "rolling"}, ambiguous_recent, True

    if ambiguous_recent:
        return {"mode": "relative", "value": 14, "unit": "day", "anchor": "rolling"}, True, False
    return None, False, False


def _parse_time_of_day_range_from_question(question: str) -> Optional[Dict[str, int]]:
    q = (question or "").lower()

    def normalize(hour: int, marker: str) -> int:
        hour = int(hour)
        if marker == "pm" and hour < 12:
            return hour + 12
        if marker == "am" and hour == 12:
            return 0
        return hour

    en = re.search(
        r"(?P<h1>\d{1,2})(?::(?P<min1>\d{2}))?\s*(?P<m1>am|pm)?\s*"
        r"(?:to|through|until|-|~)\s*"
        r"(?P<h2>\d{1,2})(?::(?P<min2>\d{2}))?\s*(?P<m2>am|pm)?",
        q,
    )
    if en:
        marker1 = en.group("m1") or en.group("m2") or ""
        marker2 = en.group("m2") or marker1
        start_hour = normalize(int(en.group("h1")), marker1)
        end_hour = normalize(int(en.group("h2")), marker2)
        candidate = {
            "start_hour": start_hour,
            "start_minute": int(en.group("min1") or 0),
            "end_hour": end_hour,
            "end_minute": int(en.group("min2") or 0),
        }
        return _normalize_time_of_day_range(candidate)
    return None


def _normalize_time_of_day_range(value: Any) -> Optional[Dict[str, int]]:
    """Validate a planner-produced local clock window without inferring it."""

    if not isinstance(value, dict):
        return None
    try:
        start_hour = int(value.get("start_hour"))
        end_hour = int(value.get("end_hour"))
        start_minute = int(value.get("start_minute", 0))
        end_minute = int(value.get("end_minute", 0))
    except (TypeError, ValueError, OverflowError):
        return None
    if not (0 <= start_hour < 24 and 0 <= start_minute < 60):
        return None
    if not (0 <= end_hour <= 24 and 0 <= end_minute < 60):
        return None
    if end_hour == 24 and end_minute != 0:
        return None
    start_total = start_hour * 60 + start_minute
    end_total = end_hour * 60 + end_minute
    if not (0 <= start_total < end_total <= 24 * 60):
        return None
    result = {"start_hour": start_hour, "end_hour": end_hour}
    if start_minute or end_minute:
        result["start_minute"] = start_minute
        result["end_minute"] = end_minute
    return result


def _normalize_time_range(candidate: Optional[Dict[str, Any]], fallback: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(candidate, dict):
        return dict(fallback)

    candidate = resolve_all_available_time_range(candidate)

    mode = str(candidate.get("mode", "relative")).lower()
    if str(candidate.get("anchor", "")).lower() == "absolute":
        try:
            start = datetime.date.fromisoformat(str(candidate.get("start_date") or ""))
            end = datetime.date.fromisoformat(str(candidate.get("end_date") or candidate.get("start_date") or ""))
            return _absolute_range(start, end)
        except Exception:
            pass
    if mode != "relative":
        mode = "relative"

    try:
        value = int(candidate.get("value", fallback.get("value", 7)))
    except Exception:
        value = int(fallback.get("value", 7))
    value = max(1, value)

    unit = str(candidate.get("unit", fallback.get("unit", "day"))).lower()
    if unit not in VALID_TIME_UNITS:
        unit = str(fallback.get("unit", "day"))

    anchor = str(candidate.get("anchor", fallback.get("anchor", "rolling"))).lower()
    normalized = {"mode": mode, "value": value, "unit": unit, "anchor": anchor}
    if isinstance(candidate.get("include_today"), bool):
        normalized["include_today"] = candidate["include_today"]
    return normalized


def _kql_time_window(time_range: Dict[str, Any]) -> Tuple[str, str]:
    if str(time_range.get("anchor", "")).lower() == "absolute":
        try:
            start = datetime.date.fromisoformat(str(time_range.get("start_date") or ""))
            end = datetime.date.fromisoformat(str(time_range.get("end_date") or time_range.get("start_date") or ""))
            if end < start:
                start, end = end, start
            return f"datetime({start.isoformat()})", f"datetime({(end + datetime.timedelta(days=1)).isoformat()})"
        except Exception:
            pass

    value = max(1, int(time_range.get("value", 7)))
    unit = str(time_range.get("unit", "day")).lower()
    anchor = str(time_range.get("anchor", "rolling")).lower()

    if unit == "hour":
        return f"ago({value}h)", "now()"

    if unit == "day":
        if anchor == "today":
            return "startofday(now())", "startofday(now()) + 1d"
        if anchor == "yesterday":
            return "startofday(now() - 1d)", "startofday(now())"
        if anchor == "day_before_yesterday":
            return "startofday(now() - 2d)", "startofday(now() - 1d)"
        # N completed local dates immediately before today.
        if time_range.get("include_today") is True:
            return f"startofday(now() - {value - 1}d)", "startofday(now()) + 1d"
        return f"startofday(now() - {value}d)", "startofday(now())"

    if unit == "week":
        # Python weekday(): Monday=0.  Research calendar weeks are
        # Sunday-through-Saturday.
        today = datetime.date.today()
        this_sunday = today - datetime.timedelta(days=(today.weekday() + 1) % 7)
        if anchor == "this_week":
            return f"datetime({this_sunday.isoformat()})", "now()"
        if anchor == "last_week":
            first_sunday = this_sunday - datetime.timedelta(days=7 * value)
            return f"datetime({first_sunday.isoformat()})", f"datetime({this_sunday.isoformat()})"
        if time_range.get("include_today") is True:
            return f"startofday(now() - {value * 7 - 1}d)", "startofday(now()) + 1d"
        return f"startofday(now() - {value * 7}d)", "startofday(now())"

    if anchor == "this_month":
        return "startofmonth(now())", "now()"
    if anchor == "last_month":
        return "startofmonth(datetime_add('month', -1, now()))", "startofmonth(now())"
    return f"datetime_add('month', -{value}, now())", "now()"


def _valid_timezone(timezone_iana: str, fallback: str = "Asia/Seoul") -> str:
    value = str(timezone_iana or "").strip() or fallback
    try:
        ZoneInfo(value)
        return value
    except Exception:
        if value in SUPPORTED_IANA_TIMEZONES:
            return value
        return fallback


def _local_now_expr(timezone_iana: str) -> str:
    timezone = _valid_timezone(timezone_iana)
    return f'datetime_utc_to_local(now(), "{timezone}")'


def _kql_phone_local_time_window(
    time_range: Dict[str, Any],
    timezone_iana: str,
) -> Tuple[str, str]:
    """Return local wall-clock KQL bounds for Phone DateFrom/DateTo and MVs.

    Phone DateFrom/DateTo and Phone MV buckets are local wall-clock values, not
    UTC instants. The only UTC operation here is deriving the user's current
    local wall-clock time from now().
    """

    if str(time_range.get("anchor", "")).lower() == "absolute":
        try:
            start = datetime.date.fromisoformat(str(time_range.get("start_date") or ""))
            end = datetime.date.fromisoformat(str(time_range.get("end_date") or time_range.get("start_date") or ""))
            if end < start:
                start, end = end, start
            return f"datetime({start.isoformat()})", f"datetime({(end + datetime.timedelta(days=1)).isoformat()})"
        except Exception:
            pass

    value = max(1, int(time_range.get("value", 14)))
    unit = str(time_range.get("unit", "day")).lower()
    anchor = str(time_range.get("anchor", "rolling")).lower()
    local_now = _local_now_expr(timezone_iana)

    if unit == "hour":
        return f"({local_now} - {value}h)", local_now

    if unit == "day":
        if anchor == "today":
            return f"startofday({local_now})", f"startofday({local_now}) + 1d"
        if anchor == "yesterday":
            return f"startofday({local_now} - 1d)", f"startofday({local_now})"
        if anchor == "day_before_yesterday":
            return f"startofday({local_now} - 2d)", f"startofday({local_now} - 1d)"
        if time_range.get("include_today") is True:
            return f"startofday({local_now} - {value - 1}d)", f"startofday({local_now}) + 1d"
        return f"startofday({local_now} - {value}d)", f"startofday({local_now})"

    if unit == "week":
        # Sunday-based week start. dayofweek() is the elapsed timespan since
        # the preceding Sunday, so this remains correct on Sundays as well.
        this_sunday = (
            f"(startofday({local_now}) - "
            f"toint(dayofweek({local_now}) / 1d) * 1d)"
        )
        if anchor == "this_week":
            return this_sunday, local_now
        if anchor == "last_week":
            return f"({this_sunday} - {value * 7}d)", this_sunday
        if time_range.get("include_today") is True:
            return f"startofday({local_now} - {value * 7 - 1}d)", f"startofday({local_now}) + 1d"
        return f"startofday({local_now} - {value * 7}d)", f"startofday({local_now})"

    if anchor == "this_month":
        return f"startofmonth({local_now})", local_now
    if anchor == "last_month":
        return f"startofmonth(datetime_add('month', -1, {local_now}))", f"startofmonth({local_now})"
    return f"datetime_add('month', -{value}, {local_now})", local_now


def _kql_utc_event_time_window(time_range: Dict[str, Any], timezone_iana: str) -> Tuple[str, str]:
    """Return UTC KQL bounds for TelegramConversationLog.EventTimeUtc."""

    local_start, local_end = _kql_phone_local_time_window(time_range, timezone_iana)
    timezone = _valid_timezone(timezone_iana)
    return (
        f'datetime_local_to_utc({local_start}, "{timezone}")',
        f'datetime_local_to_utc({local_end}, "{timezone}")',
    )


def _kql_baseline_window(time_range: Dict[str, Any]) -> Tuple[str, str]:
    start, _ = _kql_time_window(time_range)
    value = max(1, int(time_range.get("value", 7)))
    unit = str(time_range.get("unit", "day")).lower()
    anchor = str(time_range.get("anchor", "rolling")).lower()

    if unit == "hour":
        return f"{start} - {value}h", start

    if unit == "day":
        baseline_days = value if value >= 3 else 7
        return f"{start} - {baseline_days}d", start
    if unit == "week":
        return f"{start} - {value * 7}d", start
    if anchor == "last_month":
        return "startofmonth(datetime_add('month', -2, now()))", "startofmonth(datetime_add('month', -1, now()))"
    return f"datetime_add('month', -{value * 2}, now())", f"datetime_add('month', -{value}, now())"


__all__ = [
    "EvaluationTimeContext",
    "SUPPORTED_IANA_TIMEZONES",
    "_MONTH_NAMES",
    "_absolute_range",
    "_compute_baseline_bounds",
    "_default_time_range",
    "_has_policy_defined_time_expression",
    "_kql_baseline_window",
    "_kql_phone_local_time_window",
    "_kql_time_window",
    "_kql_utc_event_time_window",
    "_local_now_expr",
    "_local_today",
    "_normalize_time_range",
    "_normalize_time_of_day_range",
    "_parse_absolute_date_range_from_question",
    "_parse_time_of_day_range_from_question",
    "_parse_time_range_from_question",
    "_safe_date",
    "_shift_months",
    "_valid_timezone",
    "compute_window_bounds",
    "active_evaluation_time",
    "evaluation_time_family",
    "resolve_all_available_time_range",
    "resolve_evaluation_plan_time_range",
    "use_evaluation_time",
    "validate_v2_time_range",
]
