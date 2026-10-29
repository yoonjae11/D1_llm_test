"""Pure response-formatting and text-polishing policy."""

import datetime
import re
from typing import Any, Dict, List, Optional

from chatbot.policies.domain_catalog import METRIC_DEFINITIONS
from chatbot.policies.kql_generation import (
    is_conversation_history_request,
    metric_result_alias,
)
from chatbot.policies.question_analysis import _is_comparison_question
from chatbot.policies.time_ranges import _compute_baseline_bounds, compute_window_bounds
from chatbot.policies.metric_normalization import _normalize_metrics
from chatbot.policies.result_processing import _is_meaningless_value


def _human_metric_name(metric: str) -> str:
    raw = metric.strip().lower()
    labels = {
        "total_sleep_time": "total sleep time",
        "sleep_score": "sleep score",
        "sleep_efficiency": "sleep efficiency",
        "sleep_start_local": "sleep start time",
        "sleep_end_local": "wake-up time",
        "heart_rate": "heart rate",
        "rmssd": "RMSSD",
        "sdnn1": "SDNN1",
        "phone_hrv_rmssd": "Phone RMSSD",
        "phone_hrv_sdnn": "Phone SDNN",
        "steps": "steps",
        "distance": "distance",
        "calories": "calories",
        "workout_count": "workout count",
        "workout_calories": "workout calories",
        "phone_steps": "smartphone steps",
        "phone_distance": "smartphone distance",
        "phone_active_calories": "smartphone active calories",
        "phone_total_calories": "smartphone total calories",
        "phone_heart_rate": "smartphone heart rate",
        "phone_resting_heart_rate": "smartphone resting heart rate",
        "phone_weight": "smartphone weight",
        "phone_bmi": "smartphone BMI",
        "phone_blood_pressure_systolic": "smartphone systolic blood pressure",
        "phone_blood_pressure_diastolic": "smartphone diastolic blood pressure",
        "phone_blood_glucose": "smartphone blood glucose",
        "phone_body_temperature": "smartphone body temperature",
        "phone_birth_date": "smartphone birth date",
        "phone_gender": "smartphone gender",
        "phone_blood_type": "smartphone blood type",
        "phone_sleep_duration": "smartphone sleep duration",
        "phone_sleep_start_local": "smartphone sleep start time",
        "phone_sleep_end_local": "smartphone wake-up time",
        "phone_sleep_efficiency": "smartphone sleep efficiency",
        "phone_deep_sleep": "smartphone deep sleep",
        "phone_rem_sleep": "smartphone REM sleep",
        "phone_light_sleep": "smartphone light sleep",
        "phone_sleep_sessions": "smartphone sleep sessions",
        "phone_workout_count": "smartphone workout count",
        "phone_workout_duration": "smartphone workout duration",
    }
    return labels.get(raw, raw.replace("_", " "))


def _date_span_text(start: datetime.date, end: datetime.date) -> str:
    if start == end:
        return start.isoformat()
    return f"{start.isoformat()} to {end.isoformat()}"


