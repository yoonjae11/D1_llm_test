"""Shared LLM, conversation-state, text, and diagnostic helpers for graph nodes."""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import sys
import time
from typing import Any, Dict, Iterable, List, Mapping, Optional

from langchain_core.runnables import RunnableLambda

from chatbot.config import get_settings
from chatbot.graph.state import AgentState, format_recent_history
from chatbot.services.llm import LLMPort, default_llm_service


logger = logging.getLogger(__name__)
_default_llm_port: LLMPort = default_llm_service()


# ---------------------------------------------------------------------------
# LLM integration
# ---------------------------------------------------------------------------

def call_chat_completion(
    messages: Iterable[Mapping[str, Any]],
    *,
    deployment: Optional[str] = None,
    temperature: Optional[float] = 0,
    max_tokens: Optional[int] = None,
    insecure_ssl: Optional[bool] = None,
) -> str:
    """Call the default LLM port through the established patch seam."""

    return _default_llm_port.complete(
        messages,
        deployment=deployment,
        temperature=temperature,
        max_tokens=max_tokens,
        insecure_ssl=insecure_ssl,
    )


def _message_to_dict(message: Any) -> Dict[str, str]:
    message_type = str(getattr(message, "type", "") or "").lower()
    role = "assistant" if message_type in {"ai", "assistant"} else "system" if message_type == "system" else "user"
    content = getattr(message, "content", message)
    if isinstance(content, list):
        content = " ".join(str(part) for part in content)
    return {"role": role, "content": str(content or "")}


def _llm_invoke(prompt_value: Any) -> str:
    if hasattr(prompt_value, "to_messages"):
        messages = [_message_to_dict(message) for message in prompt_value.to_messages()]
    elif isinstance(prompt_value, list):
        messages = [_message_to_dict(message) for message in prompt_value]
    else:
        messages = [{"role": "user", "content": str(prompt_value or "")}]
    return call_chat_completion(messages, temperature=0)


llm = RunnableLambda(_llm_invoke)


# ---------------------------------------------------------------------------
# Conversation state
# ---------------------------------------------------------------------------

def latest_question(state: AgentState) -> str:
    """Return the current user message using the existing graph-state shape."""

    messages = state.get("messages", [])
    if not messages:
        return ""
    content = getattr(messages[-1], "content", "")
    if isinstance(content, list):
        return " ".join(str(item) for item in content)
    return str(content or "").strip()


def stored_turns_history(state: AgentState, max_turns: int = 5) -> str:
    """Render durable conversation turns loaded by ``prepare_context``."""

    turns = state.get("conversation_recent_turns") or []
    lines: List[str] = []
    for turn in turns[-(max_turns * 2):]:
        if not isinstance(turn, dict):
            continue
        role = "Assistant" if str(turn.get("role", "")).lower() in {"assistant", "ai"} else "User"
        text = str(turn.get("message_text", "") or "").strip()
        if len(text) > 320:
            text = text[:320] + "..."
        if text:
            lines.append(f"{role}: {text}")
    return "\n".join(lines)


def effective_history(state: AgentState, max_turns: int = 5) -> str:
    """Use in-memory history when present; otherwise use durable turns."""

    messages = state.get("messages") or []
    if isinstance(messages, list) and len(messages) > 1:
        return format_recent_history(messages, max_turns=max_turns)
    stored = stored_turns_history(state, max_turns=max_turns)
    current = latest_question(state)
    if current:
        stored = (stored + "\n" if stored else "") + f"User: {current[:320]}"
    return stored


def last_assistant_text(state: AgentState) -> str:
    """Return the most recent assistant text from state or durable turns."""

    messages = state.get("messages") or []
    if isinstance(messages, list):
        for message in reversed(messages[:-1] if messages else []):
            if str(getattr(message, "type", "")).lower() in {"ai", "assistant"}:
                content = getattr(message, "content", "")
                if isinstance(content, list):
                    return " ".join(str(item) for item in content)
                return str(content or "")
    turns = state.get("conversation_recent_turns") or []
    for turn in reversed(turns):
        if isinstance(turn, dict) and str(turn.get("role", "")).lower() in {"assistant", "ai"}:
            return str(turn.get("message_text", "") or "")
    return ""


