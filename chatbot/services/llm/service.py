"""Application-facing LLM service factories."""

from __future__ import annotations

from typing import Optional

from .ports import LLMPort, TextGenerationPort


def default_llm_service() -> LLMPort:
    from chatbot.adapters.defaults import DefaultLLMAdapter

    return DefaultLLMAdapter()


def complete_chat(*args, service: Optional[LLMPort] = None, **kwargs) -> str:
    """Run a completion through the injectable LLM boundary."""

    return (service or default_llm_service()).complete(*args, **kwargs)


def generate_alert_text(
    prompt: str,
    *,
    service: Optional[TextGenerationPort] = None,
) -> str:
    """Use the behavior-compatible alert narration adapter."""

    if service is None:
        from chatbot.adapters.defaults import LegacyAlertNarrationAdapter

        service = LegacyAlertNarrationAdapter()
    return service.generate(prompt)


__all__ = ["complete_chat", "default_llm_service", "generate_alert_text"]