def _build_analysis_footer(plan: Dict[str, Any], question: str = "") -> str:
    if not isinstance(plan, dict):
        return ""
    # Conversation-history answers ("what did I ask earlier?") are not metric
    # analyses — they read the chat log, so a "Metrics used: ..." footer (carried
    # over from a prior plan's metrics) is misleading. Omit the footer entirely.
    if is_conversation_history_request(question, plan):
        return ""
    # A latest-sample answer already includes the exact value and recorded
    # timestamp. A broad date-range/metrics footer makes that point observation
    # look like a period analysis and was the source of confusing output such as
    # "Analysis scope: 14 days" beneath a single current heart-rate reading.
    if bool(plan.get("latest_sample")):
        return ""
    time_range = plan.get("time_range", {}) if isinstance(plan.get("time_range"), dict) else {}
    metrics = _normalize_metrics(plan.get("metrics", []) if isinstance(plan.get("metrics"), list) else [])
    case_type = str(plan.get("case_type", "monitoring")).lower()
    timezone_iana = str(plan.get("timezone_iana") or "")

    # A one-day factual answer is self-explanatory. Reserve the analysis footer
    # for comparisons, interpretation/action cases, and genuinely multi-day
    # analyses instead of adding two metadata lines to every simple value.
    period_value = int(time_range.get("value", 1) or 1)
    # A valid planner decision is authoritative. Keyword recovery is only for
    # older/malformed plans that omitted the flag, not an override of False.
    comparison_flag = plan.get("comparison_requested")
    comparison_requested = (
        comparison_flag if isinstance(comparison_flag, bool)
        else bool(question and _is_comparison_question(question))
    )
    if (
        case_type == "monitoring"
        and not comparison_requested
        and period_value <= 1
    ):
        return ""

    # Exclude profile-only metrics from footer display (they aren't health analysis metrics).
    _PROFILE_ONLY = {"phone_blood_type", "phone_gender", "phone_birth_date"}
    display_metrics = [m for m in metrics if m not in _PROFILE_ONLY]

    current_start, current_end = compute_window_bounds(time_range, timezone_iana)
    current_span = _date_span_text(current_start, current_end)
    metric_text = ", ".join(_human_metric_name(metric) for metric in display_metrics[:6]) if display_metrics else "sleep and activity metrics"

    # Interpretation/action labels alone do not establish a comparison. A
    # footer must not claim a baseline merely because of the response style.
    if comparison_requested:
        baseline_start, baseline_end = _compute_baseline_bounds(time_range, timezone_iana)
        baseline_span = _date_span_text(baseline_start, baseline_end)
        return (
            f"Analysis scope: {current_span}, compared with baseline {baseline_span}.\n"
            f"Metrics used: {metric_text}."
        )
    return (
        f"Analysis scope: {current_span}.\n"
        f"Metrics used: {metric_text}."
    )


def _to_plain_text_answer(text: str) -> str:
    if not text:
        return ""
    lines: List[str] = []
    for raw in text.replace("\r\n", "\n").split("\n"):
        line = raw.strip()
        if not line:
            if lines and lines[-1] != "":
                lines.append("")
            continue
        line = line.replace("**", "").replace("__", "")
        line = re.sub(r"^\s*[-*]\s+", "", line)
        line = re.sub(
            r"^(?:Answer|Interpretation|Suggestion|Follow-?up Question)\s*:\s*",
            "",
            line,
            flags=re.IGNORECASE,
        )
        lines.append(line)
    normalized = "\n".join(lines).strip()
    return re.sub(r"\n{3,}", "\n\n", normalized)


def _capitalize_line_starts(text: str) -> str:
    if not text:
        return ""
    lines: List[str] = []
    for raw in text.split("\n"):
        if not raw.strip():
            lines.append(raw)
            continue
        stripped = raw.lstrip()
        if stripped.lower().startswith(("analysis scope:", "metrics used:")):
            lines.append(raw)
            continue
        prefix_len = len(raw) - len(stripped)
        chars = list(stripped)
        # Only capitalize when the line genuinely STARTS with a letter (a
        # sentence). Do NOT hunt for the first letter after a number — that
        # wrongly produced "55.3 Bpm", "11,424 Vs", "13,218 Steps/day" on
        # data lines that begin with a value.
        if chars and chars[0].isalpha():
            chars[0] = chars[0].upper()
        lines.append(raw[:prefix_len] + "".join(chars))
    return "\n".join(lines)


def _polish_answer_text(text: str) -> str:
    polished = _to_plain_text_answer(text)
    polished = (
        polished.replace("\u2018", "'")
        .replace("\u2019", "'")
        .replace("\u201c", "\"")
        .replace("\u201d", "\"")
        .replace("\u2014", "-")
        .replace("\u2013", "-")
    )
    polished = _capitalize_line_starts(polished)
    return re.sub(r"\n{3,}", "\n\n", polished).strip()


def _append_followup(text: str, followup_question: str) -> str:
    if not followup_question:
        return text
    if followup_question in text:
        return text
    return text.rstrip() + "\n\n" + followup_question


def normalize_followup_text(value: str) -> str:
    """Normalize a follow-up suggestion for deterministic duplicate checks."""

    normalized = re.sub(r"[\W_]+", " ", str(value or "").lower(), flags=re.UNICODE)
    return re.sub(r"\s+", " ", normalized).strip()


