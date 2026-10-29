"""LangGraph node that hydrates durable profile and conversation context."""

from __future__ import annotations

import datetime as dt
import json
from typing import Any, Dict, List

from chatbot.config import get_settings
from chatbot.graph.state import AgentState
from chatbot.nodes.common.support import logger
from chatbot.operational_storage import utc_now
from chatbot.policies.availability import enabled_availability_items
from chatbot.response_chain import resolve_previous_response_id
from chatbot.services.memory import create_operational_storage
from chatbot.timezone_onboarding import timezone_is_resolved


def _thread_id(state: AgentState) -> str:
    return str(state.get("thread_id") or state.get("episode_id") or "local").strip() or "local"


def _merge_memory_context(existing: str, conversation_state: Any) -> str:
    parts: List[str] = []
    if existing:
        parts.append(str(existing))
    if conversation_state.rolling_summary:
        parts.append("Rolling summary: " + conversation_state.rolling_summary)
    if conversation_state.active_context:
        prompt_context = conversation_state.active_context
        if isinstance(prompt_context, dict):
            prompt_context = {
                key: value for key, value in prompt_context.items()
                if key != "domain_orchestration"
            }
        try:
            parts.append(
                "Active data context: "
                + json.dumps(prompt_context, ensure_ascii=False)
            )
        except Exception:
            parts.append("Active data context: " + str(prompt_context))
    return "\n".join(part for part in parts if part).strip()


def _load_durable_context(state: AgentState) -> Dict[str, Any]:
    """Load the user profile, data availability, episode, and response-chain state."""

    app_user_id = str(state.get("user_id") or "").strip()
    thread_id = _thread_id(state)
    repository = create_operational_storage()

    profile = repository.get_user_profile(app_user_id)
    conversation_state = repository.get_conversation_state(app_user_id, thread_id)
    availability = enabled_availability_items(
        repository.get_metric_availability_map(app_user_id)
    )

    if not availability and app_user_id:
        try:
            from chatbot.availability_refresh import refresh_metric_availability

            refresh_metric_availability(app_user_id, repository=repository)
            availability = enabled_availability_items(
                repository.get_metric_availability_map(app_user_id)
            )
        except Exception as exc:
            logger.warning("lazy metric availability refresh failed for %s: %s", app_user_id, exc)

    settings = get_settings()
    timezone_resolved = timezone_is_resolved(profile)
    onboarding_required = bool(
        settings.strict_timezone_onboarding and not timezone_resolved
    )

    previous_response_id = resolve_previous_response_id(
        conversation_state,
        settings,
        utc_now(),
    )
    chain_started = conversation_state.response_chain_started_at_utc
    chain_started_iso = (
        chain_started.isoformat()
        if isinstance(chain_started, dt.datetime)
        else (chain_started or None)
    )

    last_plan = state.get("last_plan") if isinstance(state.get("last_plan"), dict) else {}
    if not last_plan and isinstance(conversation_state.active_context, dict):
        stored_plan = conversation_state.active_context.get("last_plan")
        if isinstance(stored_plan, dict):
            last_plan = stored_plan

    context_mode = ""
    if str(conversation_state.active_episode_type or "") == "proactive_alert":
        context_mode = "continue_system_initiated_episode"

    return {
        "context_mode": context_mode,
        "thread_id": thread_id,
        "timezone_iana": profile.timezone_iana,
        "timezone_resolved": timezone_resolved,
        "onboarding_required": onboarding_required,
        "locale": profile.locale,
        "user_profile": {
            "app_user_id": profile.app_user_id,
            "timezone_iana": profile.timezone_iana,
            "locale": profile.locale,
            "study_site_code": profile.study_site_code,
            "country_code": profile.country_code,
            "timezone_source": profile.timezone_source,
            "default_trend_days": profile.default_trend_days,
            "system_steps_alert_enabled": profile.system_steps_alert_enabled,
        },
        "availability_context": {
            metric: {
                "is_available": item.is_available,
                "preferred_source_table": item.preferred_source_table,
                "earliest_date_from_local": item.earliest_date_from_local,
                "latest_date_to_local": item.latest_date_to_local,
                "latest_ingestion_time_utc": item.latest_ingestion_time_utc,
                "refreshed_at_utc": item.refreshed_at_utc,
            }
            for metric, item in availability.items()
        },
        "conversation_recent_turns": conversation_state.recent_turns,
        "conversation_rolling_summary": conversation_state.rolling_summary,
        "conversation_active_context": conversation_state.active_context,
        "memory_context": _merge_memory_context(
            str(state.get("memory_context") or ""),
            conversation_state,
        ),
        "last_plan": last_plan,
        "prev_plan": last_plan,
        "previous_response_id": previous_response_id,
        "latest_response_id": conversation_state.latest_response_id,
        "response_chain_started_at_utc": chain_started_iso,
        "active_episode_id": conversation_state.active_episode_id,
        "active_episode_type": conversation_state.active_episode_type,
        "suspended_episodes": conversation_state.suspended_episodes,
        "fallback_summary": conversation_state.fallback_summary,
        "pending_clarification": conversation_state.pending_clarification,
    }


def prepare_context(state: AgentState) -> Dict[str, Any]:
    """Reuse hydration and clear request-local execution artifacts on each turn."""
    hydrated = _load_durable_context(state)
    return {
        **hydrated,
        "domain_execution_plan": None, "domain_results": None,
        "d1_result": None, "coaching_strategy": None, "orchestration_trace": None,
        "dependency_results": None, "response_domain_context": None,
        "plan": None, "query_result": None, "coverage_context": None,
        "kql_query": None, "kql_source": None, "kql_generation_status": None,
        "kql_generation_trace": None, "kql_guard": None, "guard_status": None,
        "error": None, "error_type": None, "execution_status": None,
        "adx_error": None, "mcp_error": None, "failed_kql": None,
        "raw_plan": None, "sanitized_plan": None, "kql_issues": None,
        "repair_attempts": 0, "response_id": None,
    }


__all__ = ["prepare_context"]
