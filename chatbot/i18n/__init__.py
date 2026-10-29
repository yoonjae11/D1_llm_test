"""Deterministic English user-facing text with stable compatibility exports."""

from .catalog import (
    CAPABILITIES_GENERAL,
    METRIC_LABELS,
    TIMEZONE_SITE_LABELS,
    message,
    validate_catalog,
)

__all__ = [
    "CAPABILITIES_GENERAL",
    "METRIC_LABELS",
    "TIMEZONE_SITE_LABELS",
    "message",
    "validate_catalog",
]
