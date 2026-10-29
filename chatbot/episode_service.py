"""Episode interrupt / resume on top of ChatbotConversationState.

A "reactive" episode is the normal user-driven conversation. When a proactive
alert interrupts it, the active episode is pushed onto a suspended stack and the
alert becomes the active episode. ``/resume`` pops the stack; ``/new`` starts a
fresh reactive episode. ``/resume`` and ``/new`` are always available; the
proactive interrupt itself is flag-gated so default behavior is unchanged.

All state lives in the pre-existing ChatbotConversationState table. These helpers
read/modify/save whole-state objects and never create tables.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, Optional

from chatbot.config import get_settings
from chatbot.operational_storage import utc_now
from chatbot.services.memory import OperationalStoragePort, create_operational_storage


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


def thread_id_for_chat(chat_id: Any) -> str:
    """Mirror the thread id used by the chatbot entrypoint for a Telegram chat."""
    return f"telegram:{chat_id}"


def _snapshot(state) -> Dict[str, Any]:
    return {
        "episode_id": str(state.active_episode_id or uuid.uuid4().hex),
        "episode_type": str(state.active_episode_type or "reactive"),
        "latest_response_id": str(state.latest_response_id or ""),
        "active_context": dict(state.active_context or {}),
        "rolling_summary": str(state.rolling_summary or ""),
        "suspended_at_utc": utc_now().isoformat(),
    }


def handle_new_command(
    app_user_id: str,
    thread_id: str,
    *,
    is_korean: bool = False,
    repository: Optional[OperationalTableRepository] = None,
) -> str:
    """Start a fresh reactive episode: clear active context, clarification, chain."""
    repository = repository or OperationalTableRepository(fail_soft=True)
    state = repository.get_conversation_state(app_user_id, thread_id)
    state.active_context = {}
    state.pending_clarification = {}
    state.active_episode_id = uuid.uuid4().hex
    state.active_episode_type = "reactive"
    state.latest_response_id = ""
    state.response_chain_started_at_utc = None
    repository.save_conversation_state(state)
    return "Starting a new conversation. Previous context cleared. How can I help?"


def handle_resume_command(
    app_user_id: str,
    thread_id: str,
    *,
    is_korean: bool = False,
    repository: Optional[OperationalTableRepository] = None,
) -> str:
    """Restore the most recently suspended episode, if any."""
    repository = repository or OperationalTableRepository(fail_soft=True)
    state = repository.get_conversation_state(app_user_id, thread_id)
    suspended = list(state.suspended_episodes or [])
    if not suspended:
        return "There is no previous conversation to resume."

    episode = suspended.pop()
    state.suspended_episodes = suspended
    state.active_context = dict(episode.get("active_context") or {})
    state.latest_response_id = str(episode.get("latest_response_id") or "")
    state.response_chain_started_at_utc = None  # chain age unknown; start fresh on next turn
    state.active_episode_id = str(episode.get("episode_id") or uuid.uuid4().hex)
    state.active_episode_type = str(episode.get("episode_type") or "reactive")
    if episode.get("rolling_summary"):
        state.rolling_summary = str(episode.get("rolling_summary"))
    repository.save_conversation_state(state)

    summary = str(episode.get("rolling_summary") or "").strip()
    base = "Resuming your previous conversation."
    return f"{base}\n\n{summary}" if summary else base


def suspend_active_episode_for_alert(
    app_user_id: str,
    thread_id: str,
    *,
    alert_context: Optional[Dict[str, Any]] = None,
    alert_message_text: str = "",
    repository: Optional[OperationalTableRepository] = None,
) -> bool:
    """Push the active reactive episode onto the suspended stack for a proactive alert.

    The alert itself is recorded as an ASSISTANT TURN in the conversation state,
    so the system-initiated message becomes the opening turn of the new episode:
    the user's reply then flows through the SAME LangGraph with the alert text as
    the previous assistant message and the alert facts in active_context.

    Flag-gated by callers via ``settings.proactive_episode_interrupt_enabled``.
    """
    repository = repository or OperationalTableRepository(fail_soft=True)
    settings = get_settings()
    state = repository.get_conversation_state(app_user_id, thread_id)

    # Only suspend when there is a real reactive episode to preserve.
    if state.active_context or state.latest_response_id or state.active_episode_id:
        suspended = list(state.suspended_episodes or [])
        suspended.append(_snapshot(state))
        limit = int(settings.suspended_episode_limit or 3)
        state.suspended_episodes = suspended[-limit:]

    state.active_episode_id = uuid.uuid4().hex
    state.active_episode_type = "proactive_alert"
    state.active_context = {"proactive_alert": dict(alert_context or {})}
    # Seed a follow-up plan when the alert carries one (e.g. the daily summary), so
    # a reply like "show more details" inherits a sensible time range + metrics and
    # actually queries the summarized data instead of an empty fresh day.
    seed_plan = (alert_context or {}).get("followup_plan")
    if isinstance(seed_plan, dict) and seed_plan:
        state.active_context["last_plan"] = dict(seed_plan)
    state.latest_response_id = ""
    state.response_chain_started_at_utc = None

    # The proactive alert is the first assistant turn of this episode.
    if str(alert_message_text or "").strip():
        turns = list(state.recent_turns or [])
        turns.append(
            {
                "role": "assistant",
                "message_text": str(alert_message_text)[:3000],
                "event_time_utc": utc_now().isoformat(),
            }
        )
        state.recent_turns = turns[-12:]

    return repository.save_conversation_state(state)
