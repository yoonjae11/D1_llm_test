"""User-facing Responses API orchestration with chat-completions fallback."""

from __future__ import annotations

from typing import Any, Callable, Mapping

from langchain_core.output_parsers import StrOutputParser

from chatbot.config import get_settings


ResponseCall = Callable[..., Mapping[str, Any]]
DebugCall = Callable[[str], None]


def generate_user_facing(
    *,
    state: Mapping[str, Any],
    prompt: Any,
    variables: Mapping[str, Any],
    response_call: ResponseCall,
    fallback_model: Any,
    debug: DebugCall,
    settings: Any = None,
) -> tuple[str, str]:
    """Return ``(text, response_id)`` while preserving the legacy fallback."""

    settings = settings or get_settings()
    if settings.use_azure_responses_api:
        try:
            rendered = prompt.format(**variables)
            if not isinstance(rendered, str):
                rendered = str(rendered)
            previous = state.get("previous_response_id") or None
            result = response_call(
                rendered,
                previous_response_id=previous,
                store=settings.response_store,
            )
            text = str(result.get("text") or "")
            if text.strip():
                return text, str(result.get("response_id") or "")
            debug("  -> Responses API returned empty text; falling back to chat.completions.")
        except Exception as exc:  # pragma: no cover - defensive fallback path.
            debug(f"  -> Responses API call failed, falling back: {exc}")
    chain = prompt | fallback_model | StrOutputParser()
    return chain.invoke(dict(variables)), ""


__all__ = ["generate_user_facing"]
