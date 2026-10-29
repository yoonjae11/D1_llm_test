# -*- coding: utf-8 -*-
"""User-configured threshold alerts + daily report (system-initiated).

- evaluate_user_thresholds: for each enabled threshold, check recent readings for
  a sustained breach (value beyond bound for >= sustain_minutes) and send a
  Telegram alert (reusing the proactive episode integration). Cooldown prevents
  repeat alerts for the same ongoing breach.
- build_daily_report / send_daily_report: once-a-day summary from the logical
  daily-activity resource + how many threshold breaches happened that day.

Detection core (_longest_breach_minutes) is pure and unit-testable.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from chatbot.alert_thresholds import metric_label, metric_unit
from chatbot.config import get_settings
from chatbot.operational_storage import (
    MetricAlertThreshold,
    utc_now,
)
from chatbot.prompts import load_prompt, load_role_prompt
from chatbot.policies.semantic_resources import (
    ACTIVITY_DAILY,
    HEALTH_SAMPLES,
    SLEEP_NIGHTLY,
    physical_name,
    tabular_call,
)
from chatbot.services.adx import ADXQueryPort, default_adx_service
from chatbot.services.llm import complete_chat, generate_alert_text
from chatbot.services.memory import OperationalStoragePort, create_operational_storage

logger = logging.getLogger(__name__)

adx_manager: ADXQueryPort = default_adx_service()


def OperationalTableRepository(
    connection_string: str | None = None,
    *,
    fail_soft: bool = True,
) -> OperationalStoragePort:
    """Compatibility constructor routed through the memory service."""

    return create_operational_storage(
        connection_string=connection_string,
        fail_soft=fail_soft,
    )


def _quote(value: str) -> str:
    return "'" + str(value or "").replace("'", "''") + "'"


def _parse_dt(value: Any) -> Optional[dt.datetime]:
    if isinstance(value, dt.datetime):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Detection core (pure)
# ---------------------------------------------------------------------------
def _longest_breach_minutes(
    readings: List[Tuple[dt.datetime, float]],
    lower: Optional[float],
    upper: Optional[float],
) -> Tuple[float, str, Optional[float], Optional[dt.datetime], Optional[dt.datetime]]:
    """Longest CONSECUTIVE run (in minutes) where a reading breaches a bound.

    readings: (timestamp, value) sorted ascending. Returns
    (max_run_minutes, direction, extreme_value, start_ts, end_ts) where direction
    is "high"/"low"/"" and start_ts/end_ts bound the longest breaching run.
    A run breaks when a reading is within bounds OR switches breach direction.
    """
    best_minutes = 0.0
    best_dir = ""
    best_extreme: Optional[float] = None
    best_start: Optional[dt.datetime] = None
    best_end: Optional[dt.datetime] = None

    run_start: Optional[dt.datetime] = None
    run_dir = ""
    run_extreme: Optional[float] = None
    run_last: Optional[dt.datetime] = None

    def _breach_dir(v: float) -> str:
        if upper is not None and v > upper:
            return "high"
        if lower is not None and v < lower:
            return "low"
        return ""

    for ts, value in readings:
        if ts is None or value is None:
            continue
        d = _breach_dir(value)
        if d and d == run_dir:
            run_last = ts
            if d == "high":
                run_extreme = max(run_extreme, value) if run_extreme is not None else value
            else:
                run_extreme = min(run_extreme, value) if run_extreme is not None else value
        elif d:
            # start a new run (different direction or first breach)
            run_start = ts
            run_last = ts
            run_dir = d
            run_extreme = value
        else:
            run_start = None
            run_dir = ""
            run_extreme = None
            run_last = None
        if run_start is not None and run_last is not None:
            minutes = (run_last - run_start).total_seconds() / 60.0
            if minutes > best_minutes:
                best_minutes = minutes
                best_dir = run_dir
                best_extreme = run_extreme
                best_start = run_start
                best_end = run_last
    return best_minutes, best_dir, best_extreme, best_start, best_end


def _readings_query(user_id: str, metric_type: str, timezone: str, lookback_minutes: int) -> str:
    local_now = f'datetime_utc_to_local(now(), "{timezone}")'
    return (
        f"let TargetUserId={_quote(user_id)};\n"
        f"let EndLocal={local_now};\n"
        f"let StartLocal=EndLocal-{int(lookback_minutes)}m;\n"
        f"{tabular_call(HEALTH_SAMPLES, 'TargetUserId', 'StartLocal', 'EndLocal')}"
        " | where UserId == TargetUserId"
        f" | where Type == {_quote(metric_type)}"
        " | where DateFrom >= StartLocal and DateFrom < EndLocal"
        " | project T=DateFrom, V=todouble(ValueNumber)"
        " | where isnotnull(V)"
        " | order by T asc"
    )


def evaluate_user_thresholds(
    app_user_id: str,
    *,
    repository: Optional[OperationalTableRepository] = None,
    now_utc: Optional[dt.datetime] = None,
    send: bool = True,
) -> Dict[str, Any]:
    """Check every enabled threshold for the user; alert on sustained breach."""
    settings = get_settings()
    if not settings.threshold_alert_enabled:
        return {"appUserId": app_user_id, "status": "disabled"}
    repository = repository or OperationalTableRepository(fail_soft=True)
    now_utc = now_utc or utc_now()
    profile = repository.get_user_profile(app_user_id)
    timezone = profile.timezone_iana or settings.default_timezone
    cooldown_min = int(settings.threshold_alert_lookback_minutes or 60)

    results: List[Dict[str, Any]] = []
    for thr in repository.list_alert_thresholds(app_user_id):
        if not thr.enabled or (thr.lower is None and thr.upper is None):
            continue
        lookback = max(int(thr.sustain_minutes or 10) + 5, cooldown_min)
        query = _readings_query(app_user_id, thr.metric_type, timezone, lookback)
        rows = adx_manager.execute_query(query, suppress_error_log=True)
        if isinstance(rows, str):  # "Error: ..."
            results.append({"metric": thr.metric_type, "status": "query_error"})
            continue
        readings = [(_parse_dt(r.get("T")), r.get("V")) for r in rows if isinstance(r, dict)]
        readings = [(t, float(v)) for t, v in readings if t is not None and v is not None]
        minutes, direction, extreme, breach_start, breach_end = _longest_breach_minutes(readings, thr.lower, thr.upper)
        breached = bool(direction) and minutes >= float(thr.sustain_minutes or 10)
        item = {"metric": thr.metric_type, "sustainedMinutes": round(minutes, 1),
                "direction": direction, "extreme": extreme, "breached": breached}

        if breached:
            last_alert = _parse_dt(thr.last_alert_at_utc)
            in_cooldown = bool(last_alert and (now_utc - last_alert).total_seconds() < cooldown_min * 60)
            item["inCooldown"] = in_cooldown
            if not in_cooldown and send:
                message = _compose_threshold_alert(thr, direction, extreme, minutes, timezone, breach_start, breach_end)
                sent = _send(app_user_id, message, {"type": "threshold_alert", "metric": thr.metric_type,
                                                    "direction": direction, "extreme": extreme})
                item["notificationSent"] = bool(sent)
                if sent:
                    thr.last_alert_at_utc = now_utc
                    repository.save_alert_threshold(thr)
        results.append(item)
    return {"appUserId": app_user_id, "timezone": timezone, "results": results}


def _clock_local(value: Optional[dt.datetime]) -> str:
    if not isinstance(value, dt.datetime):
        return ""
    hour, minute = value.hour, value.minute
    period = "AM" if hour < 12 else "PM"
    disp = hour if 1 <= hour <= 12 else (hour - 12 if hour > 12 else 12)
    return f"{disp:02d}:{minute:02d} {period}"


def _compose_threshold_alert(thr: MetricAlertThreshold, direction: str, extreme: Optional[float],
                             minutes: float, timezone: str,
                             breach_start: Optional[dt.datetime] = None,
                             breach_end: Optional[dt.datetime] = None) -> str:
    """Build the alert.

    Frames it as a DETECTED window ("from HH:MM to HH:MM") rather than a real-time
    reading, because the custom logger uploads to ADX only about once an hour, so
    the breach happened earlier than this alert. Then optionally naturalizes the
    wording with the LLM (CHATBOT_THRESHOLD_ALERT_LLM != "0"), with a deterministic
    fallback so a model failure never blocks the alert.
    """
    label = metric_label(thr.metric_type)
    unit = metric_unit(thr.metric_type)
    u = f" {unit}" if unit else ""
    bound = thr.upper if direction == "high" else thr.lower
    word = "above" if direction == "high" else "below"
    ext = f"{extreme:g}{u}" if extreme is not None else "n/a"
    bnd = f"{bound:g}{u}" if bound is not None else "your set limit"
    start_clk, end_clk = _clock_local(breach_start), _clock_local(breach_end)

    if start_clk and end_clk:
        window = f"from {start_clk} to {end_clk}" if start_clk != end_clk else f"around {start_clk}"
    else:
        window = f"for about {int(round(minutes))} minutes"

    facts = (
        f"metric={label}; direction={word} {bnd}; window={window}; "
        f"duration~{int(round(minutes))}min; peak={ext}; "
        f"data syncs about hourly so timing is approximate; not a diagnosis"
    )
    deterministic = (
        f"Heads up - looking at your synced data, your {label} stayed {word} {bnd} {window} "
        f"(about {int(round(minutes))} minutes, peak {ext}). Your readings upload roughly once an hour, "
        f"so this is from the latest sync and the exact timing is approximate. "
        f"This is from your phone sensor and is not a diagnosis; if you feel unwell, rest and, "
        f"if it persists, seek medical advice."
    )
    return _naturalize_alert(deterministic, facts)


def _naturalize_alert(deterministic: str, facts: str) -> str:
    """Optionally rephrase the alert with the LLM for a warmer, less templated tone.

    Enabled unless CHATBOT_THRESHOLD_ALERT_LLM is "0"/"false". Always falls back to
    the deterministic text on any error so alerts never depend on the model."""
    import os
    if os.environ.get("CHATBOT_THRESHOLD_ALERT_LLM", "1").strip().lower() in {"0", "false", "no", "off"}:
        return deterministic
    try:
        prompt = load_prompt(
            "proactive/threshold_monitor/rewrite_threshold_alert.txt"
        ).rstrip("\n").format(
            facts=facts,
            deterministic=deterministic,
        )
        text = generate_alert_text(prompt)
        return text or deterministic
    except Exception as exc:
        logger.warning("threshold alert LLM phrasing failed, using template: %s", exc)
        return deterministic


# ---------------------------------------------------------------------------
# Daily report
# ---------------------------------------------------------------------------
def _daily_summary_query(user_id: str, timezone: str) -> str:
    local_now = f'datetime_utc_to_local(now(), "{timezone}")'
    activity_daily = physical_name(ACTIVITY_DAILY)
    return (
        f"let TargetUserId={_quote(user_id)};\n"
        f"let DayStart=startofday({local_now});\n"
        "let DayEnd=DayStart+1d;\n"
        f"{tabular_call(ACTIVITY_DAILY, 'TargetUserId', 'DayStart', 'DayEnd')}"
        " | where UserId == TargetUserId | where LocalDate == DayStart"
        " | summarize Value=sum(ValueSumExact) by Type, Unit"
        " | project Type, Unit, Value"
    )


def _daily_sleep_query(user_id: str, timezone: str) -> str:
    """Last night's MAIN sleep attributed to today's wake-up."""
    local_now = f'datetime_utc_to_local(now(), "{timezone}")'
    sleep_nightly = physical_name(SLEEP_NIGHTLY)
    return (
        f"let TargetUserId={_quote(user_id)};\n"
        f"let DayStart=startofday({local_now});\n"
        "let DayEnd=DayStart+1d;\n"
        f"{tabular_call(SLEEP_NIGHTLY, 'TargetUserId', 'DayStart', 'DayEnd')}\n"
        "| where IsMainSleep\n"
        "| extend HasAsleepDuration=coalesce(column_ifexists(\"HasAsleepDuration\", bool(null)), isnotnull(AsleepMin) and AsleepMin > 0)\n"
        "| project AsleepMin, AsleepHm, TimeInBedHm, AwakeMin, SleepEfficiencyPct,"
        " DeepPct, RemPct, HasStageBreakdown, HasAsleepDuration,"
        " Onset=tostring(OnsetLocal), Wake=tostring(WakeLocal)\n"
        "| take 1"
    )


def _daily_heart_query(user_id: str, timezone: str) -> str:
    local_now = f'datetime_utc_to_local(now(), "{timezone}")'
    health_samples = physical_name(HEALTH_SAMPLES)
    return (
        f"let TargetUserId={_quote(user_id)};\n"
        f"let DayStart=startofday({local_now});\n"
        "let DayEnd=DayStart+1d;\n"
        f"{tabular_call(HEALTH_SAMPLES, 'TargetUserId', 'DayStart', 'DayEnd')}"
        " | where UserId == TargetUserId"
        " | where Type in ('HEART_RATE','RESTING_HEART_RATE')"
        " | where DateFrom >= DayStart and DateFrom < DayStart + 1d"
        " | summarize HrAvg=avgif(todouble(ValueNumber), Type=='HEART_RATE'),"
        " RhrAvg=avgif(todouble(ValueNumber), Type=='RESTING_HEART_RATE'), N=count()"
    )


def _first_row(rows: Any) -> Optional[Dict[str, Any]]:
    if isinstance(rows, list) and rows and isinstance(rows[0], dict):
        return rows[0]
    return None


def build_daily_report(app_user_id: str, *, repository: Optional[OperationalTableRepository] = None) -> Dict[str, Any]:
    settings = get_settings()
    repository = repository or OperationalTableRepository(fail_soft=True)
    profile = repository.get_user_profile(app_user_id)
    timezone = profile.timezone_iana or settings.default_timezone

    rows = adx_manager.execute_query(_daily_summary_query(app_user_id, timezone), suppress_error_log=True)
    metrics: Dict[str, float] = {}
    if isinstance(rows, list):
        for r in rows:
            if isinstance(r, dict) and r.get("Type"):
                metrics[str(r["Type"]).upper()] = r.get("Value")

    # Sleep + heart are best-effort: only included when the user actually has them.
    sleep: Dict[str, Any] = {}
    try:
        sleep_row = _first_row(adx_manager.execute_query(_daily_sleep_query(app_user_id, timezone), suppress_error_log=True))
        if sleep_row:
            sleep = sleep_row
    except Exception as exc:
        logger.warning("daily sleep fetch failed %s: %s", app_user_id, exc)

    heart: Dict[str, Any] = {}
    try:
        heart_row = _first_row(adx_manager.execute_query(_daily_heart_query(app_user_id, timezone), suppress_error_log=True))
        if heart_row and (heart_row.get("N") or 0):
            heart = heart_row
    except Exception as exc:
        logger.warning("daily heart fetch failed %s: %s", app_user_id, exc)

    # Threshold breaches today (best-effort, reuse the detector without sending).
    breach_eval = evaluate_user_thresholds(app_user_id, repository=repository, send=False)
    breaches = [x for x in (breach_eval.get("results") or []) if x.get("breached")]
    return {
        "appUserId": app_user_id, "timezone": timezone,
        "metrics": metrics, "sleep": sleep, "heart": heart, "breaches": breaches,
    }


def _num(value: Any) -> Optional[float]:
    try:
        return float(value)
    except Exception:
        return None


def _clock(value: Any) -> str:
    """Pull HH:MM AM/PM from a local datetime-ish string."""
    import re
    m = re.search(r"(\d{1,2}):(\d{2})", str(value or ""))
    if not m:
        return ""
    hour, minute = int(m.group(1)), int(m.group(2))
    period = "AM" if hour < 12 else "PM"
    disp = hour if 1 <= hour <= 12 else (hour - 12 if hour > 12 else 12)
    return f"{disp:02d}:{minute:02d} {period}"


def _daily_assessment(metrics: Dict[str, Any], sleep: Dict[str, Any]) -> Tuple[str, str]:
    """Short data-based read + a tonight/tomorrow tip, using only collected data."""
    read_parts: List[str] = []
    tip = ""
    asleep_min = _num(sleep.get("AsleepMin")) if sleep else None
    if asleep_min is not None:
        eff = _num(sleep.get("SleepEfficiencyPct"))
        if asleep_min < 420:
            read_parts.append(
                f"you slept {sleep.get('AsleepHm') or f'{int(asleep_min)} min'}, below the 7-9 hour recommendation"
                + (f" with {eff:g}% efficiency" if eff is not None else "")
            )
            tip = "Tonight, aim for an earlier lights-out and a 7-9 hour window; keep caffeine to the morning and wind down without screens."
        else:
            read_parts.append(
                f"you slept {sleep.get('AsleepHm') or f'{int(asleep_min)} min'}, within a healthy range"
                + (f" ({eff:g}% efficiency)" if eff is not None else "")
            )
            tip = "Tonight, keep your schedule consistent to maintain this pattern."
    steps = _num(metrics.get("STEPS"))
    if steps is not None:
        if steps >= 10000:
            read_parts.append(f"your activity was strong at {steps:,.0f} steps")
        elif steps >= 5000:
            read_parts.append(f"your activity was moderate at {steps:,.0f} steps")
        else:
            read_parts.append(f"your activity was light at {steps:,.0f} steps")
            if not tip:
                tip = "Tomorrow, a short walk or two could lift your daily activity."
    if not read_parts:
        return "", ""
    read = "Today's read: " + "; ".join(read_parts) + "."
    return read, (tip if tip else "")


def _data_lines(metrics: Dict[str, Any], sleep: Dict[str, Any], heart: Dict[str, Any]) -> List[str]:
    """Factual bullet lines for the metrics the user actually has today."""
    pretty = {
        "STEPS": ("Steps", ""), "DISTANCE_DELTA": ("Distance", "m"),
        "DISTANCE_WALKING_RUNNING": ("Distance", "m"),
        "TOTAL_CALORIES_BURNED": ("Total calories", "kcal"),
        "ACTIVE_ENERGY_BURNED": ("Active calories", "kcal"),
    }
    data_lines: List[str] = []
    if metrics:
        for mt, val in sorted(metrics.items()):
            label, unit = pretty.get(mt, (metric_label(mt).capitalize(), metric_unit(mt)))
            try:
                v = f"{float(val):,.0f}"
            except Exception:
                v = str(val)
            data_lines.append(f"- {label}: {v}{(' ' + unit) if unit else ''}")
    if sleep:
        onset, wake = _clock(sleep.get("Onset")), _clock(sleep.get("Wake"))
        asleep_min = _num(sleep.get("AsleepMin"))
        has_asleep_duration = (
            str(sleep.get("HasAsleepDuration")).strip().lower()
            in {"true", "1"}
            if "HasAsleepDuration" in sleep
            else asleep_min is not None
        )
        asleep = sleep.get("AsleepHm") or (
            f"{int(asleep_min)} min" if asleep_min is not None else ""
        )
        in_bed = sleep.get("TimeInBedHm") or ""
        eff = _num(sleep.get("SleepEfficiencyPct"))
        awake = _num(sleep.get("AwakeMin"))
        detail = (
            f"- Sleep: {asleep} asleep"
            if has_asleep_duration and asleep
            else "- Sleep: actual asleep duration unavailable"
        )
        if onset and wake:
            in_bed_suffix = f", {in_bed}" if in_bed else ""
            detail += f" (in bed {onset}-{wake}{in_bed_suffix}"
            if has_asleep_duration and awake is not None:
                detail += f", awake ~{int(awake)} min"
            detail += ")"
        if has_asleep_duration and eff is not None:
            detail += f", efficiency {eff:g}%"
        data_lines.append(detail)
        if str(sleep.get("HasStageBreakdown")).lower() in {"true", "1"}:
            dp, rp = _num(sleep.get("DeepPct")), _num(sleep.get("RemPct"))
            if dp is not None and rp is not None:
                data_lines.append(f"- Sleep stages: deep {dp:g}%, REM {rp:g}%")
    if heart:
        hr, rhr = _num(heart.get("HrAvg")), _num(heart.get("RhrAvg"))
        if hr is not None:
            line = f"- Heart rate: avg {hr:.0f} bpm"
            if rhr is not None:
                line += f", resting {rhr:.0f} bpm"
            data_lines.append(line)
    return data_lines


_COACH_SYSTEM_PROMPT, _COACH_USER_PROMPT = load_role_prompt(
    "proactive/threshold_monitor/daily_report_coach.txt"
)


def _report_part_of_day(report: Dict[str, Any]) -> str:
    try:
        tz = report.get("timezone") or get_settings().default_timezone
        hour = dt.datetime.now(ZoneInfo(tz)).hour
    except Exception:
        hour = 12
    if 5 <= hour < 12:
        return "morning"
    if 12 <= hour < 17:
        return "afternoon"
    if 17 <= hour < 22:
        return "evening"
    return "night"


def _coach_narrative(report: Dict[str, Any], data_lines: List[str]) -> str:
    """LLM health-coach paragraph grounded in today's data + time of day. Returns ''
    on any failure so the caller can fall back to the deterministic assessment."""
    if not data_lines:
        return ""
    breaches = report.get("breaches") or []
    brief = list(data_lines)
    if breaches:
        brief.append("- Threshold alerts today: " + ", ".join(
            f"{metric_label(b.get('metric', '')).capitalize()} {b.get('direction', '')}" for b in breaches))
    else:
        brief.append("- Threshold alerts today: none")
    user_msg = _COACH_USER_PROMPT.format(
        part_of_day=_report_part_of_day(report),
        data_lines="\n".join(brief),
    )
    try:
        out = complete_chat(
            [{"role": "system", "content": _COACH_SYSTEM_PROMPT},
             {"role": "user", "content": user_msg}],
            temperature=0,
        )
        return str(out or "").strip()
    except Exception as exc:
        logger.warning("daily coach narrative failed %s: %s", report.get("appUserId"), exc)
        return ""


def format_daily_report(report: Dict[str, Any]) -> str:
    metrics = report.get("metrics") or {}
    sleep = report.get("sleep") or {}
    heart = report.get("heart") or {}
    breaches = report.get("breaches") or []

    # 1) Collected data, listed simply (only what the user actually has today).
    data_lines = _data_lines(metrics, sleep, heart)
    data_block = "\n".join(data_lines) if data_lines else "- No phone health data recorded yet today."

    # 2) Health-coach narrative (LLM); fall back to the deterministic read + tip.
    coach = _coach_narrative(report, data_lines)
    if not coach:
        read, tip = _daily_assessment(metrics, sleep)
        coach = (read + (("\n\n" + tip) if tip else "")) if read else ""

    # 3) Threshold status — shown ONLY when something was breached. Threshold
    #    crossings already push their own real-time alert, so a "nothing was
    #    exceeded" line is redundant noise in the daily summary (product feedback).
    thr = ""
    if breaches:
        thr = "Threshold alerts today:\n" + "\n".join(
            f"- {metric_label(b.get('metric', '')).capitalize()}: "
            f"{b.get('direction', '')} for ~{int(round(float(b.get('sustainedMinutes') or 0)))} min"
            for b in breaches)

    # Sections separated by blank lines for readability.
    sections = ["Here is your daily health summary.\n\n" + data_block]
    if coach:
        sections.append(coach)
    if thr:
        sections.append(thr)
    return "\n\n".join(sections)


def _daily_followup_plan(report: Dict[str, Any]) -> Dict[str, Any]:
    """A monitoring plan seeded into the daily-summary episode so a follow-up
    ("show more details") queries the summarized data over the last 2 days
    (covering the report day whether asked that evening or the next morning),
    instead of a fresh, empty current day."""
    keymap = {
        "STEPS": "phone_steps", "DISTANCE_DELTA": "phone_distance",
        "DISTANCE_WALKING_RUNNING": "phone_distance",
        "TOTAL_CALORIES_BURNED": "phone_total_calories",
        "ACTIVE_ENERGY_BURNED": "phone_active_calories",
    }
    metrics: List[str] = []
    for mt in (report.get("metrics") or {}):
        key = keymap.get(str(mt).upper())
        if key and key not in metrics:
            metrics.append(key)
    if report.get("sleep") and "phone_sleep_duration" not in metrics:
        metrics.append("phone_sleep_duration")
    if report.get("heart") and "phone_heart_rate" not in metrics:
        metrics.append("phone_heart_rate")
    if not metrics:
        metrics = ["phone_steps"]
    return {
        "case_type": "monitoring",
        "metrics": metrics,
        "time_range": {"mode": "relative", "value": 2, "unit": "day", "anchor": "rolling"},
        "timezone_iana": report.get("timezone") or "",
        "comparison_requested": False,
    }


def send_daily_report(app_user_id: str, *, repository: Optional[OperationalTableRepository] = None) -> bool:
    report = build_daily_report(app_user_id, repository=repository)
    message = format_daily_report(report)
    return _send(app_user_id, message, {"type": "daily_report", "followup_plan": _daily_followup_plan(report)})


# ---------------------------------------------------------------------------
# Telegram send (reuses proactive episode integration)
# ---------------------------------------------------------------------------
def _send(app_user_id: str, message: str, payload: Dict[str, Any]) -> bool:
    try:
        from functions.shared import get_chat_id_by_user, send_telegram_message
        chat_id = get_chat_id_by_user(app_user_id)
        if not chat_id:
            return False
        send_telegram_message(chat_id, message)
        try:
            if get_settings().proactive_episode_interrupt_enabled:
                from chatbot.episode_service import suspend_active_episode_for_alert, thread_id_for_chat
                suspend_active_episode_for_alert(
                    app_user_id, thread_id_for_chat(chat_id),
                    alert_context=payload, alert_message_text=message,
                )
        except Exception as exc:
            logger.warning("threshold/report episode interrupt skipped %s: %s", app_user_id, exc)
        try:
            from functions.telegram_conversation_log import build_conversation_event, stage_conversation_event
            stage_conversation_event(build_conversation_event(
                event_type=str(payload.get("type") or "proactive_alert"),
                role="assistant", direction="outbound", event_time_utc=utc_now(),
                app_user_id=app_user_id, telegram_chat_id=chat_id, message_text=message,
                route="threshold_monitor", intent="proactive_alert", execution_status="sent",
            ))
        except Exception:
            pass
        return True
    except Exception as exc:
        logger.warning("threshold/report send failed %s: %s", app_user_id, exc)
        return False
