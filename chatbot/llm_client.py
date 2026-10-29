"""Common OpenAI/Azure OpenAI chat completion client."""

from __future__ import annotations

import os
from dataclasses import replace
from functools import lru_cache
from typing import Any, Iterable, Mapping, Optional

from openai import OpenAI

from runtime_environment import env_bool as _env_bool, env_int

from .config import get_settings
from .local_models import active_model, complete as complete_local_model

AZURE_AI_SCOPE = "https://ai.azure.com/.default"


def _max_retries() -> int:
    """Retry count for 429 / 5xx / connection errors (OPENAI_MAX_RETRIES, default 4).

    The OpenAI SDK retries these automatically with exponential backoff + jitter
    and honors the server's Retry-After header; the SDK default (2) is too low to
    survive a quota burst (e.g. everyone replying right after the daily-summary
    broadcast), so we raise it explicitly and make it operator-tunable.
    """
    value = env_int("OPENAI_MAX_RETRIES", 4)
    return max(0, min(value, 10))


def _normalize_azure_base_url(base_url: str) -> str:
    normalized = str(base_url or "").strip()
    lowered = normalized.lower().rstrip("/")
    suffix = "/chat/completions"
    if lowered.endswith(suffix):
        normalized = normalized[: -len(suffix)]
    return normalized.rstrip("/") + "/"


def _http_client_kwargs(insecure_ssl: bool) -> dict[str, Any]:
    if not insecure_ssl:
        return {}
    import httpx

    return {"http_client": httpx.Client(verify=False)}


def _supports_custom_temperature(model_name: str) -> bool:
    return not str(model_name or "").lower().startswith("gpt-5")


def _is_reasoning_model(model_name: str) -> bool:
    name = str(model_name or "").lower()
    return name.startswith("gpt-5") or name.startswith("o1") or name.startswith("o3") or name.startswith("o4")


_VALID_EFFORTS = {"minimal", "low", "medium", "high"}


def _reasoning_effort(env_name: str, default: str) -> str:
    value = str(os.environ.get(env_name, "")).strip().lower() or default
    return value if value in _VALID_EFFORTS else default


def _message_dict(message: Mapping[str, Any]) -> dict[str, Any]:
    role = str(message.get("role") or "user")
    content = message.get("content", "")
    if isinstance(content, list):
        content = " ".join(str(part) for part in content)
    return {"role": role, "content": str(content or "")}


@lru_cache(maxsize=4)
def get_llm_client(insecure_ssl: bool = False) -> OpenAI:
    settings = get_settings()
    timeout = settings.openai_timeout_seconds
    http_kwargs = _http_client_kwargs(insecure_ssl)

    if settings.ai_provider == "azure_openai_v1":
        if not settings.azure_openai_base_url:
            raise ValueError("AZURE_OPENAI_BASE_URL is required when AI_PROVIDER=azure_openai_v1")
        if not settings.azure_openai_default_deployment:
            raise ValueError(
                "AZURE_OPENAI_DEFAULT_DEPLOYMENT or AZURE_OPENAI_CHAT_DEPLOYMENT is required "
                "when AI_PROVIDER=azure_openai_v1"
            )

        base_url = _normalize_azure_base_url(settings.azure_openai_base_url)
        if settings.azure_openai_auth_mode == "api_key":
            if not settings.azure_openai_api_key:
                raise ValueError(
                    "AZURE_OPENAI_API_KEY is required when AZURE_OPENAI_AUTH_MODE=api_key"
                )
            return OpenAI(
                base_url=base_url,
                api_key=settings.azure_openai_api_key,
                timeout=timeout,
                max_retries=_max_retries(),
                **http_kwargs,
            )

        if settings.azure_openai_auth_mode == "managed_identity":
            from azure.identity import DefaultAzureCredential, get_bearer_token_provider

            token_provider = get_bearer_token_provider(
                DefaultAzureCredential(exclude_environment_credential=True),
                AZURE_AI_SCOPE,
            )
            return OpenAI(
                base_url=base_url,
                api_key=token_provider,
                timeout=timeout,
                max_retries=_max_retries(),
                **http_kwargs,
            )

        raise ValueError(f"Unsupported AZURE_OPENAI_AUTH_MODE: {settings.azure_openai_auth_mode}")

    if settings.ai_provider != "openai":
        raise ValueError(f"Unsupported AI_PROVIDER: {settings.ai_provider}")

    if not settings.openai_api_key:
        raise ValueError("OPENAI_API_KEY is required when AI_PROVIDER=openai")
    return OpenAI(api_key=settings.openai_api_key, timeout=timeout, max_retries=_max_retries(), **http_kwargs)


def get_default_model_name() -> str:
    profile = active_model()
    if profile is not None:
        return profile.deployment
    settings = get_settings()
    if settings.ai_provider == "azure_openai_v1":
        return settings.azure_openai_default_deployment
    return settings.openai_chat_model