def is_redundant_followup(last_assistant_text: str, suggestion: str) -> bool:
    """Return whether the preceding answer already contains the suggestion."""

    if not suggestion:
        return False
    last_normalized = normalize_followup_text(last_assistant_text)
    suggestion_normalized = normalize_followup_text(suggestion)
    if not last_normalized or not suggestion_normalized:
        return False
    return suggestion_normalized in last_normalized


def append_non_redundant_followup(
    text: str,
    suggestion: str,
    last_assistant_text: str,
) -> str:
    """Append a follow-up unless it was already offered in the previous answer."""

    if is_redundant_followup(last_assistant_text, suggestion):
        return text
    return _append_followup(text, suggestion)


# Trailing "offer" questions the model tends to tack on to every answer
# (e.g. "Would you like ...?" or "Want me to ...?").
# We strip these when no follow-up was actually requested, so a clear,
# self-contained answer does not end with an unsolicited question.
_OFFER_PREFIXES = (
    "would you like",
    "want me to",
    "do you want",
    "should i ",
    "shall i ",
    "would you prefer",
    "do you want me to",
    "let me know if",
    "if you want",
)
_OFFER_SENTENCE_PREFIXES = (
    "if you'd like",
    "if you would like",
    "if you want",
    "let me know if",
)
def _is_offer_paragraph(paragraph: str) -> bool:
    p = paragraph.strip()
    if not p:
        return False
    low = p.lower()
    if low.startswith(_OFFER_PREFIXES) and p.rstrip().endswith("?"):
        return True
    if low.startswith(_OFFER_SENTENCE_PREFIXES):
        return True
    return False


_FOOTER_LINE_RE = re.compile(r"^\s*(analysis scope|metrics used)\s*:", re.IGNORECASE)


def _strip_llm_footer(text: str) -> str:
    """Remove footer lines the model generated despite instructions.

    The deterministic footer (Analysis scope / Metrics used) is appended by
    code exactly once; any model-written copies would duplicate it."""
    if not text:
        return text
    lines = [line for line in text.split("\n") if not _FOOTER_LINE_RE.match(line)]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _strip_trailing_offer_question(text: str) -> str:
    """Remove a trailing unsolicited offer/follow-up question from an answer.

    Keeps the analytic body intact; only drops trailing paragraphs that are
    pure offer-style questions equivalent to "Would you like ...?" or "Want me
    to ...?" Never touches the Analysis scope / Metrics used footer (handled
    separately, appended later).
    """
    if not text:
        return text
    paragraphs = [p for p in re.split(r"\n\s*\n", text.strip())]
    while paragraphs and _is_offer_paragraph(paragraphs[-1]):
        paragraphs.pop()
    if paragraphs:
        last = paragraphs[-1].rstrip()
        pieces = re.split(r"(?<=[.!?])\s+", last)
        if len(pieces) > 1 and pieces[-1].strip().lower().startswith(_OFFER_SENTENCE_PREFIXES):
            paragraphs[-1] = " ".join(pieces[:-1]).rstrip()
            if not paragraphs[-1]:
                paragraphs.pop()
    return "\n\n".join(paragraphs).strip()


def _source_label(source: str) -> str:
    value = str(source or "").strip().lower()
    if value in {
        "smartphone",
        "phone",
        "unified",
        "connected_health",
        "healthdataflat",
        "health_data_flat",
    }:
        # HealthDataFlat is populated by the Custom Logger path and can carry
        # phone, watch, ring, or other connected-source records. Do not label
        # that unified ingestion path as if every observation came from the
        # handset itself.
        return "connected health data"
    if value == "withings":
        return "Withings"
    return value or "available source"


def _is_numeric_like(value: Any) -> bool:
    try:
        num = float(value)
        return not (num != num)  # NaN check
    except Exception:
        return False


def _format_metric_value(value: Any) -> str:
    if _is_numeric_like(value):
        num = float(value)
        if abs(num - round(num)) < 1e-9:
            return f"{int(round(num)):,}"
        return f"{num:,.2f}".rstrip("0").rstrip(".")
    return str(value)


def _format_hours_with_hm(value: Any) -> Optional[str]:
    if not _is_numeric_like(value):
        return None
    hours = float(value)
    if hours < 0:
        return None
    hours_text = f"{hours:,.2f}".rstrip("0").rstrip(".")
    total_minutes = int(round(hours * 60.0))
    hour_part, minute_part = divmod(total_minutes, 60)
    if hour_part > 0 and minute_part > 0:
        hm_text = f"{hour_part}h {minute_part}m"
    elif hour_part > 0:
        hm_text = f"{hour_part}h"
    else:
        hm_text = f"{minute_part}m"
    return f"{hours_text} hours ({hm_text})"


