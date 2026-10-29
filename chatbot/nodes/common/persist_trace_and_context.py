"""LangGraph node that persists the completed conversation turn."""

from __future__ import annotations

import datetime as dt
from hashlib import sha256
from typing import Any, Dict

from chatbot.graph.state import AgentState, latest_user_message
from chatbot.nodes.common.support import logger
from chatbot.operational_storage import ChatbotConversationState, utc_now
from chatbot.response_chain import parse_utc
from chatbot.services.memory import create_operational_storage


def _thread_id(state: AgentState) -> str:
    return str(state.get("thread_id") or state.get("episode_id") or "local").strip() or "local"


def _compact_turn(role: str, text: str, when_utc: dt.datetime | None = None) -> Dict[str, Any]:
    when = when_utc or utc_now()
    normalized = str(text or "")[:3000]
    return {
        "turn_id": "turn_" + sha256(f"{role}|{when.isoformat()}|{normalized}".encode("utf-8")).hexdigest()[:16],
        "role": role,
        "message_text": normalized,
        "event_time_utc": when.isoformat(),
    }


def _assistant_text(state: AgentState) -> str:
    for message in reversed(state.get("messages") or []):
        if str(getattr(message, "type", "")).lower() in {"ai", "assistant"}:
            content = getattr(message, "content", "")
            if isinstance(content, list):
                return " ".join(str(item) for item in content)
            return str(content or "")
    return ""


def persist_trace_and_context(state: AgentState) -> Dict[str, Any]:
    """Append the completed turn and save episode, plan, and response-chain state."""

    app_user_id = str(state.get("user_id") or "").strip()
    if not app_user_id:
        return {}

    repository = create_operational_storage()
    thread_id = _thread_id(state)
    now = utc_now()
    existing_turns = state.get("conversation_recent_turns")
    if not isinstance(existing_turns, list):
        existing_turns = []

    turns = list(existing_turns)
    user_text = latest_user_message(state.get("messages") or [])
    assistant_text = _assistant_text(state)
    if user_text:
        turns.append(_compact_turn("user", user_text, now))
    if assistant_text:
        turns.append(_compact_turn("assistant", assistant_text, now))

    plan = (
        state.get("last_plan")
        if isinstance(state.get("last_plan"), dict)
        else state.get("plan")
    )
    active_context: Dict[str, Any] = {}
    if isinstance(state.get("conversation_active_context"), dict):
        active_context.update(state.get("conversation_active_context") or {})
    # One bounded latest-turn trace, using the existing durable context store.
    # Do not persist full sensor rows or repeat them into future prompts.
    if isinstance(state.get("orchestration_trace"), dict):
        trace = state["orchestration_trace"]
        active_context["domain_orchestration"] = {
            "plan": state.get("domain_execution_plan"),
            "planner_status": trace.get("planner_status"),
            "planner_llm_calls": trace.get("planner_llm_calls"),
            "planner_latency_ms": trace.get("planner_latency_ms"),
            "execution_latency_ms": trace.get("execution_latency_ms"),
            "execution_stages": trace.get("execution_stages", []),
            "executed_stages": trace.get("executed_stages", []),
            "dependency_statuses": {
                key: (value.get("provenance") or {}).get("dependency_statuses", {})
                for key, value in (state.get("domain_results") or {}).items()
            },
            "domain_statuses": {
                key: value.get("status") for key, value in (state.get("domain_results") or {}).items()
            },
            "d2_strategy": state.get("coaching_strategy"),
            "d2_provenance": ((state.get("domain_results") or {}).get("D2") or {}).get("provenance", {}),
            "d3_result": state.get("health_knowledge_result"),
            "d3_provenance": ((state.get("domain_results") or {}).get("D3") or {}).get("provenance", {}),
            "d4_result": state.get("resolved_context"),
            "d4_provenance": ((state.get("domain_results") or {}).get("D4") or {}).get("provenance", {}),
            "updated_at_utc": now.isoformat(),
        }
    if isinstance(plan, dict) and plan:
        active_context.update(
            {
                "last_plan": plan,
                "query_family": plan.get("query_family") or plan.get("case_type"),
                "metrics": plan.get("metrics", []),
                "time_range": plan.get("time_range", {}),
                "updated_at_utc": now.isoformat(),
            }
        )

    rolling_summary = str(state.get("conversation_rolling_summary") or "")[:4000]
    if len(turns) > 12 and not rolling_summary:
        preview = []
        for turn in turns[:-8]:
            role = turn.get("role", "")
            text = str(turn.get("message_text", "")).strip()
            if text:
                preview.append(f"{role}: {text[:180]}")
        rolling_summary = "\n".join(preview)[-4000:]

    new_response_id = str(state.get("response_id") or "").strip()
    latest_response_id = new_response_id or str(state.get("latest_response_id") or "")
    chain_started = parse_utc(state.get("response_chain_started_at_utc"))
    if new_response_id and chain_started is None:
        chain_started = now

    episode_type = str(state.get("active_episode_type") or "reactive")
    if episode_type == "proactive_alert" and user_text:
        episode_type = "reactive"

    suspended = state.get("suspended_episodes")
    pending = state.get("pending_clarification")
    conversation_state = ChatbotConversationState(
        app_user_id=app_user_id,
        thread_id=thread_id,
        recent_turns=turns,
        rolling_summary=rolling_summary,
        active_context=active_context,
        turn_count=len(turns),
        last_user_message_utc=now if user_text else None,
        last_assistant_message_utc=now if assistant_text else None,
        latest_response_id=latest_response_id,
        response_chain_started_at_utc=chain_started,
        active_episode_id=str(state.get("active_episode_id") or ""),
        active_episode_type=episode_type,
        suspended_episodes=suspended if isinstance(suspended, list) else [],
        fallback_summary=str(state.get("fallback_summary") or ""),
        pending_clarification=pending if isinstance(pending, dict) else {},
    )
    saved = repository.save_conversation_state(conversation_state)
    if not saved and repository.last_error:
        logger.warning("ChatbotConversationState persist failed: %s", repository.last_error)

    return {
        "conversation_recent_turns": conversation_state.recent_turns,
        "conversation_rolling_summary": conversation_state.rolling_summary,
        "conversation_active_context": conversation_state.active_context,
    }
__all__ = ["persist_trace_and_context"]