def call_chat_completion(
    messages: Iterable[Mapping[str, Any]],
    *,
    deployment: Optional[str] = None,
    temperature: Optional[float] = 0,
    max_tokens: Optional[int] = None,
    insecure_ssl: Optional[bool] = None,
) -> str:
    profile = active_model()
    if profile is not None:
        # Internal graph calls request temperature=0. Local provider routing
        # must preserve that setting rather than silently using the provider
        # default. Responses/Anthropic deployments retain their supported
        # settings; some deployed versions reject the temperature parameter.
        if profile.temperature is None and temperature is not None and profile.api == "chat":
            profile = replace(profile, temperature=temperature)
        return complete_local_model(profile, [_message_dict(message) for message in messages])["text"]
    use_insecure_ssl = _env_bool("CHATBOT_INSECURE_SSL", False) if insecure_ssl is None else insecure_ssl
    client = get_llm_client(insecure_ssl=use_insecure_ssl)
    model_name = deployment or get_default_model_name()

    kwargs: dict[str, Any] = {
        "model": model_name,
        "messages": [_message_dict(message) for message in messages],
    }
    if temperature is not None and _supports_custom_temperature(model_name):
        kwargs["temperature"] = temperature
    # Internal calls (intent routing, planning, KQL repair) do not need deep
    # reasoning; low effort cuts latency substantially on gpt-5.
    if _is_reasoning_model(model_name):
        kwargs["reasoning_effort"] = _reasoning_effort("CHATBOT_INTERNAL_REASONING_EFFORT", "low")
        if max_tokens is not None:
            # Reasoning models reject ``max_tokens`` AND count hidden reasoning
            # tokens against the completion budget, so a tiny cap (e.g. the 4-12
            # tokens NLU classifiers pass) yields an empty answer. Use
            # ``max_completion_tokens`` with headroom for the reasoning tokens.
            kwargs["max_completion_tokens"] = int(max_tokens) + 512
    elif max_tokens is not None:
        kwargs["max_tokens"] = max_tokens

    response = client.chat.completions.create(**kwargs)
    return response.choices[0].message.content or ""


def _extract_response_text(response: Any) -> str:
    """Best-effort extraction of assistant text from a Responses API object."""
    text = getattr(response, "output_text", None)
    if text:
        return str(text)
    parts: list[str] = []
    for item in getattr(response, "output", None) or []:
        for content in getattr(item, "content", None) or []:
            value = getattr(content, "text", None)
            if value:
                parts.append(str(value))
    return "".join(parts)


def call_user_facing_response(
    input_text: str,
    *,
    previous_response_id: Optional[str] = None,
    instructions: Optional[str] = None,
    store: bool = True,
    deployment: Optional[str] = None,
    insecure_ssl: Optional[bool] = None,
) -> dict[str, Any]:
    """Generate a user-facing reply via the Azure OpenAI / OpenAI Responses API.

    Only user-facing turns belong on the response chain. Internal LLM calls
    (intent routing, planning, KQL repair) must keep using ``call_chat_completion``
    so they do not pollute the per-user conversation memory.

    Returns ``{"text", "response_id", "model", "usage"}``. The caller persists
    ``response_id`` as ``LatestResponseId`` and passes it back as
    ``previous_response_id`` on the next user-facing turn.
    """
    profile = active_model()
    if profile is not None:
        messages = []
        if instructions:
            messages.append({"role": "system", "content": instructions})
        messages.append({"role": "user", "content": str(input_text or "")})
        # Local comparisons use the graph-rendered context, never a response
        # id created by a different deployment or provider.
        return complete_local_model(profile, messages)
    use_insecure_ssl = _env_bool("CHATBOT_INSECURE_SSL", False) if insecure_ssl is None else insecure_ssl
    client = get_llm_client(insecure_ssl=use_insecure_ssl)
    model_name = deployment or get_default_model_name()

    kwargs: dict[str, Any] = {"model": model_name, "input": str(input_text or ""), "store": bool(store)}
    if previous_response_id:
        kwargs["previous_response_id"] = previous_response_id
    if instructions:
        kwargs["instructions"] = instructions
    # User-facing generation: low effort keeps coaching quality while cutting
    # the multi-minute latencies seen with default (medium) reasoning on gpt-5.
    if _is_reasoning_model(model_name):
        kwargs["reasoning"] = {"effort": _reasoning_effort("CHATBOT_RESPONSE_REASONING_EFFORT", "low")}

    response = client.responses.create(**kwargs)
    usage = getattr(response, "usage", None)
    return {
        "text": _extract_response_text(response),
        "response_id": str(getattr(response, "id", "") or ""),
        "model": model_name,
        "usage": usage.__dict__ if hasattr(usage, "__dict__") else (usage or {}),
    }
