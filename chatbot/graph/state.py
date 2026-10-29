"""Shared LangGraph state for the Sensor Analytics pipeline.

The keys deliberately match the production state dictionary. ``TypedDict``
keeps LangGraph reducers and serialized payloads compatible while making graph
state ownership explicit.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, Dict, List, Optional, TypedDict

from langchain_core.messages import BaseMessage


class KQLGenerationTrace(TypedDict, total=False):
    """Auditable metadata for one configured KQL generation path."""

    method: str
    pre_guard_kql: Optional[str]
    guard_pass: Optional[bool]
    repair_attempted: bool
    final_kql: Optional[str]
    latency_ms: int
    model: Optional[str]
    prompt_version: Optional[str]


class AgentState(TypedDict):
    domain_execution_plan: Optional[Dict[str, Any]]
    domain_results: Optional[Dict[str, Any]]
    d1_result: Optional[Dict[str, Any]]
    dependency_results: Optional[Dict[str, Any]]
    response_domain_context: Optional[Dict[str, Any]]
    coaching_strategy: Optional[Dict[str, Any]]
    health_knowledge_result: Optional[Dict[str, Any]]
    resolved_context: Optional[Dict[str, Any]]
    orchestration_trace: Optional[Dict[str, Any]]
    messages: Annotated[List[BaseMessage], operator.add]
    user_id: str
    intent: str
    kql_query: Optional[str]
    kql_source: Optional[str]
    kql_generation_status: Optional[str]
    kql_generation_trace: Optional[KQLGenerationTrace]
    query_result: Optional[str]
    error: Optional[str]
    raw_plan: Optional[Dict[str, Any]]
    sanitized_plan: Optional[Dict[str, Any]]
    sensor_plan_trace: Optional[Dict[str, Any]]
    plan: Optional[Dict[str, Any]]
    last_plan: Optional[Dict[str, Any]]
    prev_plan: Optional[Dict[str, Any]]
    kql_issues: Optional[List[str]]
    episode_id: Optional[str]
    thread_id: Optional[str]
    context_mode: Optional[str]
    context_confidence: Optional[float]
    active_episode_summary: Optional[str]
    related_episode_summaries: Optional[List[Dict[str, Any]]]
    system_context: Optional[Dict[str, Any]]
    memory_context: Optional[str]
    recent_turns: Optional[List[Dict[str, Any]]]
    last_kql_query_hash: Optional[str]
    last_query_result_summary: Optional[str]
    last_alert_evidence: Optional[Dict[str, Any]]
    route: Optional[str]
    timezone_iana: Optional[str]
    timezone_resolved: Optional[bool]
    onboarding_required: Optional[bool]
    locale: Optional[str]
    user_profile: Optional[Dict[str, Any]]
    availability_context: Optional[Dict[str, Any]]
    coverage_context: Optional[Dict[str, Any]]
    conversation_active_context: Optional[Dict[str, Any]]
    conversation_rolling_summary: Optional[str]
    conversation_recent_turns: Optional[List[Dict[str, Any]]]
    kql_guard: Optional[Dict[str, Any]]
    guard_status: Optional[str]
    execution_status: Optional[str]
    previous_response_id: Optional[str]
    response_id: Optional[str]
    latest_response_id: Optional[str]
    response_chain_started_at_utc: Optional[str]
    active_episode_id: Optional[str]
    active_episode_type: Optional[str]
    suspended_episodes: Optional[List[Dict[str, Any]]]
    fallback_summary: Optional[str]
    pending_clarification: Optional[Dict[str, Any]]
    reply_to_text: Optional[str]
    reply_to_role: Optional[str]
    inbound_payload: Optional[Dict[str, Any]]
    error_type: Optional[str]
    adx_error: Optional[str]
    mcp_error: Optional[str]
    failed_kql: Optional[str]
    last_result_summary: Optional[str]
    repair_attempts: Optional[int]
    max_repair_attempts: Optional[int]
    # Set once query generation got the fact that an empty query selected only
    # records this user does not have (chatbot/policies/evidence_feedback.py).
    evidence_feedback_used: Optional[bool]
    evidence_feedback_request: Optional[Dict[str, Any]]


def format_recent_history(messages: List[BaseMessage], max_turns: int = 4) -> str:
    """Render recent messages to compact plain text for prompt variables."""

    if not messages:
        return ""

    recent = messages[-(max_turns * 2) :]
    lines: List[str] = []
    for msg in recent:
        msg_type = getattr(msg, "type", "")
        role = (
            "User"
            if msg_type in ("human", "user")
            else "Assistant"
            if msg_type in ("ai", "assistant")
            else "Other"
        )
        content = getattr(msg, "content", "")
        text = str(content).strip()
        if len(text) > 320:
            text = text[:320] + "..."
        lines.append(f"{role}: {text}")
    return "\n".join(lines)


def latest_user_message(messages: List[BaseMessage]) -> str:
    """Return the latest user/human message from a message list."""

    if not messages:
        return ""
    for msg in reversed(messages):
        msg_type = str(getattr(msg, "type", "")).lower()
        if msg_type in {"human", "user"}:
            content = getattr(msg, "content", "")
            if isinstance(content, list):
                return " ".join(str(part) for part in content).strip()
            return str(content or "").strip()
    return ""


__all__ = [
    "AgentState",
    "KQLGenerationTrace",
    "format_recent_history",
    "latest_user_message",
]
