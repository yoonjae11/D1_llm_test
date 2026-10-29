"""Deterministic metric-availability decisions for response composition.

Unified availability is stored under the ADX ``Type`` value (the historical
contract). Optional direct Withings tables do not have an equivalent type
column, so a deployment that deliberately enables them stores their availability
under stable table-scoped keys. Canonical requirements are derived from the
shared domain catalog rather than treating a vendor table as a generic fallback.
"""

import os
import re
from typing import Any, Mapping, Sequence

from .domain_catalog import (
    METRIC_DEFINITIONS,
    PHONE_TO_WITHINGS_METRIC_PRIORITY,
    PHONE_SLEEP_NIGHTS_AVAILABILITY_TYPES,
)
from .semantic_resources import (
    WITHINGS_RESOURCE_IDS,
    WORKOUT_SESSIONS,
    logical_name,
    physical_name,
)


SOURCE_TABLE_AVAILABILITY_PREFIX = "SOURCE_TABLE:"
_TYPE_EQUALITY_RE = re.compile(
    r"\bType\s*==\s*['\"]([A-Z0-9_]+)['\"]",
    flags=re.IGNORECASE,
)
_TYPE_IN_RE = re.compile(
    r"\bType\s+in\s*\(([^)]*)\)",
    flags=re.IGNORECASE,
)
_QUOTED_TYPE_RE = re.compile(r"['\"]([A-Z0-9_]+)['\"]", flags=re.IGNORECASE)


def source_table_availability_key(table: str) -> str:
    """Return the persisted availability key for a source without ``Type``."""

    return f"{SOURCE_TABLE_AVAILABILITY_PREFIX}{str(table or '').strip().upper()}"


def optional_withings_tables_enabled() -> bool:
    """Return whether this deployment opted into direct Withings resources."""

    return str(
        os.environ.get("CHATBOT_ENABLE_OPTIONAL_WITHINGS_TABLES", "")
    ).strip().lower() in {"1", "true", "yes", "on"}


def enabled_availability_items(items: Mapping[str, Any]) -> dict[str, Any]:
    """Hide cached optional-source rows when that source is disabled.

    Older cache entities can outlive a policy deployment because availability
    storage uses upserts. Filtering at hydration prevents those rows from
    steering planning while the next refresh writes explicit disabled markers.
    """

    if optional_withings_tables_enabled():
        return dict(items)
    result: dict[str, Any] = {}
    for metric_type, item in items.items():
        key = str(metric_type or "").upper()
        table = (
            str(item.get("preferred_source_table") or "")
            if isinstance(item, dict)
            else str(getattr(item, "preferred_source_table", "") or "")
        )
        is_optional_source = False
        if table:
            try:
                is_optional_source = logical_name(table) in WITHINGS_RESOURCE_IDS
            except KeyError:
                is_optional_source = False
        if key.startswith(SOURCE_TABLE_AVAILABILITY_PREFIX) or is_optional_source:
            continue
        result[metric_type] = item
    return result


def _types_in_expression(text: str) -> set[str]:
    """Extract ADX ``Type`` values from equality and ``in (...)`` predicates."""

    value = str(text or "")
    result = {match.upper() for match in _TYPE_EQUALITY_RE.findall(value)}
    for values in _TYPE_IN_RE.findall(value):
        result.update(match.upper() for match in _QUOTED_TYPE_RE.findall(values))
    return result


def metric_adx_types(metric: str) -> set[str]:
    """Return the ADX Type values referenced by a canonical metric."""

    definition = METRIC_DEFINITIONS.get(metric, {})
    text = str(definition.get("summary", "")) + " " + str(definition.get("trend", ""))
    return _types_in_expression(text)


def metric_availability_keys(metric: str) -> set[str]:
    """Return persisted keys that can satisfy a canonical metric.

    Generic metrics resolve to HealthDataFlat types (including the types used by
    curated activity and sleep resources). Historical ``phone_*`` names inherit
    the same unified type keys as their source-neutral counterpart. Vendor-only
    metrics use only their explicit table-scoped key and never borrow a unified
    key merely because the preferred source has no rows.
    """

    definition = METRIC_DEFINITIONS.get(metric, {})
    keys = metric_adx_types(metric)
    keys.update(PHONE_SLEEP_NIGHTS_AVAILABILITY_TYPES.get(metric, ()))
    source_neutral_metric = PHONE_TO_WITHINGS_METRIC_PRIORITY.get(metric)
    if source_neutral_metric:
        keys.update(metric_adx_types(source_neutral_metric))
        keys.update(
            PHONE_SLEEP_NIGHTS_AVAILABILITY_TYPES.get(source_neutral_metric, ())
        )
    resource_id = str(definition.get("resource") or "")
    if resource_id == WORKOUT_SESSIONS:
        # Structured workout records do not use scalar Type predicates in
        # catalog expressions. Presence is a session-family discovery hint;
        # optional attributes still require bounded payload/field inspection.
        keys.add("WORKOUT")
    if metric in {"total_sleep_time", "phone_sleep_duration"}:
        keys.add("SLEEP_SESSION")
    if resource_id in WITHINGS_RESOURCE_IDS:
        keys.add(source_table_availability_key(physical_name(resource_id)))
    return {key.upper() for key in keys if key}


def uncollected_metrics(
    metrics: Sequence[str],
    availability_context: Mapping[str, Any],
) -> list[str]:
    """Return metrics for which every configured backing source is unavailable."""

    if not availability_context or not metrics:
        return []
    available_types = {
        str(metric_type).upper()
        for metric_type, item in availability_context.items()
        if (item.get("is_available") if isinstance(item, dict) else getattr(item, "is_available", False))
    }
    result: list[str] = []
    for metric in metrics:
        availability_keys = metric_availability_keys(metric)
        if availability_keys and not (availability_keys & available_types):
            result.append(metric)
    return result


__all__ = [
    "enabled_availability_items",
    "metric_adx_types",
    "metric_availability_keys",
    "optional_withings_tables_enabled",
    "source_table_availability_key",
    "uncollected_metrics",
]