# ---------------------------------------------------------------------------
# Text, JSON, and query rendering
# ---------------------------------------------------------------------------

def clean_model_text(text: Any) -> str:
    """Strip common model code fences without changing the generated payload."""

    if not text:
        return ""
    cleaned = str(text).strip().lstrip("\ufeff").strip()
    # Models also use the standard `kusto` fence for KQL. Removing only the
    # backticks left that language name as an apparent unapproved data source.
    blocks = re.findall(r"(?m)^[ \t]*(```|~~~)(?:json|kql|kusto)?[ \t]*\r?\n(.*?)^[ \t]*\1[ \t]*(?:\r?$)", cleaned,
                        flags=re.IGNORECASE | re.DOTALL)
    if len(blocks) == 1:
        cleaned = blocks[0][1].strip()
    elif not blocks:
        # Do not concatenate several fenced queries. Their full output must
        # reach validation/repair unchanged.
        wrapped = re.fullmatch(r"(```|~~~)(?:json|kql|kusto)?[ \t]*(.*?)\1", cleaned,
                               flags=re.IGNORECASE | re.DOTALL)
        if wrapped:
            cleaned = wrapped[2].strip()
        else:
            # A complete API response can omit its closing Markdown fence.
            # Remove only that outer opener; KQL/JSON validation still owns
            # the body. Incomplete API responses are rejected by the adapter.
            opener = re.match(r"(?:```|~~~)(?:json|kql|kusto)?[ \t]*\r?\n", cleaned,
                              flags=re.IGNORECASE)
            if opener and not re.search(r"(?m)^[ \t]*(?:```|~~~)", cleaned[opener.end():]):
                cleaned = cleaned[opener.end():].strip()
    # Accept traces/candidates already passed through the former fence cleaner.
    # An isolated language label is packaging even when comments, a JSON
    # array, or a query-local relation follow it. Never remove inline tokens
    # or language names inside the actual payload.
    cleaned = re.sub(r"(?i)^(?:json|kql|kusto)[ \t]*\r?\n",
                     "", cleaned)
    return cleaned.strip()


def safe_json_loads(text: Any, default: Any) -> Any:
    try:
        return json.loads(text)
    except Exception:
        return default


def format_kql_for_log(kql: str) -> str:
    text = str(kql or "").strip()
    text = re.sub(r"\s+\|\s+", "\n| ", text)
    return re.sub(r";\s*", ";\n", text)


def now_string() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------

def legacy_debug_enabled() -> bool:
    value = str(os.environ.get("CHATBOT_DEBUG", "")).strip().lower()
    return value in {"1", "true", "yes", "y", "on"}


def debug_enabled() -> bool:
    value = str(os.environ.get("CHATBOT_DEBUG", "")).strip().lower()
    return value in {"1", "true", "yes", "y", "on"} if value else get_settings().chatbot_debug


def _print_debug(message: str) -> None:
    try:
        print(message)
    except UnicodeEncodeError:
        encoding = sys.stdout.encoding or "utf-8"
        safe = str(message).encode(encoding, errors="replace").decode(encoding, errors="replace")
        print(safe)


def debug(message: str) -> None:
    if debug_enabled():
        _print_debug(message)


def legacy_debug(message: str) -> None:
    if legacy_debug_enabled():
        _print_debug(message)


def elapsed_ms(start_perf: float) -> int:
    return int((time.perf_counter() - start_perf) * 1000)


__all__ = [
    "call_chat_completion",
    "clean_model_text",
    "debug",
    "effective_history",
    "elapsed_ms",
    "format_kql_for_log",
    "last_assistant_text",
    "latest_question",
    "legacy_debug",
    "llm",
    "logger",
    "now_string",
    "safe_json_loads",
]


def conversation_context(state: dict) -> dict:
    """Explicit runtime inputs; never forward inbound payloads or arbitrary state."""
    return {
        "question": latest_question(state),
        "history": effective_history(state, max_turns=5),
        "rolling_summary": str(state.get("conversation_rolling_summary") or "")[:4000],
        "reply_to_text": str(state.get("reply_to_text") or "")[:600],
        "reply_to_role": str(state.get("reply_to_role") or ""),
        "context_mode": str(state.get("context_mode") or ""),
    }
