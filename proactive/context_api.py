"""API for creating system-initiated conversation episodes."""

from __future__ import annotations

import datetime as dt
from typing import Any, Dict, Optional

from memory import config
from memory.models import ConversationTurn, json_dumps, utc_now
from memory.store import ConversationMemoryStore


def create_system_initiated_episode(
    *,
    app_user_id: str,
    telegram_chat_id_hash: str = "",
    initial_message: str,
    trigger_id: str,
    trigger_type: str = "proactive_alert",
    evidence: Optional[Dict[str, Any]] = None,
    context_type: str = "proactive_alert",
    store=None,
):
    """Create a system-initiated episode before sending a proactive message.

    Returns ``(episode, system_context)``. The caller remains responsible for
    sending the Telegram message; this helper records the operational memory.
    """

    memory_store = store or ConversationMemoryStore()
    now = utc_now()
    expires_at = now + dt.timedelta(days=config.MULTITURN_SYSTEM_CONTEXT_RETENTION_DAYS)

    state = memory_store.get_user_state(app_user_id)
    if state.active_episode_id:
        memory_store.set_episode_status(app_user_id, state.active_episode_id, "paused")

    episode = memory_store.create_episode(
        app_user_id=app_user_id,
        telegram_chat_id_hash=telegram_chat_id_hash,
        initiated_by="system",
        trigger_type=trigger_type,
        trigger_id=trigger_id,
        topic_label="proactive alert",
        topic_embedding_text=initial_message,
        status="active",
        priority=10,
        last_alert_id=trigger_id if trigger_type == "proactive_alert" else "",
        evidence_ref=trigger_id,
    )

    system_context = memory_store.create_system_context(
        app_user_id=app_user_id,
        episode_id=episode.episode_id,
        context_type=context_type,
        trigger_id=trigger_id,
        initial_message=initial_message,
        evidence_json=json_dumps(evidence or {}),
        status="open",
        expires_at_utc=expires_at,
    )

    memory_store.append_turn(
        episode.episode_id,
        ConversationTurn(
            episode_id=episode.episode_id,
            app_user_id=app_user_id,
            role="system",
            direction="outbound",
            message_text=initial_message,
            event_time_utc=now,
            route="proactive_alert",
            intent="proactive_alert",
            payload_json=json_dumps({"trigger_id": trigger_id, "evidence": evidence or {}}),
        ),
    )

    state.active_episode_id = episode.episode_id
    state.last_system_initiated_episode_id = episode.episode_id
    if trigger_type == "proactive_alert":
        state.last_alert_episode_id = episode.episode_id
    state.recent_episode_ids = [episode.episode_id] + [
        item for item in state.recent_episode_ids if item != episode.episode_id
    ]
    memory_store.upsert_user_state(state)
    return episode, system_context
