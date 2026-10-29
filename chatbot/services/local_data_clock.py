"""Discover a bounded local-test clock from shared sensor date metadata."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
import json
from zoneinfo import ZoneInfo

from chatbot.policies.kql_generation import (
    PHONE_CUMULATIVE_METRIC_TYPES, PHONE_SAMPLED_METRIC_TYPES,
)
from chatbot.policies.kql_safety import inspect_query_safety
from chatbot.policies.semantic_resources import (
    ACTIVITY_DAILY, HEALTH_SAMPLES, SLEEP_NIGHTLY, WORKOUT_SESSIONS, tabular_call,
)
from chatbot.policies.time_ranges import EvaluationTimeContext
from chatbot.services.adx import ADXQueryPort, default_adx_service


class LocalClockDiscoveryError(RuntimeError):
    """A latest-data clock cannot be safely inferred from available metadata."""


@dataclass(frozen=True)
class DiscoveredDataClock:
    context: EvaluationTimeContext
    basis: str
    record_count: int
    queries: tuple[str, ...]
    scan_start: date
    scan_end: date
    mode: str = "latest-data"
    missing_families: tuple[str, ...] = ()

    @property
    def limitation(self) -> str:
        if self.missing_families:
            return ("No clock evidence for " + ", ".join(self.missing_families)
                    + "; that family's clock uses the available family bounds. "
                    "This does not establish measurement availability.")
        if self.basis == "normalized_sleep_day":
            return (
                "No daytime measurement or workout dates were found; the clock uses "
                "normalized SleepDay, which may reflect a wake day rather than a "
                "source's daily record date."
            )
        return "Clock discovery is limited to the latest 3,660 calendar days."

    def diagnostics(self) -> dict:
        return {
            "mode": self.mode, "basis": self.basis,
            "record_count": self.record_count,
            "scan_start": self.scan_start.isoformat(),
            "scan_end_exclusive": self.scan_end.isoformat(),
            "discovery_kql": list(self.queries), "limitation": self.limitation,
            "missing_families": list(self.missing_families),
        }


def _type_values(catalog: dict) -> set[str]:
    return {kind for kinds in catalog.values() for kind in kinds}


def build_clock_metadata_kql(user_id: str, scan_start: date, scan_end: date,
                             *, sleep_only: bool = False) -> str:
    """Retrieve dates only; overnight endpoints never define the daytime clock."""
    if not user_id or not (0 < (scan_end - scan_start).days <= 3660):
        raise ValueError("Clock lookup requires a user and at most 3,660 bounded days")
    preamble = (
        f"let TargetUserId={json.dumps(user_id, ensure_ascii=False)};\n"
        f"let StartLocal=datetime({scan_start.isoformat()});\n"
        f"let EndLocal=datetime({scan_end.isoformat()});\n"
    )
    if sleep_only:
        source = (
            f"{tabular_call(SLEEP_NIGHTLY, 'TargetUserId', 'StartLocal', 'EndLocal')}\n"
            "| where UserId == TargetUserId and SleepDay >= StartLocal and SleepDay < EndLocal\n"
            "| project LogicalDay=startofday(SleepDay)\n"
        )
    else:
        cumulative = _type_values(PHONE_CUMULATIVE_METRIC_TYPES)
        numeric_types = cumulative | _type_values(PHONE_SAMPLED_METRIC_TYPES)
        cumulative_kql = ", ".join(json.dumps(value) for value in sorted(cumulative))
        numeric_kql = ", ".join(json.dumps(value) for value in sorted(numeric_types))
        source = (
            "union\n"
            f"({tabular_call(ACTIVITY_DAILY, 'TargetUserId', 'StartLocal', 'EndLocal')}\n"
            "| where UserId == TargetUserId and LocalDate >= StartLocal and LocalDate < EndLocal\n"
            f"| where Type in ({cumulative_kql})\n"
            "| project LogicalDay=startofday(LocalDate)),\n"
            f"({tabular_call(HEALTH_SAMPLES, 'TargetUserId', 'StartLocal', 'EndLocal')}\n"
            "| where UserId == TargetUserId and DateFrom >= StartLocal and DateFrom < EndLocal\n"
            f"| where Type in ({numeric_kql})\n"
            "| project LogicalDay=startofday(DateFrom)),\n"
            f"({tabular_call(WORKOUT_SESSIONS, 'TargetUserId', 'StartLocal', 'EndLocal')}\n"
            "| project-rename WorkoutStart=StartLocal\n"
            "| where UserId == TargetUserId and WorkoutStart >= StartLocal and WorkoutStart < EndLocal\n"
            "| project LogicalDay=startofday(WorkoutStart))\n"
        )
    return (
        preamble + source
        + "| where isnotnull(LogicalDay)\n"
        + "| summarize DataStartLocal=min(LogicalDay), AsOfLocal=max(LogicalDay), ClockRecordCount=count()"
    )


def _date_value(value) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except ValueError as exc:
        raise LocalClockDiscoveryError("ADX clock metadata contains an invalid date") from exc


def _read_metadata(port: ADXQueryPort, query: str, user_id: str):
    decision = inspect_query_safety(query, user_id)
    if not decision["is_safe"]:
        raise LocalClockDiscoveryError("Clock metadata lookup did not pass the shared query guard")
    try:
        result = port.execute_query(query, suppress_error_log=True)
    except Exception as exc:
        raise LocalClockDiscoveryError("ADX clock lookup failed; check authentication and retry") from exc
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except (ValueError, TypeError) as exc:
            raise LocalClockDiscoveryError("ADX clock lookup failed or returned invalid metadata") from exc
    if not isinstance(result, list) or len(result) > 1:
        raise LocalClockDiscoveryError("ADX clock lookup returned an unexpected metadata shape")
    if not result:
        return None, None, 0
    row = result[0]
    if not isinstance(row, dict) or not {"DataStartLocal", "AsOfLocal", "ClockRecordCount"} <= row.keys():
        raise LocalClockDiscoveryError("ADX clock lookup returned incomplete metadata")
    try:
        count = int(row["ClockRecordCount"])
    except (ValueError, TypeError) as exc:
        raise LocalClockDiscoveryError("ADX clock metadata contains an invalid record count") from exc
    minimum, reference = _date_value(row["DataStartLocal"]), _date_value(row["AsOfLocal"])
    if count == 0 and minimum is None and reference is None:
        return None, None, 0
    if count < 1 or minimum is None or reference is None or minimum > reference:
        raise LocalClockDiscoveryError("ADX clock metadata has inconsistent date bounds")
    return minimum, reference, count


def discover_latest_data_clock(user_id: str, timezone_iana: str = "Asia/Seoul", *,
                               port: ADXQueryPort | None = None,
                               today: date | None = None) -> DiscoveredDataClock:
    """Use one global latest logical day for every resource in a local run."""
    timezone = ZoneInfo(timezone_iana)
    real_today = today or datetime.now(timezone).date()
    scan_end = real_today + timedelta(days=1)
    scan_start = scan_end - timedelta(days=3660)
    selected_port = port if port is not None else default_adx_service()
    queries = [build_clock_metadata_kql(user_id, scan_start, scan_end)]
    minimum, reference, count = _read_metadata(selected_port, queries[-1], user_id)
    basis = "daytime_measurements_and_workout_starts"
    if count == 0:
        queries.append(build_clock_metadata_kql(user_id, scan_start, scan_end, sleep_only=True))
        minimum, reference, count = _read_metadata(selected_port, queries[-1], user_id)
        basis = "normalized_sleep_day"
    if count == 0:
        raise LocalClockDiscoveryError(
            "No logical data dates were found for this user in the bounded lookup; "
            "provide explicit --as-of/--data-start or choose --clock current."
        )
    if not (scan_start <= minimum <= reference < scan_end):
        raise LocalClockDiscoveryError("ADX clock dates fall outside the guarded lookup window")
    context = EvaluationTimeContext(
        summary_reference_date=reference, summary_min_date=minimum,
        workout_reference_date=reference, workout_min_date=minimum,
    )
    return DiscoveredDataClock(context, basis, count, tuple(queries), scan_start, scan_end)


def build_referenced_clock_metadata_kql(user_id: str, scan_start: date,
                                      scan_end: date, family: str) -> str:
    """Whole-table date metadata; never filter by question metric/activity.

    Non-overnight observations preserve the available daily date domain.
    Sleep starts/ends cannot reconstruct a lost source-summary date.
    """
    if not user_id or not (0 < (scan_end - scan_start).days <= 3660):
        raise ValueError("Clock lookup requires a user and at most 3,660 bounded days")
    preamble = (f"let TargetUserId={json.dumps(user_id, ensure_ascii=False)};\n"
                f"let StartLocal=datetime({scan_start.isoformat()});\n"
                f"let EndLocal=datetime({scan_end.isoformat()});\n")
    if family == "summary":
        source = (f"{tabular_call(HEALTH_SAMPLES, 'TargetUserId', 'StartLocal', 'EndLocal')}\n"
                  "| where UserId == TargetUserId and DateFrom >= StartLocal and DateFrom < EndLocal\n"
                  '| where Type !~ "WORKOUT" and not(Type startswith "SLEEP_")\n'
                  "| project LogicalDay=startofday(DateFrom)\n")
    elif family == "workout":
        source = (f"{tabular_call(WORKOUT_SESSIONS, 'TargetUserId', 'StartLocal', 'EndLocal')}\n"
                  "| project-rename WorkoutStart=StartLocal\n"
                  "| where UserId == TargetUserId and WorkoutStart >= StartLocal and WorkoutStart < EndLocal\n"
                  "| project LogicalDay=startofday(WorkoutStart)\n")
    else:
        raise ValueError(f"Unknown reference family: {family}")
    return (preamble + source + "| where isnotnull(LogicalDay)\n"
            "| summarize DataStartLocal=min(LogicalDay), AsOfLocal=max(LogicalDay), ClockRecordCount=count()")


def discover_referenced_data_clock(user_id: str, timezone_iana: str = "Asia/Seoul", *,
                                   port: ADXQueryPort | None = None,
                                   today: date | None = None) -> DiscoveredDataClock:
    """Local entrypoint opts into clocks per referenced semantic table family.

    The shared graph selects those clocks only after the LLM identifies its
    dependencies. A cross-family request uses their maximum reference date.
    Live callers never bind this evaluation context.
    """
    real_today = today or datetime.now(ZoneInfo(timezone_iana)).date()
    scan_end = real_today + timedelta(days=1)
    scan_start = scan_end - timedelta(days=3660)
    selected_port = port if port is not None else default_adx_service()
    families, queries = {}, []
    for family in ("summary", "workout"):
        query = build_referenced_clock_metadata_kql(user_id, scan_start, scan_end, family)
        queries.append(query)
        minimum, reference, count = _read_metadata(selected_port, query, user_id)
        if count:
            if not (scan_start <= minimum <= reference < scan_end):
                raise LocalClockDiscoveryError("ADX clock dates fall outside the guarded lookup window")
            families[family] = (minimum, reference, count)
    if not families:
        raise LocalClockDiscoveryError(
            "No non-overnight summary or workout date evidence; provide explicit "
            "--as-of/--data-start rather than guessing a lost summary date."
        )
    missing = tuple(family for family in ("summary", "workout") if family not in families)
    summary = families.get("summary") or families["workout"]
    workout = families.get("workout") or families["summary"]
    context = EvaluationTimeContext(summary_reference_date=summary[1], summary_min_date=summary[0],
                                    workout_reference_date=workout[1], workout_min_date=workout[0])
    return DiscoveredDataClock(context, "referenced_table_dates", sum(v[2] for v in families.values()),
                               tuple(queries), scan_start, scan_end, "referenced-data", missing)
