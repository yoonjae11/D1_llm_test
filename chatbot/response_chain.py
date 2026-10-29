"""Pure helpers for the Azure OpenAI Responses API conversation chain.

A ``previous_response_id`` chain is only valid for a bounded age. Beyond that the
chain is reset (a new episode starts) and the stored FallbackSummary / active
context provide minimal continuity. These helpers contain no Azure calls so they
are unit-testable without credentials.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Optional


def parse_utc(value: Any) -> Optional[dt.datetime]:
    if isinstance(value, dt.datetime):
        moment = value
    else:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            moment = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        except Exception:
            return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return moment.astimezone(dt.timezone.utc)


def chain_is_fresh(started_at_utc: Any, now: dt.datetime, max_age_days: int) -> bool:
    """True when the chain start is present and within ``max_age_days`` of ``now``."""
    started = parse_utc(started_at_utc)
    if started is None:
        return False
    if max_age_days <= 0:
        return True
    return (now - started) <= dt.timedelta(days=int(max_age_days))


def resolve_previous_response_id(conversation_state: Any, settings: Any, now: dt.datetime) -> Optional[str]:
    """Return the previous_response_id to continue the chain, or None to start fresh.

    Returns None when the API is disabled, storage is off, the id is missing, or the
    chain has aged out. In all those cases the next turn begins a new chain.
    """
    if not getattr(settings, "use_azure_responses_api", False):
        return None
    if not getattr(settings, "response_store", True):
        return None
    response_id = str(getattr(conversation_state, "latest_response_id", "") or "").strip()
    if not response_id:
        return None
    if not chain_is_fresh(
        getattr(conversation_state, "response_chain_started_at_utc", None),
        now,
        int(getattr(settings, "response_chain_max_age_days", 25)),
    ):
        return None
    return response_id
