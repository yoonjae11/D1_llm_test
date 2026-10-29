"""Opt-in, request-scoped model routing for local evaluation (not production).

All providers receive the same application prompts. Only wire format differs.
Credentials stay in environment variables, never in the model registry.
"""
from __future__ import annotations

import json
import logging
import os
import time
import zlib
from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence
from urllib.parse import urlsplit

import httpx

_PATHS = {"responses": "/openai/v1/responses", "chat": "/openai/v1/chat/completions",
          "anthropic": "/anthropic/v1/messages"}


@dataclass(frozen=True)
class ModelProfile:
    name: str
    deployment: str
    api: str
    endpoint: str
    api_key_env: str
    max_output_tokens: int = 32768
    reasoning_effort: str | None = None
    temperature: float | None = None
    max_output_tokens_retry: int | None = None
    repetition_retry_frequency_penalty: float | None = None


_ACTIVE: ContextVar[ModelProfile | None] = ContextVar("local_model", default=None)


def active_model() -> ModelProfile | None:
    return _ACTIVE.get()


@contextmanager
def use_model(profile: ModelProfile) -> Iterator[None]:
    token = _ACTIVE.set(profile)
    try:
        yield
    finally:
        _ACTIVE.reset(token)


def load_models(path: Path) -> dict[str, ModelProfile]:
    config = json.loads(path.read_text(encoding="utf-8-sig"))
    profiles = {}
    for name, value in config["models"].items():
        api = value["api"]
        if api not in _PATHS:
            raise ValueError(f"Unsupported API for {name}: {api}")
        endpoint = str(value.get("endpoint", config.get("endpoint", ""))).rstrip("/")
        parsed = urlsplit(endpoint)
        # Refuse accidental credential forwarding to non-Azure URLs or redirects.
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or parsed.port not in (None, 443)
                or not parsed.hostname.endswith((".services.ai.azure.com", ".openai.azure.com"))):
            raise ValueError(f"Expected an HTTPS Azure resource endpoint for {name}")
        if parsed.path in ("", "/"):
            endpoint += _PATHS[api]
        elif parsed.path != _PATHS[api]:
            raise ValueError(f"Endpoint path does not match {api} for {name}")
        budget = int(value.get("max_output_tokens", 32768))
        if budget < 1:
            raise ValueError(f"max_output_tokens must be positive for {name}")
        retry_budget = value.get("max_output_tokens_retry")
        if retry_budget is not None:
            retry_budget = int(retry_budget)
            if api != "chat" or retry_budget <= budget:
                raise ValueError(f"max_output_tokens_retry must exceed the chat budget for {name}")
        penalty = value.get("repetition_retry_frequency_penalty")
        if penalty is not None:
            penalty = float(penalty)
            if api != "chat" or not 0 < penalty <= 2:
                raise ValueError(f"repetition_retry_frequency_penalty must be in (0, 2] for chat model {name}")
        profiles[name] = ModelProfile(
            name=name, deployment=value.get("deployment", name), api=api, endpoint=endpoint,
            api_key_env=value.get("api_key_env", config.get("api_key_env", "AZURE_OPENAI_API_KEY")),
            max_output_tokens=budget, reasoning_effort=value.get("reasoning_effort"),
            temperature=value.get("temperature"),
            max_output_tokens_retry=retry_budget,
            repetition_retry_frequency_penalty=penalty,
        )
    return profiles


def require_key(profile: ModelProfile) -> str:
    key = os.environ.get(profile.api_key_env, "").strip()
    if not key:
        raise ValueError(f"Set {profile.api_key_env} in your private local.settings.json or .env")
    return key


