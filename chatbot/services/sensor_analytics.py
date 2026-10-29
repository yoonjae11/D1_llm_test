"""Application boundary around the existing LangGraph Sensor Analytics flow.

Transport adapters call this module without knowing how to assemble LangGraph
state. The public boundary now invokes the shared D1/D2 orchestration graph.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Protocol

from langchain_core.messages import HumanMessage
from chatbot.config import get_settings

class CompiledGraph(Protocol):
    def invoke(self, state: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]: ...


GraphFactory = Callable[[], CompiledGraph]


def build_initial_state(
    *,
    app_user_id: str,
    telegram_chat_id: str,
    message_text: str,
    inbound_payload: dict,
    thread_id: str = "",
) -> Dict[str, Any]:
    """Build the exact state shape historically created by the entrypoint."""

    resolved_thread_id = str(thread_id or "").strip()
    if not resolved_thread_id:
        resolved_thread_id = (
            f"telegram:{telegram_chat_id}"
            if telegram_chat_id
            else f"user:{app_user_id}"
        )
    payload = inbound_payload or {}
    return {
        "messages": [HumanMessage(content=message_text)],
        "user_id": app_user_id,
        "thread_id": resolved_thread_id,
        "intent": "",
        "route": "",
        "domain_execution_plan": None,
        "domain_results": None,
        "d1_result": None,
        "coaching_strategy": None,
        "orchestration_trace": None,
        "plan": None,
        "last_plan": None,
        "prev_plan": None,
        "kql_query": None,
        "kql_generation_status": None,
        "kql_generation_trace": None,
        "query_result": None,
        "error": None,
        "repair_attempts": 0,
        "max_repair_attempts": max(0, int(get_settings().max_kql_repair_attempts)),
        "inbound_payload": payload,
        "reply_to_text": str(payload.get("reply_to_text") or ""),
        "reply_to_role": str(payload.get("reply_to_role") or ""),
    }


def run_sensor_analytics_graph(
    *,
    app_user_id: str,
    telegram_chat_id: str,
    message_text: str,
    inbound_payload: dict,
    thread_id: str = "",
    graph_factory: GraphFactory | None = None,
) -> Dict[str, Any]:
    """Invoke the existing graph through a small, injectable service seam."""

    if graph_factory is None:
        from chatbot.graph import build_graph

        graph_factory = build_graph

    state = build_initial_state(
        app_user_id=app_user_id,
        telegram_chat_id=telegram_chat_id,
        message_text=message_text,
        inbound_payload=inbound_payload,
        thread_id=thread_id,
    )
    thread_id = str(state["thread_id"])
    graph = graph_factory()
    return graph.invoke(state, config={"configurable": {"thread_id": thread_id}})