def _format_metric_value_for_metric(metric: str, value: Any) -> str:
    metric_key = str(metric or "").strip().lower()
    if metric_key in {
        "total_sleep_time",
        "phone_sleep_duration",
        "phone_deep_sleep",
        "phone_rem_sleep",
        "phone_light_sleep",
        "time_in_bed",
        "light_sleep",
        "deep_sleep",
        "rem_sleep",
        "phone_workout_duration",
    }:
        hours_text = _format_hours_with_hm(value)
        if hours_text:
            return hours_text
    return _format_metric_value(value)


def _display_metric_value(metric: str, value: Any) -> str:
    """Add only units whose normalized metric contract is unambiguous."""

    text = _format_metric_value_for_metric(metric, value)
    metric_key = str(metric or "").strip().lower()
    if metric_key in {"heart_rate", "phone_heart_rate", "phone_resting_heart_rate"}:
        return f"{text} bpm"
    if metric_key in {"steps", "phone_steps"}:
        return f"{text} steps"
    if metric_key in {"sleep_efficiency", "phone_sleep_efficiency"}:
        return f"{text}%"
    return text


def _extract_date_label(dt_string: str) -> str:
    """Extract a human-readable date like 'February 24' from a datetime string."""
    date_match = re.search(r"(\d{4})-(\d{2})-(\d{2})", str(dt_string))
    if date_match:
        try:
            d = datetime.date(int(date_match.group(1)), int(date_match.group(2)), int(date_match.group(3)))
            return d.strftime("%B %d").replace(" 0", " ")  # "February 24"
        except ValueError:
            pass
    return ""


_SLEEP_METRICS = {
    "phone_sleep_duration",
    "phone_sleep_start_local",
    "phone_sleep_end_local",
    "phone_sleep_efficiency",
    "phone_deep_sleep",
    "phone_rem_sleep",
    "phone_light_sleep",
    "sleep_start_local",
    "sleep_end_local",
    "total_sleep_time",
    "phone_sleep_sessions",
}


def _is_sleep_metric_set(metrics: List[str]) -> bool:
    return any(m in _SLEEP_METRICS for m in metrics)


def _short_metric_name(metric: str) -> str:
    """Return a concise metric label when the source is shown separately."""
    name = _human_metric_name(metric)
    if name.startswith("smartphone "):
        name = name[len("smartphone "):]
    return name


def _clock_and_date(value: Any) -> Optional[Dict[str, str]]:
    """Parse a local datetime-ish value into a 12-hour clock + readable date."""
    if not value:
        return None
    s = str(value)
    m = re.search(r"(\d{1,2}):(\d{2})", s)
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2))
    period = "AM" if hour < 12 else "PM"
    display_hour = hour if 1 <= hour <= 12 else (hour - 12 if hour > 12 else 12)
    return {"clock": f"{display_hour:02d}:{minute:02d} {period}", "date": _extract_date_label(s)}


def _recorded_at_text(value: Any) -> Optional[str]:
    """Format a local measurement timestamp without treating it as UTC."""

    parsed = _clock_and_date(value)
    if not parsed:
        return None
    date_suffix = f" on {parsed['date']}" if parsed.get("date") else ""
    return f"{parsed['clock']}{date_suffix}"


