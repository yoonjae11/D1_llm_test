"""Compatibility-preserving adapters over the existing provider modules."""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Optional

from chatbot.services.memory.ports import OperationalStoragePort


class DefaultLLMAdapter:
    """Delegate LLM operations to the established ``chatbot.llm_client``."""

    def complete(
        self,
        messages: Iterable[Mapping[str, Any]],
        *,
        deployment: Optional[str] = None,
        temperature: Optional[float] = 0,
        max_tokens: Optional[int] = None,
        insecure_ssl: Optional[bool] = None,
    ) -> str:
        from chatbot import llm_client

        kwargs = {"temperature": temperature}
        if deployment is not None:
            kwargs["deployment"] = deployment
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        if insecure_ssl is not None:
            kwargs["insecure_ssl"] = insecure_ssl
        return llm_client.call_chat_completion(messages, **kwargs)

    def respond(
        self,
        input_text: str,
        *,
        previous_response_id: Optional[str] = None,
        instructions: Optional[str] = None,
        store: bool = True,
        deployment: Optional[str] = None,
        insecure_ssl: Optional[bool] = None,
    ) -> Mapping[str, Any]:
        from chatbot import llm_client

        kwargs = {"store": store}
        if previous_response_id is not None:
            kwargs["previous_response_id"] = previous_response_id
        if instructions is not None:
            kwargs["instructions"] = instructions
        if deployment is not None:
            kwargs["deployment"] = deployment
        if insecure_ssl is not None:
            kwargs["insecure_ssl"] = insecure_ssl
        return llm_client.call_user_facing_response(input_text, **kwargs)


class DefaultADXQueryAdapter:
    """Delegate read-only queries to the established ADX singleton."""

    def execute_query(self, query: str, suppress_error_log: bool = False) -> Any:
        from chatbot import adx_client

        return adx_client.adx_manager.execute_query(
            query,
            suppress_error_log=suppress_error_log,
        )


class LegacyAlertNarrationAdapter:
    """Preserve the existing non-Azure ChatOpenAI alert narration call."""

    def generate(self, prompt: str) -> str:
        from langchain_openai import ChatOpenAI

        model = ChatOpenAI(model="gpt-4o-mini", temperature=0.6)
        response = model.invoke(prompt)
        return str(getattr(response, "content", None) or "").strip()


class LegacyRegisteredUserProfileAdapter:
    """Delegate onboarding-profile reads to the established Functions helper."""

    def get_registered_user(self, app_user_id: str) -> Mapping[str, Any]:
        # Resolve at call time so ``functions.shared.get_registered_user``
        # remains the operator/test monkeypatch seam and keeps its existing
        # AzureWebJobsAppStorage / APP_USERS_TABLE_NAME behavior.
        from functions import shared

        return shared.get_registered_user(app_user_id) or {}


def create_operational_storage_adapter(
    connection_string: Optional[str] = None,
    *,
    fail_soft: bool = True,
) -> OperationalStoragePort:
    from chatbot import operational_storage

    if connection_string is None:
        return operational_storage.OperationalTableRepository(fail_soft=fail_soft)
    return operational_storage.OperationalTableRepository(
        connection_string=connection_string,
        fail_soft=fail_soft,
    )


__all__ = [
    "DefaultADXQueryAdapter",
    "DefaultLLMAdapter",
    "LegacyAlertNarrationAdapter",
    "LegacyRegisteredUserProfileAdapter",
    "create_operational_storage_adapter",
]
