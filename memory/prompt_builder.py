"""Prompt helpers for memory-enriched chatbot state."""

from __future__ import annotations

import json
from typing import Any, Iterable, List

from .models import ConversationEpisode, ConversationTurn, SystemInitiatedContext, UserLongTermMemory


def _clip(text: Any, limit: int) -> str:
    value = str(text or "").strip()
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."


def _turn_line(turn: ConversationTurn) -> str:
    role = str(turn.role or "user").title()
    return f"{role}: {_clip(turn.message_text, 500)}"


def build_memory_context_text(
    *,
    active_episode: ConversationEpisode | None,
    recent_turns: Iterable[ConversationTurn] | None = None,
    related_episode_summaries: List[dict] | None = None,
    long_term_memory: Iterable[UserLongTermMemory] | None = None,
    system_context: dict | SystemInitiatedContext | None = None,
) -> str:
    """Build a compact, separated memory block for LLM prompts."""

    lines: List[str] = []
    if active_episode:
        lines.extend(
            [
                "[Active episode]",
                f"episode_id: {active_episode.episode_id}",
                f"initiated_by: {active_episode.initiated_by}",
                f"trigger_type: {active_episode.trigger_type}",
                f"topic: {_clip(active_episode.topic_label, 180) or 'unknown'}",
            ]
        )
        if active_episode.summary:
            lines.append(f"summary: {_clip(active_episode.summary, 1200)}")
        if active_episode.last_query_result_summary:
            lines.append(f"last_query_result_summary: {_clip(active_episode.last_query_result_summary, 1000)}")
        if active_episode.open_question:
            lines.append(f"open_question: {_clip(active_episode.open_question, 600)}")

    turns = list(recent_turns or [])[-8:]
    if turns:
        lines.append("")
        lines.append("[Recent turns in active episode]")
        lines.extend(_turn_line(turn) for turn in turns)

    if system_context:
        if isinstance(system_context, SystemInitiatedContext):
            context_payload = {
                "context_type": system_context.context_type,
                "trigger_id": system_context.trigger_id,
                "initial_message": system_context.initial_message,
                "evidence": system_context.evidence(),
            }
        else:
            context_payload = dict(system_context)
        lines.append("")
        lines.append("[System-initiated context]")
        lines.append(_clip(json.dumps(context_payload, ensure_ascii=False, default=str), 1800))

    related = list(related_episode_summaries or [])[:3]
    if related:
        lines.append("")
        lines.append("[Related paused/resolved episodes]")
        for item in related:
            topic = _clip(item.get("topic_label", ""), 120)
            summary = _clip(item.get("summary", ""), 500)
            lines.append(f"- {item.get('episode_id')}: {topic}; {summary}")

    memories = list(long_term_memory or [])[:10]
    if memories:
        lines.append("")
        lines.append("[Long-term user memory]")
        for memory in memories:
            lines.append(f"- {memory.memory_type}: {_clip(memory.content, 300)}")

    return "\n".join(line for line in lines if line is not None).strip()


def memory_context_for_state(resolved_context) -> str:
    return build_memory_context_text(
        active_episode=getattr(resolved_context, "selected_episode", None),
        recent_turns=getattr(resolved_context, "recent_turns", []),
        related_episode_summaries=getattr(resolved_context, "related_episode_summaries", []),
        long_term_memory=getattr(resolved_context, "long_term_memory", []),
        system_context=getattr(resolved_context, "system_context", None),
    )
