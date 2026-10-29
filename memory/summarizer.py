"""Heuristic episode summarization for operational memory."""

from __future__ import annotations

from typing import Iterable, List

from . import config
from .models import ConversationEpisode, ConversationTurn


def should_summarize(episode: ConversationEpisode, recent_turns: Iterable[ConversationTurn]) -> bool:
    turns = list(recent_turns or [])
    if not turns:
        return False
    raw_chars = sum(len(turn.message_text or "") for turn in turns)
    if len(turns) >= config.MULTITURN_SUMMARY_TRIGGER_TURNS:
        return True
    if raw_chars >= config.MULTITURN_SUMMARY_TRIGGER_CHARS:
        return True
    return episode.status in {"paused", "resolved"} and bool(turns)


def summarize_episode(
    episode: ConversationEpisode,
    recent_turns: List[ConversationTurn],
    previous_summary: str | None = None,
) -> str:
    """Create a compact structured summary without inventing numeric facts."""

    user_turns = [turn for turn in recent_turns if turn.role == "user"]
    assistant_turns = [turn for turn in recent_turns if turn.role == "assistant"]
    last_user = user_turns[-1].message_text if user_turns else ""
    last_assistant = assistant_turns[-1].message_text if assistant_turns else ""
    data_summaries = [
        turn.query_result_summary
        for turn in reversed(recent_turns)
        if turn.query_result_summary
    ]

    lines = [
        f"Topic: {episode.topic_label or 'unknown'}",
        f"What the user asked: {last_user[:900] if last_user else 'No recent user turn.'}",
        f"What the assistant answered: {last_assistant[:900] if last_assistant else 'No recent assistant turn.'}",
    ]
    if data_summaries:
        lines.append(f"Data used: {data_summaries[0][:1200]}")
    elif episode.last_query_result_summary:
        lines.append(f"Data used: {episode.last_query_result_summary[:1200]}")
    else:
        lines.append("Data used: None recorded in operational memory.")
    if episode.last_kql_query_hash:
        lines.append(f"KQL query hash: {episode.last_kql_query_hash}")
    if episode.open_question:
        lines.append(f"Open follow-up: {episode.open_question[:500]}")
    if episode.last_alert_id or episode.evidence_ref:
        lines.append(f"Alert/evidence context: alert_id={episode.last_alert_id}; evidence_ref={episode.evidence_ref}")
    if previous_summary:
        lines.append(f"Previous summary: {previous_summary[:1200]}")
    return "\n".join(lines)
