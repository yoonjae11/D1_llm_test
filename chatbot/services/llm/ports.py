"""LLM service interfaces used by graph nodes and background flows."""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Optional, Protocol


class LLMPort(Protocol):
    def complete(
        self,
        messages: Iterable[Mapping[str, Any]],
        *,
        deployment: Optional[str] = None,
        temperature: Optional[float] = 0,
        max_tokens: Optional[int] = None,
        insecure_ssl: Optional[bool] = None,
    ) -> str: ...

    def respond(
        self,
        input_text: str,
        *,
        previous_response_id: Optional[str] = None,
        instructions: Optional[str] = None,
        store: bool = True,
        deployment: Optional[str] = None,
        insecure_ssl: Optional[bool] = None,
    ) -> Mapping[str, Any]: ...


class TextGenerationPort(Protocol):
    """Small seam for a legacy single-prompt generation call."""

    def generate(self, prompt: str) -> str: ...


__all__ = ["LLMPort", "TextGenerationPort"]
