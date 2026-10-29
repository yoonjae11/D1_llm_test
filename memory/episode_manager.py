"""Helpers for updating episode memory around chatbot turns."""

from __future__ import annotations

import json
from typing import Any, Dict

from .models import ConversationEpisode, ConversationTurn, json_dumps, sha256_text, utc_now
from .summarizer import should_summarize, summarize_episode


def _short_json(value: Any, limit: int = 1200) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))
    except Exception:
        text = str(value or "")
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def build_inbound_turn(
    *,
    app_user_id: str,
    episode_id: str,
    message_text: str,
    event_time_utc=None,
    route: str = "chat",
    payload: Dict[str, Any] | None = None,
) -> ConversationTurn:
    return ConversationTurn(
        episode_id=episode_id,
        app_user_id=app_user_id,
        role="user",
        direction="inbound",
        message_text=message_text,
        event_time_utc=event_time_utc or utc_now(),
        route=route,
        payload_json=json_dumps(payload or {}),
    )


def build_outbound_turn(
    *,
    app_user_id: str,
    episode_id: str,
    message_text: str,
    final_state: Dict[str, Any] | None = None,
    payload: Dict[str, Any] | None = None,
) -> ConversationTurn:
    state = final_state if isinstance(final_state, dict) else {}
    kql = str(state.get("kql_query") or "")
    query_summary = str(state.get("last_result_summary") or "")
    if not query_summary:
        query_result = state.get("query_result")
        if query_result not in (None, "", []):
            query_summary = _short_json(query_result)
    return ConversationTurn(
        episode_id=episode_id,
        app_user_id=app_user_id,
        role="assistant",
        direction="outbound",
        message_text=message_text,
        event_time_utc=utc_now(),
        intent=str(state.get("intent") or ""),
        route=str(state.get("intent") or "chat"),
        kql_query_hash=sha256_text(kql) if kql else "",
        query_result_summary=query_summary,
        payload_json=json_dumps(payload or {}),
    )


def update_episode_after_turn(
    *,
    store,
    episode: ConversationEpisode,
    final_state: Dict[str, Any] | None,
    outbound_turn: ConversationTurn,
) -> None:
    state = final_state if isinstance(final_state, dict) else {}
    episode.last_turn_at_utc = outbound_turn.event_time_utc
    episode.last_intent = str(state.get("intent") or episode.last_intent or "")
    episode.last_route = str(state.get("intent") or episode.last_route or "")
    if outbound_turn.kql_query_hash:
        episode.last_kql_query_hash = outbound_turn.kql_query_hash
    if outbound_turn.query_result_summary:
        episode.last_query_result_summary = outbound_turn.query_result_summary

    recent_turns = store.get_recent_turns(episode.episode_id, limit=20)
    if should_summarize(episode, recent_turns):
        episode.summary = summarize_episode(episode, recent_turns, episode.summary)
    store.update_episode(episode)
