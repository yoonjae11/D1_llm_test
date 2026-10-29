"""Pure metric normalization and language helpers."""

from __future__ import annotations

import re
from typing import List

from .domain_catalog import (
    METRIC_ALIASES,
    METRIC_DEFINITIONS,
    PHONE_TO_WITHINGS_METRIC_PRIORITY,
    SMARTPHONE_SOURCE_KEYWORDS,
    WITHINGS_PHONE_SLEEP_FALLBACK_METRICS,
    WITHINGS_TO_PHONE_METRIC_PRIORITY,
)


def _is_phone_metric(metric: str) -> bool:
    """Return whether the canonical metric explicitly requests Phone data."""

    return str(metric or "").startswith("phone_")


def _has_phone_metric(metrics: List[str]) -> bool:
    return any(_is_phone_metric(metric) for metric in metrics)


def _has_non_phone_metric(metrics: List[str]) -> bool:
    return any(not _is_phone_metric(metric) for metric in metrics)


def _mentions_smartphone_source(question: str) -> bool:
    q = (question or "").lower()
    return any(keyword in q for keyword in SMARTPHONE_SOURCE_KEYWORDS)


def _mentions_both_sources(question: str) -> bool:
    q = (question or "").lower()
    # "both" by itself can refer to two metrics or two periods.  Only create a
    # source comparison when the user actually names Withings and a phone-side
    # source; otherwise the source-neutral curated route is authoritative.
    return _mentions_withings_source(q) and _mentions_smartphone_source(q)


def _mentions_withings_source(question: str) -> bool:
    q = (question or "").lower()
    return "withings" in q


def _apply_source_scope(metrics: List[str], question: str) -> List[str]:
    """Normalize explicit source wording without changing generic semantics.

    Unprefixed metrics use the curated HealthDataFlat-derived layer. Historical
    ``phone_*`` names are retained for explicit mobile wording and compatibility.
    Equivalent provider-specific pairs are not synthesized because the core
    contract exposes one source-resolved value; a generic question never
    acquires a direct vendor route.
    """

    if not metrics:
        return []

    ordered: List[str] = []
    for metric in metrics:
        if metric not in ordered:
            ordered.append(metric)

    explicit_both = _mentions_both_sources(question)
    explicit_phone = _mentions_smartphone_source(question)

    # A "phone/mobile/smartphone steps" question can reach this policy with an
    # unprefixed metric from the LLM. Resolve it to the historical ``phone_*``
    # compatibility name. An explicit phone_* metric is already authoritative.
    # If the planner already identified an unrelated explicit ``phone_*``
    # metric alongside a generic metric (for example ``steps`` +
    # ``phone_weight``), "phone" in the question qualifies that explicit
    # metric rather than globally converting the generic one.
    mixed_explicit_phone_metrics = _has_phone_metric(ordered) and _has_non_phone_metric(ordered)
    if explicit_phone and not explicit_both and not mixed_explicit_phone_metrics:
        converted: List[str] = []
        for metric in ordered:
            phone_metric = WITHINGS_TO_PHONE_METRIC_PRIORITY.get(metric)
            if not phone_metric and metric in WITHINGS_PHONE_SLEEP_FALLBACK_METRICS:
                # PhoneSleepNights provides the duration, stage, efficiency,
                # and bedtime/wake fields behind the explicit Phone sleep path.
                phone_metric = "phone_sleep_duration"
            resolved = phone_metric or metric
            if resolved not in converted:
                converted.append(resolved)
        ordered = converted

    if explicit_both:
        # The unified catalog does not expose two equivalent provider-specific
        # series. Do not manufacture a ``phone_*`` pair that would resolve to
        # the same curated resource and look like a real provider comparison.
        return ordered

    # For a normal source-neutral question, keep the generic curated metric and
    # discard a paired compatibility copy if the LLM emitted both.
    generic_metrics = set(ordered)
    result: List[str] = []
    for metric in ordered:
        paired_generic = PHONE_TO_WITHINGS_METRIC_PRIORITY.get(metric)
        if paired_generic and paired_generic in generic_metrics:
            continue
        if metric not in result:
            result.append(metric)
    return result


def _apply_withings_source_priority(metrics: List[str], question: str) -> List[str]:
    """Compatibility alias for the former Withings-primary policy helper."""

    return _apply_source_scope(metrics, question)


def _normalize_metric_name(metric: str) -> str:
    value = str(metric or "").strip().lower()
    value = value.replace("-", "_").replace(" ", "_")
    value = value.replace(".", "_")
    value = re.sub(r"_+", "_", value).strip("_")
    if ":" in value:
        value = value.split(":", 1)[-1].strip()
    return METRIC_ALIASES.get(value, value)


def _normalize_metrics(metrics: List[str]) -> List[str]:
    normalized: List[str] = []
    for metric in metrics:
        item = _normalize_metric_name(metric)
        if item in METRIC_DEFINITIONS and item not in normalized:
            normalized.append(item)
    return normalized


def _contains_hangul(text: str) -> bool:
    """Compatibility helper retained for callers in the English-only runtime."""

    return False


def _question_language(question: str) -> str:
    return "en"


def _default_followup_question(case_type: str) -> str:
    if case_type in {"factor_analysis", "prescription"}:
        return "Would you like me to compare this with another period or include more metrics?"
    return "Would you like me to check a different period as well?"


def _suggest_followup_action(case_type: str, metrics: list) -> str:
    """Generate a short follow-up suggestion based on the case type and metrics."""
    sleep_metrics = {
        "total_sleep_time",
        "sleep_score",
        "sleep_efficiency",
        "phone_sleep_duration",
        "sleep_start_local",
        "sleep_end_local",
        "deep_sleep",
        "rem_sleep",
        "light_sleep",
    }
    activity_metrics = {
        "steps",
        "phone_steps",
        "distance",
        "phone_distance",
        "calories",
        "phone_active_calories",
        "workout_count",
        "phone_workout_count",
    }
    heart_metrics = {
        "heart_rate",
        "phone_heart_rate",
        "phone_resting_heart_rate",
        "rmssd",
        "sdnn1",
    }

    has_sleep = any(m in sleep_metrics for m in metrics)
    has_activity = any(m in activity_metrics for m in metrics)
    has_heart = any(m in heart_metrics for m in metrics)

    if case_type == "monitoring":
        if has_sleep and not has_activity:
            return "Want me to check other nights in the past week?"
        if has_activity and not has_sleep:
            return "Want me to compare this with your recent 7-day average?"
        if has_heart:
            return "Want me to show your heart rate trend for the past week?"
        return "Want me to check a different day or compare with last week?"
    if case_type == "factor_analysis":
        if has_sleep and has_activity:
            return "Want me to suggest ways to improve your sleep or activity?"
        if has_sleep:
            return "Want me to look into factors that might be affecting your sleep?"
        return "Want me to dig deeper into any specific metric?"
    if case_type == "prescription":
        return "Want me to track your progress on these suggestions next week?"
    return "Want me to check a different period as well?"


__all__ = [
    "_apply_source_scope",
    "_apply_withings_source_priority",
    "_contains_hangul",
    "_default_followup_question",
    "_has_non_phone_metric",
    "_has_phone_metric",
    "_is_phone_metric",
    "_mentions_both_sources",
    "_mentions_smartphone_source",
    "_mentions_withings_source",
    "_normalize_metric_name",
    "_normalize_metrics",
    "_question_language",
    "_suggest_followup_action",
]