def request_body(profile: ModelProfile, messages: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    normalized = [{"role": str(m.get("role", "user")), "content": str(m.get("content") or "")}
                  for m in messages]
    body: dict[str, Any] = {"model": profile.deployment}
    if profile.temperature is not None:
        body["temperature"] = profile.temperature
    if profile.api == "responses":
        body.update(input=normalized, store=False, max_output_tokens=profile.max_output_tokens)
        if profile.reasoning_effort:
            body["reasoning"] = {"effort": profile.reasoning_effort}
    elif profile.api == "chat":
        body.update(messages=normalized, max_tokens=profile.max_output_tokens)
    else:
        system = [m["content"] for m in normalized if m["role"] in ("system", "developer")]
        conversation = [m for m in normalized if m["role"] not in ("system", "developer")]
        if any(m["role"] not in ("user", "assistant") for m in conversation):
            raise ValueError("Local Anthropic adapter accepts text user/assistant messages only")
        body.update(messages=conversation, max_tokens=profile.max_output_tokens)
        if system:
            body["system"] = "\n\n".join(system)
    return body


def extract_text(api: str, data: Mapping[str, Any]) -> str:
    if api == "responses":
        if data.get("status") in ("incomplete", "failed", "cancelled") or data.get("error"):
            raise RuntimeError("Selected model returned an incomplete/failed response")
        text = "".join(str(c.get("text", "")) for item in data.get("output", [])
                       if item.get("type") == "message" for c in item.get("content", [])
                       if c.get("type") == "output_text")
    elif api == "chat":
        choices = data.get("choices") or []
        finish_reason = choices[0].get("finish_reason") if choices else "no_choice"
        if finish_reason == "content_filter":
            filtering = choices[0].get("content_filter_results") or {}
            error = filtering.get("error") or {}
            raise RuntimeError("Azure response content filtered: "
                               + str(error.get("message") or error.get("code") or "content_filter"))
        if finish_reason not in ("stop", None):
            usage = data.get("usage") or {}
            raise RuntimeError(
                "Selected model returned no complete text choice "
                f"(finish_reason={finish_reason}, "
                f"output_tokens={usage.get('completion_tokens', 'unknown')})"
            )
        text = choices[0].get("message", {}).get("content", "")
    else:
        if data.get("stop_reason") not in ("end_turn", "stop_sequence", None) or data.get("error"):
            raise RuntimeError("Selected model returned an incomplete/failed message")
        text = "".join(str(c.get("text", "")) for c in data.get("content", []) if c.get("type") == "text")
    if not isinstance(text, str) or not text.strip():
        raise RuntimeError("Selected model returned no final text (possibly exhausted its reasoning budget)")
    return text


def looks_degenerate(text: Any) -> bool:
    """Detect a token-repetition loop, not merely a long or repetitive query.

    Measured on 682 generated KQL queries: none compressed below 0.30 or
    repeated one line more than 3 times; looped outputs were <= 0.19 / >= 7.
    """
    text = str(text or "")
    if len(text) < 1000:
        return False
    lines = [line.strip()[:40] for line in text.splitlines() if line.strip()]
    repeated = max(Counter(lines).values()) if lines else 0
    ratio = len(zlib.compress(text.encode("utf-8"), 9)) / len(text.encode("utf-8"))
    return ratio < 0.22 or repeated >= 6


def _stream_enabled() -> bool:
    return os.environ.get("SENSEI_LOCAL_LLM_STREAM", "1").strip().lower() not in ("0", "false", "no", "off")


# Stream events that mean the model has started producing output (visible text,
# reasoning text or a new output item). Status/heartbeat events are excluded.
_RESPONSES_OUTPUT_EVENTS = ("response.output_item.added", "response.output_text.delta",
                            "response.reasoning_summary_text.delta", "response.reasoning_text.delta",
                            "response.content_part.added")


def _sse_events(lines: Iterator[str]) -> Iterator[tuple[str, Any]]:
    """Yield (event name, parsed data) from a server-sent-events stream."""
    event = ""
    for line in lines:
        if not line:
            event = ""
            continue
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            raw = line[5:].strip()
            if raw == "[DONE]":
                return
            try:
                yield event, json.loads(raw)
            except ValueError:
                continue


def _collect_stream(api: str, lines: Iterator[str]) -> tuple[dict[str, Any], float | None]:
    """Rebuild the non-streaming response body and note the first output time."""
    first_output: float | None = None

    def mark() -> None:
        nonlocal first_output
        if first_output is None:
            first_output = time.perf_counter()

    if api == "responses":
        final: dict[str, Any] = {"status": "failed"}
        for _, data in _sse_events(lines):
            kind = str(data.get("type", "")) if isinstance(data, dict) else ""
            if kind in _RESPONSES_OUTPUT_EVENTS:
                mark()
            elif kind in ("response.completed", "response.incomplete", "response.failed"):
                final = data.get("response") or final
            elif kind == "error":
                final = {"status": "failed", "error": data}
        return final, first_output
    if api == "chat":
        content, finish, filters, usage = [], None, None, {}
        for _, data in _sse_events(lines):
            if not isinstance(data, dict):
                continue
            if data.get("error"):
                return {"error": data["error"], "choices": []}, first_output
            if data.get("usage"):
                usage = data["usage"]
            for choice in data.get("choices") or []:
                delta = choice.get("delta") or {}
                if delta.get("content") or delta.get("reasoning_content") or delta.get("reasoning"):
                    mark()
                if delta.get("content"):
                    content.append(str(delta["content"]))
                finish = choice.get("finish_reason") or finish
                filters = choice.get("content_filter_results") or filters
        choice = {"message": {"content": "".join(content)}, "finish_reason": finish}
        if filters:
            choice["content_filter_results"] = filters
        return {"choices": [choice], "usage": usage}, first_output
    text, stop_reason, usage, error = [], None, {}, None
    for event, data in _sse_events(lines):
        if not isinstance(data, dict):
            continue
        kind = data.get("type", event)
        if kind == "message_start":
            usage.update((data.get("message") or {}).get("usage") or {})
        elif kind == "content_block_start":
            mark()
        elif kind == "content_block_delta":
            mark()
            delta = data.get("delta") or {}
            if delta.get("type") == "text_delta":
                text.append(str(delta.get("text", "")))
        elif kind == "message_delta":
            stop_reason = (data.get("delta") or {}).get("stop_reason") or stop_reason
            usage.update(data.get("usage") or {})
        elif kind == "error":
            error = data.get("error") or data
    result: dict[str, Any] = {"content": [{"type": "text", "text": "".join(text)}],
                              "stop_reason": stop_reason, "usage": usage}
    if error:
        result["error"] = error
    return result, first_output


_SENT_EVENTS = ("http11.send_request_body.complete", "http2.send_request_body.complete")


def _request_tracer() -> tuple[dict[str, float], Any]:
    """httpx trace hook: remember when the request body has been fully sent."""
    marks: dict[str, float] = {}

    def trace(event: str, info: Any) -> None:
        if event in _SENT_EVENTS and "sent" not in marks:
            marks["sent"] = time.perf_counter()
    return marks, trace


def _split_timing(started: float, ended: float, marks: Mapping[str, float]) -> dict[str, float]:
    """network = connect (DNS/TCP) + TLS + upload; server = request sent -> response done."""
    sent = marks.get("sent", started)
    return {"request_ms": (ended - started) * 1000.0, "network_ms": (sent - started) * 1000.0,
            "server_ms": (ended - sent) * 1000.0}


def _post(client: httpx.Client, profile: ModelProfile, headers: Mapping[str, str],
          request: Mapping[str, Any], stream: bool) -> tuple[httpx.Response, dict | None, dict]:
    """Send one request; split network setup from server time and note stream timing."""
    marks, trace = _request_tracer()
    started = time.perf_counter()
    if not stream:
        response = client.post(profile.endpoint, headers=headers, json=request, extensions={"trace": trace})
        return response, None, {**_split_timing(started, time.perf_counter(), marks), "streamed": False}
    body = {**request, "stream": True}
    if profile.api == "chat":
        body["stream_options"] = {"include_usage": True}
    with client.stream("POST", profile.endpoint, headers=headers, json=body,
                       extensions={"trace": trace}) as response:
        headers_at = time.perf_counter()
        if (response.status_code != 200
                or "text/event-stream" not in response.headers.get("content-type", "")):
            response.read()
            return response, None, {**_split_timing(started, time.perf_counter(), marks),
                                    "headers_ms": (headers_at - started) * 1000.0, "streamed": False}
        data, first_output = _collect_stream(profile.api, response.iter_lines())
    ended = time.perf_counter()
    timing = {**_split_timing(started, ended, marks), "headers_ms": (headers_at - started) * 1000.0,
              "streamed": True}
    if first_output is not None:
        timing["first_output_ms"] = (first_output - started) * 1000.0
        timing["generation_ms"] = (ended - first_output) * 1000.0
    return response, data, timing


_BASELINE_MESSAGES = [{"role": "user", "content": "Reply with the word OK."}]


def measure_baseline_ms(profile: ModelProfile, repeats: int = 3, *,
                        transport: httpx.BaseTransport | None = None) -> float | None:
    """Median server time of a minimal request to this deployment.

    Server time starts when the request is fully sent, so connection setup
    (DNS/TCP/TLS, measured per call separately) is excluded. What remains for
    a request with almost no model work is the fixed server overhead (gateway,
    queue admission, request handling); server time minus this baseline
    approximates model inference. Unlike token streaming this works the same
    for deployments that buffer the whole response.
    """
    key = require_key(profile)
    headers = {"Content-Type": "application/json"}
    if profile.api == "anthropic":
        headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
    else:
        headers["api-key"] = key
    body = request_body(profile, _BASELINE_MESSAGES)
    body["max_output_tokens" if profile.api == "responses" else "max_tokens"] = 16
    samples = []
    for _ in range(max(1, repeats)):
        # A fresh client per sample, like complete(): each real call opens its
        # own connection, so the baseline includes the same TLS handshake.
        with httpx.Client(timeout=float(os.environ.get("OPENAI_TIMEOUT_SECONDS", "120")),
                          follow_redirects=False, transport=transport) as client:
            marks, trace = _request_tracer()
            started = time.perf_counter()
            try:
                response = client.post(profile.endpoint, headers=headers, json=body,
                                       extensions={"trace": trace})
            except httpx.HTTPError:
                continue
            if response.status_code < 500 and response.status_code != 429:
                samples.append(_split_timing(started, time.perf_counter(), marks)["server_ms"])
    if not samples:
        return None
    samples.sort()
    return samples[len(samples) // 2]


def usage_tokens(api: str, usage: Mapping[str, Any] | None) -> dict[str, int | None]:
    """Input, output and reasoning token counts in one shape for every API."""
    usage = usage or {}
    if api == "responses":
        details = usage.get("output_tokens_details") or {}
        return {"input": usage.get("input_tokens"), "output": usage.get("output_tokens"),
                "reasoning": details.get("reasoning_tokens")}
    if api == "chat":
        details = usage.get("completion_tokens_details") or {}
        prompt, total = usage.get("prompt_tokens"), usage.get("total_tokens")
        # Some providers (e.g. xAI) leave reasoning out of completion_tokens but
        # count it in total_tokens; total - prompt is everything the model generated.
        output = (total - prompt if isinstance(total, int) and isinstance(prompt, int) and total > prompt
                  else usage.get("completion_tokens"))
        reasoning = details.get("reasoning_tokens")
        if reasoning is None:  # DeepSeek reports it at the top level of usage
            reasoning = usage.get("reasoning_tokens")
        return {"input": prompt, "output": output, "reasoning": reasoning}
    # Anthropic reports thinking inside output_tokens; there is no separate count.
    return {"input": usage.get("input_tokens"), "output": usage.get("output_tokens"), "reasoning": None}


def complete(profile: ModelProfile, messages: Sequence[Mapping[str, Any]], *,
             transport: httpx.BaseTransport | None = None) -> dict[str, Any]:
    key = require_key(profile)
    headers = {"Content-Type": "application/json"}
    if profile.api == "anthropic":
        headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
    else:
        headers["api-key"] = key
    # A bounded transport retry keeps the SAME deployment and request. Set 0
    # for single-attempt evaluations; never retry authentication or bad input.
    retries = min(2, max(0, int(os.environ.get("SENSEI_LOCAL_LLM_MAX_RETRIES", "1"))))
    attempts = 0
    transport_retries = retries
    request = request_body(profile, messages)
    expanded_output = False
    penalized_output = False
    stream = _stream_enabled()
    calls: list[dict[str, Any]] = []  # one timing/usage entry per HTTP attempt
    retry_wait_ms = 0.0
    streamed_data: dict | None = None
    try:
        with httpx.Client(timeout=float(os.environ.get("OPENAI_TIMEOUT_SECONDS", "120")),
                          follow_redirects=False, transport=transport) as client:
            while True:
                attempts += 1
                try:
                    response, streamed_data, timing = _post(client, profile, headers, request, stream)
                except (httpx.TimeoutException, httpx.NetworkError):
                    if transport_retries <= 0:
                        raise
                    transport_retries -= 1
                    logging.getLogger(__name__).warning("Model %s transport retry %s", profile.name, attempts)
                    time.sleep(0.5 * attempts)
                    retry_wait_ms += 500.0 * attempts
                    continue
                calls.append(timing)
                if (stream and response.status_code == 400 and profile.api == "chat"
                        and "stream" in (response.text or "").lower()):
                    # A route that rejects streaming options: measure without them.
                    stream = False
                    logging.getLogger(__name__).warning("Model %s rejected streaming; retrying unstreamed",
                                                        profile.name)
                    continue
                if streamed_data is not None:
                    timing["usage"] = streamed_data.get("usage") or {}
                if response.status_code == 200 and profile.api == "chat":
                    payload = streamed_data if streamed_data is not None else response.json()
                    if streamed_data is None and isinstance(payload, dict):
                        timing["usage"] = payload.get("usage") or {}
                    choices = payload.get("choices") or [] if isinstance(payload, dict) else []
                    truncated = bool(choices and choices[0].get("finish_reason") == "length")
                    content = (choices[0].get("message") or {}).get("content") if choices else ""
                    if ((truncated or looks_degenerate(content)) and not penalized_output
                            and profile.repetition_retry_frequency_penalty):
                        # Some chat models fall into a token-repetition loop at
                        # temperature 0 (truncated, or garbage ending in "stop").
                        # Retry once with a mild repetition penalty; same prompt.
                        request = {**request, "frequency_penalty": profile.repetition_retry_frequency_penalty}
                        penalized_output = True
                        logging.getLogger(__name__).warning(
                            "Model %s output looped; retrying with frequency_penalty=%s",
                            profile.name, profile.repetition_retry_frequency_penalty,
                        )
                        continue
                    if truncated and not expanded_output and profile.max_output_tokens_retry:
                        # A truncated response is unusable KQL. Retry only with
                        # a larger model-approved budget, never unchanged input.
                        request = {**request, "max_tokens": profile.max_output_tokens_retry}
                        expanded_output = True
                        logging.getLogger(__name__).warning(
                            "Model %s output truncated at %s tokens; retrying at %s",
                            profile.name, profile.max_output_tokens,
                            profile.max_output_tokens_retry,
                        )
                        continue
                if response.status_code not in {429, 500, 502, 503, 504} or transport_retries <= 0:
                    break
                transport_retries -= 1
                logging.getLogger(__name__).warning("Model %s HTTP %s retry %s", profile.name, response.status_code, attempts)
                time.sleep(0.5 * attempts)
                retry_wait_ms += 500.0 * attempts
        if response.status_code != 200:
            detail = ""
            try:
                payload = response.json()
                error = payload.get("error", payload) if isinstance(payload, dict) else payload
                if isinstance(payload, dict) and payload.get("choices"):
                    filtered = next((choice for choice in payload["choices"]
                                     if choice.get("finish_reason") == "content_filter"), None)
                    if filtered:
                        error = ((filtered.get("content_filter_results") or {}).get("error")
                                 or {"code": "content_filter", "message": "Azure response content filtered"})
                if isinstance(error, dict):
                    detail = ": ".join(str(error[field]) for field in ("code", "message", "detail")
                                       if error.get(field))
                elif isinstance(error, (str, list)):
                    detail = str(error)
            except (ValueError, AttributeError):
                pass
            # Some Azure model routes return top-level detail/message or plain
            # text instead of the OpenAI error envelope. Do not lose that cause.
            detail = detail or response.text or response.reason_phrase
            detail = " ".join(detail.replace(key, "[redacted]").split())[:1200]
            raise RuntimeError(f"Model {profile.name} ({profile.api}): HTTP {response.status_code}; "
                               + (detail or "check deployment, endpoint, key, quota and supported parameters"))
        data = streamed_data if streamed_data is not None else response.json()
    except httpx.HTTPError:
        raise RuntimeError(f"Model {profile.name}: network/timeout error") from None
    except ValueError:
        raise RuntimeError(f"Model {profile.name}: invalid response or timeout configuration") from None
    if not isinstance(data, dict):
        raise RuntimeError(f"Model {profile.name}: expected a JSON response object")
    if calls and "usage" not in calls[-1]:
        calls[-1]["usage"] = data.get("usage") or {}
    return {"text": extract_text(profile.api, data), "model": profile.deployment,
            "response_id": "", "usage": data.get("usage", {}), "transport_attempts": attempts,
            "metrics": call_metrics(profile.api, calls, retry_wait_ms)}


def call_metrics(api: str, calls: Sequence[Mapping[str, Any]], retry_wait_ms: float) -> dict[str, Any]:
    """Sum timing and tokens over the HTTP attempts of one model call.

    generation_ms (first output event -> last event) is the closest client-side
    measure of model inference; it exists only for streamed attempts.
    """
    tokens = [usage_tokens(api, c.get("usage")) for c in calls]

    def total(key: str) -> int | None:
        values = [t[key] for t in tokens if isinstance(t.get(key), int)]
        return sum(values) if values else None

    streamed = [c for c in calls if c.get("streamed") and "generation_ms" in c]
    return {
        "attempts": len(calls),
        "streamed": bool(calls) and len(streamed) == len(calls),
        "request_ms": sum(float(c.get("request_ms", 0.0)) for c in calls),
        "network_ms": sum(float(c.get("network_ms", 0.0)) for c in calls),
        "server_ms": sum(float(c.get("server_ms", c.get("request_ms", 0.0))) for c in calls),
        "headers_ms": sum(float(c.get("headers_ms", 0.0)) for c in calls if "headers_ms" in c),
        "first_output_ms": sum(float(c["first_output_ms"]) for c in streamed),
        "generation_ms": sum(float(c["generation_ms"]) for c in streamed) if streamed else None,
        "retry_wait_ms": retry_wait_ms,
        "input_tokens": total("input"),
        "output_tokens": total("output"),
        "reasoning_tokens": total("reasoning"),
    }
