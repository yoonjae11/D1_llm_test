"""Authenticated REST boundary for non-Telegram Sensei channels.

Conversational Care calls this Function-level authenticated endpoint with an
opaque external user id.  It never receives a phone number and never returns
KQL, ADX rows, LangGraph state, or traces.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, Tuple

import azure.functions as func
import azure.durable_functions as df

from chatbot.availability_refresh import enqueue_metric_availability_refresh
from chatbot.entrypoint import handle_user_message
from functions.interview import (
    begin_interview,
    cancel_interview,
    process_interview_answer,
    split_message_bubbles,
)
from functions.pairing import (
    PairingConfigurationError,
    PairingStorageError,
    consume_pairing_code,
    finalize_pairing_code,
    is_pairing_code,
)
from functions.shared import (
    UserMappingStorageError,
    get_channel_app_user_id,
    get_chat_id_by_user,
    get_interview_status,
    get_registered_user,
    save_channel_user_mapping,
    upsert_registered_user,
    verify_registered_user_passcode,
)


bp = df.Blueprint()

_CHANNEL = "ConversationalCare"
_EXTERNAL_USER_ID_RE = re.compile(r"^cc_[A-Za-z0-9_-]{16,128}$")
_APP_USER_ID_RE = re.compile(r"^[^/\\#?\x00-\x1f\x7f]{1,128}$")
_MAX_MESSAGE_CHARS = 12000
_MAX_CONVERSATION_ID_CHARS = 200


def _response(status: str, message: str, http_status: int = 200) -> func.HttpResponse:
    return func.HttpResponse(
        json.dumps(
            {"status": str(status or "error"), "response": str(message or "")},
            ensure_ascii=False,
        ),
        status_code=http_status,
        mimetype="application/json",
    )


def _string(payload: Dict[str, Any], key: str) -> str:
    value = payload.get(key)
    return str(value).strip() if isinstance(value, str) else ""


def _valid_external_user_id(value: str) -> bool:
    return bool(_EXTERNAL_USER_ID_RE.fullmatch(str(value or "")))


def _valid_app_user_id(value: str) -> bool:
    return bool(_APP_USER_ID_RE.fullmatch(str(value or "")))


def _conversation_thread_id(conversation_id: str) -> str:
    normalized = str(conversation_id or "").strip()
    if not normalized or len(normalized) > _MAX_CONVERSATION_ID_CHARS:
        raise ValueError("Invalid conversation id.")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in normalized):
        raise ValueError("Invalid conversation id.")
    return f"conversational_care:{normalized}"


def _join_bubbles(message: str) -> str:
    return "\n\n".join(split_message_bubbles(str(message or "")))


def _refresh_availability(app_user_id: str) -> None:
    try:
        enqueue_metric_availability_refresh(app_user_id)
    except Exception as exc:
        logging.warning(
            "Conversational Care availability refresh skipped error_type=%s",
            type(exc).__name__,
        )


def _has_telegram_delivery(app_user_id: str) -> bool:
    try:
        return bool(get_chat_id_by_user(app_user_id))
    except Exception:
        return False


def _delivery_note(app_user_id: str) -> str:
    if _has_telegram_delivery(app_user_id):
        return ""
    return (
        "Your alert and daily summary settings are saved to your Sensei account. "
        "Proactive delivery is currently available through Telegram only. If you "
        "later use /login with the same AppUserId in Telegram, these settings will "
        "already be there."
    )


def _with_delivery_note(message: str, app_user_id: str) -> str:
    note = _delivery_note(app_user_id)
    return f"{str(message or '').rstrip()}\n\n{note}" if note else str(message or "")


def _login(payload: Dict[str, Any], external_user_id: str) -> func.HttpResponse:
    app_user_id = _string(payload, "app_user_id")
    passcode = _string(payload, "passcode")
    if not _valid_app_user_id(app_user_id) or not passcode:
        return _response("error", "app_user_id and passcode are required.", 400)

    verified, reason, attempt_status = verify_registered_user_passcode(
        f"{_CHANNEL}:{external_user_id}", app_user_id, passcode
    )
    if not verified:
        if reason == "locked":
            seconds = max(0, int(attempt_status.get("retry_after_seconds") or 0))
            minutes = max(1, (seconds + 59) // 60)
            return _response(
                "error",
                f"Too many incorrect passcode attempts. Try again in about {minutes} minute(s).",
                429,
            )
        if reason == "not_found":
            return _response("error", "No matching AppUserId was found.", 401)
        return _response("error", "That passcode does not match.", 401)

    if not save_channel_user_mapping(_CHANNEL, external_user_id, app_user_id):
        return _response("error", "Could not save the account connection.", 503)
    _refresh_availability(app_user_id)
    return _response("ok", f"Logged in to Sensei as '{app_user_id}'.")


def _register(payload: Dict[str, Any], external_user_id: str) -> func.HttpResponse:
    app_user_id = _string(payload, "app_user_id")
    passcode = _string(payload, "passcode")
    if not _valid_app_user_id(app_user_id) or not passcode:
        return _response("error", "app_user_id and passcode are required.", 400)

    current_mapping = get_channel_app_user_id(_CHANNEL, external_user_id)
    existing_user = get_registered_user(app_user_id)
    if existing_user:
        if current_mapping == app_user_id:
            return _response(
                "ok",
                f"This Conversational Care user is already connected to '{app_user_id}'.",
            )
        return _response(
            "error",
            "That AppUserId is already registered. Use /login instead.",
            409,
        )

    created, reason = upsert_registered_user(app_user_id, passcode)
    if not created:
        if reason == "duplicate_different_passcode":
            return _response(
                "error",
                "That AppUserId is already registered. Use /login instead.",
                409,
            )
        return _response("error", "Registration could not be completed.", 400)
    if not save_channel_user_mapping(_CHANNEL, external_user_id, app_user_id):
        return _response("error", "Could not save the account connection.", 503)

    _refresh_availability(app_user_id)
    return _response(
        "ok",
        f"Sensei account '{app_user_id}' was registered. Send /start to begin onboarding.",
    )


def _help_message() -> str:
    return (
        "Available commands:\n\n"
        "/login <AppUserId> <Passcode> — connect an existing Sensei account\n"
        "/register_user <AppUserId> <Passcode> — create a Sensei account\n"
        "/start — start onboarding when your profile is not complete\n"
        "/help — show this message\n"
        "/cancel — cancel onboarding in progress\n\n"
        "After login and onboarding, send a health-data question normally."
    )


def _unmapped_account_message() -> str:
    return (
        "Your chat account is not yet linked to a Sensei sensor account.\n\n"
        "1. Install the Custom Logger App using the installation link provided by the research team.\n"
        "2. Register a new account or sign in to your existing account in the app.\n"
        "3. Complete the initial sensor-data backfill.\n"
        "4. Select Generate Authorization PIN in the app.\n"
        "5. Send the six-digit PIN in this chat within five minutes.\n\n"
        "You can use Sensei after the connection is complete."
    )


def _pair_unmapped_user(code: str, external_user_id: str) -> func.HttpResponse:
    try:
        result = consume_pairing_code(code, _CHANNEL, external_user_id)
    except (PairingConfigurationError, PairingStorageError):
        logging.error("Conversational Care pairing is temporarily unavailable.")
        return _response(
            "error",
            "Account connection is temporarily unavailable. Please try again shortly.",
            503,
        )

    if result.status == "rate_limited":
        seconds = max(0, int(result.retry_after_seconds or 0))
        minutes = max(1, (seconds + 59) // 60)
        return _response(
            "error",
            f"Too many incorrect pairing attempts. Try again in about {minutes} minute(s).",
            429,
        )
    if not result.paired:
        return _response(
            "error",
            "That pairing code is invalid or expired. Generate a new code in the Custom Logger App and try again.",
            401,
        )

    if not save_channel_user_mapping(
        _CHANNEL,
        external_user_id,
        result.app_user_id,
        overwrite=False,
    ):
        # The claimed code remains bound to this external identity until expiry,
        # so the same message can safely retry a transient mapping failure.
        return _response(
            "error",
            "The code was verified, but the account connection could not be saved. Please try the same code again shortly.",
            503,
        )

    try:
        finalize_pairing_code(code, _CHANNEL, external_user_id)
    except (PairingConfigurationError, PairingStorageError):
        # UserMapping is already the durable authorization record. A stale
        # claimed marker remains single-identity-bound and expires safely.
        logging.warning(
            "Conversational Care pairing finalization deferred after mapping save."
        )
    _refresh_availability(result.app_user_id)
    return _response(
        "ok",
        "Your Sensei sensor account is now connected. You can now use Sensei in this chat.",
    )


async def _message(payload: Dict[str, Any], external_user_id: str) -> func.HttpResponse:
    message = _string(payload, "message")
    if not message or len(message) > _MAX_MESSAGE_CHARS:
        return _response("error", "A non-empty message is required.", 400)
    command = message.split(maxsplit=1)[0].lower()

    # Credentials are accepted only in the structured operations above.  This
    # prevents a raw passcode from ever entering LangGraph state or traces.
    if command in {"/login", "/register_user"}:
        return _response(
            "error",
            "Credential commands must be sent through the structured login or register operation.",
            400,
        )

    if command == "/help":
        return _response("ok", _help_message())

    try:
        app_user_id = get_channel_app_user_id(
            _CHANNEL, external_user_id, raise_on_storage_error=True
        )
    except UserMappingStorageError:
        return _response(
            "error",
            "Account connection status is temporarily unavailable. Please try again shortly.",
            503,
        )
    if not app_user_id:
        # Conversational Care needs no PIN-specific adapter operation: it keeps
        # forwarding the user's ordinary message and stable external_user_id.
        if is_pairing_code(message):
            return _pair_unmapped_user(message, external_user_id)
        return _response("error", _unmapped_account_message(), 401)

    if command == "/start":
        _refresh_availability(app_user_id)
        if get_interview_status(app_user_id) == "completed":
            return _response(
                "ok",
                "You're all set! Your Sensei profile is already complete. You can ask a health-data question now.",
            )
        return _response("ok", _join_bubbles(begin_interview(app_user_id)))

    interview_status = get_interview_status(app_user_id)
    if command == "/cancel":
        if interview_status == "in_progress":
            return _response("ok", cancel_interview(app_user_id))
        return _response("ok", "There is no onboarding flow in progress to cancel.")

    if interview_status == "in_progress":
        interview_reply, is_done = process_interview_answer(app_user_id, message)
        if is_done:
            _refresh_availability(app_user_id)
            interview_reply = _with_delivery_note(interview_reply, app_user_id)
        return _response("ok", _join_bubbles(interview_reply))

    if command.startswith("/"):
        return _response("error", f"Unknown command: {command}. Send /help for supported commands.", 400)

    try:
        thread_id = _conversation_thread_id(_string(payload, "conversation_id"))
    except ValueError:
        return _response("error", "A valid conversation_id is required.", 400)

    result = await handle_user_message(
        app_user_id=app_user_id,
        message_text=message,
        thread_id=thread_id,
        inbound_payload={"source": "conversational_care"},
    )
    answer = str(result.text or "")
    if str(result.route or "") in {"alert_config", "settings"}:
        answer = _with_delivery_note(answer, app_user_id)
    return _response("ok", answer)


async def handle_operation(payload: Dict[str, Any]) -> func.HttpResponse:
    operation = _string(payload, "operation").lower()
    external_user_id = _string(payload, "external_user_id")
    if operation not in {"message", "login", "register"}:
        return _response("error", "Unsupported operation.", 400)
    if not _valid_external_user_id(external_user_id):
        return _response("error", "A valid opaque external_user_id is required.", 400)

    if operation == "login":
        return _login(payload, external_user_id)
    if operation == "register":
        return _register(payload, external_user_id)
    return await _message(payload, external_user_id)


@bp.function_name(name="SenseiSendMessage")
@bp.route(
    route="send_message",
    methods=["POST"],
    auth_level=func.AuthLevel.FUNCTION,
)
async def send_message(req: func.HttpRequest) -> func.HttpResponse:
    try:
        payload = req.get_json()
    except (ValueError, TypeError):
        return _response("error", "Invalid JSON request.", 400)
    if not isinstance(payload, dict):
        return _response("error", "The JSON body must be an object.", 400)

    operation = _string(payload, "operation").lower() or "unknown"
    try:
        return await handle_operation(payload)
    except Exception as exc:
        # Do not log request bodies, messages, credentials, or exception text.
        logging.error(
            "Sensei external API operation failed operation=%s error_type=%s",
            operation,
            type(exc).__name__,
        )
        return _response("error", "Sensei could not process the request.", 500)


__all__ = ["bp", "handle_operation", "send_message"]