def _build_latest_sample_answer(
    plan: Dict[str, Any],
    data_rows: Any,
    question: str = "",
) -> Optional[str]:
    """Build a point-observation answer from value + measurement timestamp."""

    if not bool(plan.get("latest_sample")):
        return None
    if not isinstance(data_rows, list) or len(data_rows) != 1:
        return None
    row = data_rows[0]
    if not isinstance(row, dict):
        return None

    metrics = _normalize_metrics(
        plan.get("metrics", []) if isinstance(plan.get("metrics"), list) else []
    )
    if len(metrics) != 1:
        return None
    metric = metrics[0]
    alias = metric_result_alias(METRIC_DEFINITIONS.get(metric, {}).get("summary", ""))
    if not alias:
        return None

    value = row.get(alias)
    recorded_at = _recorded_at_text(row.get(f"{alias}_recorded_at_local"))
    source = str(row.get(f"{alias}_source", "")).strip().lower()
    source_suffix = ""
    if source in {"smartphone", "withings"}:
        source_suffix = f" ({_source_label(source)})"
    focus = str(plan.get("answer_focus") or "value_and_time").strip().lower()
    metric_name = _short_metric_name(metric)

    if focus == "measurement_time":
        if recorded_at:
            return (
                f"Your latest {metric_name} measurement was recorded at "
                f"{recorded_at}{source_suffix}."
            )
        return f"The latest {metric_name} record does not include a measurement time{source_suffix}."

    if _is_meaningless_value(value):
        return None
    value_text = _format_metric_value_for_metric(metric, value)
    unit = str(row.get("Unit") or "").strip()
    if metric in {"heart_rate", "phone_heart_rate", "phone_resting_heart_rate"}:
        unit = "bpm"
    display_value = f"{value_text} {unit}".strip()

    if recorded_at:
        return (
            f"Your latest recorded {metric_name} was {display_value} at "
            f"{recorded_at}{source_suffix}."
        )
    return (
        f"Your latest recorded {metric_name} was {display_value}{source_suffix}; "
        "its measurement time was not available."
    )


def _build_source_aware_monitoring_answer(plan: Dict[str, Any], data_rows: Any, question: str = "") -> Optional[str]:
    case_type = str(plan.get("case_type", "monitoring")).lower()
    if case_type != "monitoring":
        return None
    if not isinstance(data_rows, list) or not data_rows:
        return None
    # This fast path summarizes a SINGLE value (e.g. "today's steps"). For
    # multi-row results (a week of sleep, a trend, a period comparison) it must
    # NOT fire — otherwise it collapses the whole series to row[0], e.g. reporting
    # only the first night's sleep for "is my sleep better than last week?".
    if len(data_rows) != 1:
        return None
    if bool(plan.get("comparison_requested", False)):
        return None
    row = data_rows[0]
    if not isinstance(row, dict):
        return None
    if not any(str(key).endswith("_source") for key in row.keys()):
        return None

    metrics = _normalize_metrics(plan.get("metrics", []) if isinstance(plan.get("metrics"), list) else [])
    if not metrics:
        return None
    # Sleep questions use the grounded data-answer prompt, which distinguishes
    # wake time, bedtime, duration, and quality. Never collapse them to this
    # generic single-value composer.
    if _is_sleep_metric_set(metrics):
        return None

    latest_sample_answer = _build_latest_sample_answer(plan, data_rows, question=question)
    if latest_sample_answer:
        return latest_sample_answer

    sentences: List[str] = []
    q = str(question or "").lower()
    source_requested = any(
        token in q
        for token in ("source", "withings", "smartphone", "phone", "mobile")
    )
    time_range = plan.get("time_range", {}) if isinstance(plan.get("time_range"), dict) else {}
    anchor = str(time_range.get("anchor") or "").lower()
    for metric in metrics:
        alias = metric_result_alias(METRIC_DEFINITIONS.get(metric, {}).get("summary", ""))
        if not alias:
            continue
        value = row.get(alias)
        if _is_meaningless_value(value):
            continue
        source = str(row.get(f"{alias}_source", "")).lower()
        value_text = _display_metric_value(metric, value)
        metric_name = _human_metric_name(metric)
        if metric == "steps":
            metric_name = "step count"
        elif metric == "phone_steps":
            metric_name = "smartphone step count"
        if "average" in q or re.search(r"\bavg\b", q):
            metric_name = f"average {metric_name}"

        source_suffix = ""
        is_phone_fallback = source == "smartphone" and not metric.startswith("phone_")
        if source in {"smartphone", "withings"} and (
            source_requested or is_phone_fallback
        ):
            source_suffix = f" ({_source_label(source)} source)"

        if anchor == "today":
            sentences.append(
                f"So far today, your {metric_name} is {value_text}{source_suffix}."
            )
        elif anchor == "yesterday":
            sentences.append(
                f"Yesterday, your {metric_name} was {value_text}{source_suffix}."
            )
        else:
            sentences.append(f"Your {metric_name} is {value_text}{source_suffix}.")

    if not sentences:
        return None
    return " ".join(sentences)
