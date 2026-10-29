"""Public chatbot entrypoint used by Telegram and local tools."""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any, Dict, List

from chatbot.graph import build_graph
from chatbot.schemas.tool_result import ChatbotResponse
from chatbot.services.sensor_analytics import run_sensor_analytics_graph
from chatbot.policies.semantic_resources import active_data_profile


def _extract_kql_tables(kql: str) -> List[str]:
    # Telemetry reports the physical objects selected by the trusted active data
    # profile. The semantic planner itself never needs these deployment names.
    known = [
        binding.physical_name
        for binding in active_data_profile().bindings.values()
    ]
    return [table for table in known if re.search(rf"(?<![\w.]){re.escape(table)}(?![\w.])", str(kql or ""))]


def _row_count(query_result: Any) -> int:
    if isinstance(query_result, list):
        return len(query_result)
    if isinstance(query_result, str):
        try:
            parsed = json.loads(query_result)
            if isinstance(parsed, list):
                return len(parsed)
        except Exception:
            return 0
    return 0


def _run_graph_sync(
    *,
    app_user_id: str,
    telegram_chat_id: str,
    message_text: str,
    inbound_payload: dict,
    thread_id: str = "",
) -> Dict[str, Any]:
    return run_sensor_analytics_graph(
        app_user_id=app_user_id,
        telegram_chat_id=telegram_chat_id,
        message_text=message_text,
        inbound_payload=inbound_payload or {},
        thread_id=thread_id,
        graph_factory=build_graph,
    )


async def handle_user_message(
    *,
    app_user_id: str,
    telegram_chat_id: str = "",
    message_text: str,
    inbound_payload: dict | None = None,
    thread_id: str = "",
) -> ChatbotResponse:
    started = time.perf_counter()
    final_state = await asyncio.to_thread(
        _run_graph_sync,
        app_user_id=str(app_user_id or ""),
        telegram_chat_id=str(telegram_chat_id or ""),
        message_text=str(message_text or ""),
        inbound_payload=inbound_payload or {},
        thread_id=str(thread_id or ""),
    )
    messages = final_state.get("messages") or []
    text = ""
    if messages:
        text = str(getattr(messages[-1], "content", "") or "")
    kql_query = str(final_state.get("kql_query") or "")
    query_result = final_state.get("query_result")
    execution_status = str(final_state.get("execution_status") or "ok")
    if final_state.get("error"):
        execution_status = "error"
    payload = {
        "handler": "chatbot.entrypoint",
        "data_profile": active_data_profile().profile_id,
        "latency_ms": int((time.perf_counter() - started) * 1000),
        "plan": final_state.get("plan"),
        "last_plan": final_state.get("last_plan"),
        "guard_status": final_state.get("guard_status"),
        "error": final_state.get("error"),
        "final_state": final_state,
    }
    return ChatbotResponse(
        text=text,
        route=str(final_state.get("route") or final_state.get("intent") or "chat"),
        intent=str(final_state.get("intent") or ""),
        execution_status=execution_status,
        kql_tables=_extract_kql_tables(kql_query),
        adx_row_count=_row_count(query_result),
        payload=payload,
    )
