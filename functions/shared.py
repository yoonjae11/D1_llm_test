# -*- coding: utf-8 -*-
"""
Shared utility functions: HTTP session, Withings API wrapper, Table Storage clients,
Telegram messaging, token management, ADX ingestion.
"""

import logging
import json
import os
import datetime
import tempfile
import time
import hashlib
import random
from urllib.parse import urlencode
from typing import Optional, Tuple, Dict, Any

import requests
from requests.adapters import HTTPAdapter

import azure.functions as func
from azure.core import MatchConditions
from azure.core.exceptions import (
    ResourceExistsError,
    ResourceModifiedError,
    ResourceNotFoundError,
)
from azure.data.tables import TableClient, UpdateMode
from azure.identity import DefaultAzureCredential, ManagedIdentityCredential
from azure.kusto.ingest import QueuedIngestClient, IngestionProperties, FileDescriptor
from azure.kusto.data import DataFormat, KustoConnectionStringBuilder
from azure.storage.queue import QueueClient
from azure_storage_resources import create_queue_client, create_table_client
from queue_contracts import WITHINGS_WEBHOOK_QUEUE
from functions.security import (
    OAuthStateConfigurationError,
    create_oauth_state,
    passcode_matches,
)

from functions.config import (
    CLIENT_ID, CLIENT_SECRET,
    TABLE_CONN_STR, APP_TABLE_CONN_STR, APP_USERS_TABLE_NAME, TELEGRAM_REGISTER_STATE_TABLE,
    BASE_URL,
    ENV_NAME,
    WITHINGS_OAUTH_STATE_SECRET, WITHINGS_OAUTH_ALLOW_LEGACY_LOCAL_STATE,
    TELEGRAM_BOT_TOKEN, TELEGRAM_API_URL,
    TELEGRAM_PASSCODE_MAX_ATTEMPTS, TELEGRAM_PASSCODE_LOCKOUT_SECONDS,
    ADX_INGEST_URI, ADX_DATABASE, ADX_TABLE,
    DEDUP_TABLE_NAME, DEDUP_DATA_TYPES,
    WEBHOOK_LEASE_TABLE, WEBHOOK_LEASE_SECONDS, WEBHOOK_LEASE_ENABLE,
    WITHINGS_HTTP_MAX_ATTEMPTS, WITHINGS_HTTP_BASE_BACKOFF_MS, WITHINGS_HTTP_TIMEOUT_SEC,
    SYNC_STATE_TABLE_NAME,
)

# --------------------------------------------------------------------------
# Global HTTP session
# --------------------------------------------------------------------------
session = requests.Session()
_adapter = HTTPAdapter(pool_connections=200, pool_maxsize=200, max_retries=0)
session.mount("https://", _adapter)

_WEBHOOK_QUEUE_CLIENT = None


class TelegramStateStorageError(RuntimeError):
    """Raised when Telegram flow state cannot be read reliably."""


class UserMappingStorageError(RuntimeError):
    """Raised when absence of a channel mapping cannot be verified safely."""


# =============================================================================
# General helpers
# =============================================================================
def canonical_json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _safe_int(x):
    try:
        if x is None:
            return None
        return int(x)
    except Exception:
        return None


def _utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


# =============================================================================
# Withings HTTP wrapper (retry/backoff + 429 handling)
# =============================================================================
def withings_post(url: str, headers: dict, data: dict, timeout: int = None) -> dict:
    timeout = timeout or WITHINGS_HTTP_TIMEOUT_SEC

    last_err = None
    for attempt in range(1, WITHINGS_HTTP_MAX_ATTEMPTS + 1):
        try:
            resp = session.post(url, headers=headers, data=data, timeout=timeout)

            if resp.status_code == 429:
                ra = resp.headers.get("Retry-After")
                if ra:
                    sleep_s = max(1.0, float(ra))
                else:
                    sleep_s = (WITHINGS_HTTP_BASE_BACKOFF_MS / 1000.0) * (2 ** (attempt - 1)) + random.random()
                logging.warning(f"[Withings] 429 rate limit. attempt={attempt} sleep={sleep_s:.2f}s")
                time.sleep(min(sleep_s, 30.0))
                continue

            if 500 <= resp.status_code <= 599:
                sleep_s = (WITHINGS_HTTP_BASE_BACKOFF_MS / 1000.0) * (2 ** (attempt - 1)) + random.random()
                logging.warning(f"[Withings] {resp.status_code} server error. attempt={attempt} sleep={sleep_s:.2f}s")
                time.sleep(min(sleep_s, 30.0))
                continue

            resp.raise_for_status()
            return resp.json()

        except Exception as e:
            last_err = e
            sleep_s = (WITHINGS_HTTP_BASE_BACKOFF_MS / 1000.0) * (2 ** (attempt - 1)) + random.random()
            logging.warning(f"[Withings] error attempt={attempt}: {e} sleep={sleep_s:.2f}s")
            time.sleep(min(sleep_s, 30.0))

    raise RuntimeError(f"Withings request failed after {WITHINGS_HTTP_MAX_ATTEMPTS} attempts: {last_err}")


# =============================================================================
# Telegram messaging
# =============================================================================
def send_telegram_typing(chat_id):
    """Send 'typing...' indicator so the user knows the bot is processing."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_API_URL:
        return
    try:
        url = f"{TELEGRAM_API_URL}/sendChatAction"
        session.post(url, json={"chat_id": chat_id, "action": "typing"}, timeout=5)
    except Exception:
        pass  # best-effort, don't block on failure


def send_telegram_message(chat_id, text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_API_URL:
        logging.error("Telegram Token is missing.")
        return
    try:
        url = f"{TELEGRAM_API_URL}/sendMessage"
        payload = {"chat_id": chat_id, "text": text}
        session.post(url, json=payload, timeout=10)
    except Exception as e:
        logging.error(f"Telegram Send Error: {e}")


# =============================================================================
# User mapping (external channel <-> AppUserId)
# =============================================================================
_USER_MAPPING_CHANNELS = {"Telegram", "ConversationalCare"}


def get_mapping_client():
    return create_table_client(TABLE_CONN_STR, "UserMapping")


def _mapping_identity(channel: str, external_user_id) -> Tuple[str, str]:
    normalized_channel = str(channel or "").strip()
    normalized_external_id = str(external_user_id or "").strip()
    if normalized_channel not in _USER_MAPPING_CHANNELS:
        raise ValueError("Unsupported user-mapping channel.")
    if not normalized_external_id or len(normalized_external_id) > 256:
        raise ValueError("Invalid external user id.")
    if any(ch in normalized_external_id for ch in ("/", "\\", "#", "?")):
        raise ValueError("Invalid external user id.")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in normalized_external_id):
        raise ValueError("Invalid external user id.")
    return normalized_channel, normalized_external_id


def save_channel_user_mapping(
    channel: str,
    external_user_id,
    app_user_id: str,
    *,
    overwrite: bool = True,
) -> bool:
    """Save a channel mapping without exposing channel-specific identifiers elsewhere.

    ``Telegram`` and ``ConversationalCare`` share the existing ``UserMapping``
    table.  The latter RowKey is already an opaque HMAC id generated by the
    Conversational Care server; Sensei never receives or stores a phone number.
    """

    try:
        partition_key, row_key = _mapping_identity(channel, external_user_id)
        normalized_app_user_id = str(app_user_id or "").strip()
        if not normalized_app_user_id:
            raise ValueError("Invalid AppUserId.")
        tc = get_mapping_client()
        entity = {
            "PartitionKey": partition_key,
            "RowKey": row_key,
            "AppUserId": normalized_app_user_id,
        }
        if overwrite:
            tc.upsert_entity(mode=UpdateMode.REPLACE, entity=entity)
        else:
            try:
                tc.create_entity(entity=entity)
            except ResourceExistsError:
                # PIN pairing is create-only. A concurrent request may have
                # created the same mapping, which is idempotent; a different
                # existing AppUserId must never be replaced by a bare PIN.
                existing = tc.get_entity(
                    partition_key=partition_key,
                    row_key=row_key,
                )
                return (
                    str(existing.get("AppUserId") or "").strip()
                    == normalized_app_user_id
                )
        return True
    except Exception as e:
        logging.error("Mapping save failed for channel=%s: %s", channel, type(e).__name__)
        return False


def get_channel_app_user_id(
    channel: str, external_user_id, *, raise_on_storage_error: bool = False
) -> Optional[str]:
    try:
        partition_key, row_key = _mapping_identity(channel, external_user_id)
        tc = get_mapping_client()
        entity = tc.get_entity(partition_key=partition_key, row_key=row_key)
        return entity.get("AppUserId")
    except ResourceNotFoundError:
        return None
    except Exception as exc:
        if raise_on_storage_error:
            raise UserMappingStorageError(
                "User-mapping storage is unavailable."
            ) from exc
        return None


def save_user_mapping(chat_id, app_user_id, *, overwrite: bool = True):
    """Backward-compatible Telegram mapping helper."""

    return save_channel_user_mapping(
        "Telegram", chat_id, app_user_id, overwrite=overwrite
    )


def get_app_user_id(chat_id):
    """Backward-compatible Telegram lookup helper."""

    return get_channel_app_user_id("Telegram", chat_id)


def get_chat_id_by_user(app_user_id):
    try:
        tc = get_mapping_client()
        # OData string literals escape a quote by doubling it. This lookup is
        # shared by proactive delivery checks, so never interpolate the raw id.
        safe_app_user_id = str(app_user_id or "").replace("'", "''")
        filter_query = f"PartitionKey eq 'Telegram' and AppUserId eq '{safe_app_user_id}'"
        entities = tc.query_entities(filter_query)
        for entity in entities:
            return entity["RowKey"]
    except Exception:
        pass
    return None


# =============================================================================
# Withings auth URL builder
# =============================================================================
def _build_withings_auth_url(req: func.HttpRequest, app_user_id: str, chat_id: str) -> Optional[str]:
    if not CLIENT_ID:
        return None

    from functions.security import build_withings_redirect_uri

    try:
        redirect_uri = build_withings_redirect_uri(BASE_URL, req.url)
        if WITHINGS_OAUTH_STATE_SECRET:
            oauth_state = create_oauth_state(
                app_user_id, chat_id, WITHINGS_OAUTH_STATE_SECRET
            )
        elif (
            WITHINGS_OAUTH_ALLOW_LEGACY_LOCAL_STATE
            and str(ENV_NAME or "").lower() in {"local", "dev", "development", "test"}
        ):
            oauth_state = f"{app_user_id}:{chat_id}"
        else:
            raise OAuthStateConfigurationError("WITHINGS_OAUTH_STATE_SECRET is missing.")
    except (OAuthStateConfigurationError, ValueError) as exc:
        logging.error("Cannot build Withings OAuth URL safely: %s", exc)
        return None

    query = urlencode(
        {
            "response_type": "code",
            "client_id": CLIENT_ID,
            "state": oauth_state,
            "scope": "user.metrics,user.activity,user.sleepevents",
            "redirect_uri": redirect_uri,
        }
    )
    return f"https://account.withings.com/oauth2_user/authorize2?{query}"


# =============================================================================
# Registered users (AppUserId + Passcode)
# =============================================================================
def _get_users_client() -> TableClient:
    if not APP_TABLE_CONN_STR:
        raise RuntimeError("AzureWebJobsAppStorage is missing (Storage connection string).")

    return create_table_client(APP_TABLE_CONN_STR, APP_USERS_TABLE_NAME)


def get_registered_user(app_user_id: str) -> Optional[Dict[str, Any]]:
    if not app_user_id:
        return None

    tc = _get_users_client()
    try:
        return tc.get_entity(partition_key="user", row_key=str(app_user_id))
    except ResourceNotFoundError:
        return None


def upsert_registered_user(app_user_id: str, passcode: str) -> Tuple[bool, str]:
    app_user_id = str(app_user_id or "").strip()
    passcode = str(passcode or "").strip()

    if not app_user_id or not passcode:
        return False, "invalid_args"

    tc = _get_users_client()
    existing = None
    try:
        existing = tc.get_entity(partition_key="user", row_key=app_user_id)
    except ResourceNotFoundError:
        existing = None

    if existing:
        if not passcode_matches(existing, passcode):
            return False, "duplicate_different_passcode"

    created_at = str(existing.get("CreatedAt")) if existing and existing.get("CreatedAt") else _utc_now_iso()
    entity = {
        "PartitionKey": "user",
        "RowKey": app_user_id,
        "CreatedAt": created_at,
        # This remains a plaintext cross-app compatibility property until the
        # Smartphone Function App's AuthLogin endpoint supports the same hash.
        "Passcode": passcode,
    }
    tc.upsert_entity(mode=UpdateMode.MERGE, entity=entity)
    return True, "ok"


def verify_registered_user_passcode(
    auth_subject_id,
    app_user_id: str,
    supplied_passcode: str,
    *,
    user_entity: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, str, Dict[str, Any]]:
    """Verify an existing Sensei account using the shared persistent throttle.

    ``auth_subject_id`` identifies the login origin for rate limiting.  Callers
    should provide a channel-qualified id (for example a Telegram chat id or the
    opaque Conversational Care id).  The supplied credential is never persisted
    by this function.
    """

    normalized_app_user_id = str(app_user_id or "").strip()
    if user_entity is None:
        user_entity = get_registered_user(normalized_app_user_id)
    if not user_entity:
        return False, "not_found", {
            "allowed": True,
            "attempts": 0,
            "retry_after_seconds": 0,
        }

    status = get_passcode_attempt_status(auth_subject_id, normalized_app_user_id)
    if not status["allowed"]:
        return False, "locked", status
    if not passcode_matches(user_entity, str(supplied_passcode or "")):
        status = record_passcode_failure(auth_subject_id, normalized_app_user_id)
        return False, "locked" if not status["allowed"] else "invalid_passcode", status

    clear_passcode_attempts(auth_subject_id, normalized_app_user_id)
    return True, "ok", {
        "allowed": True,
        "attempts": 0,
        "retry_after_seconds": 0,
    }


def update_user_profile_fields(app_user_id: str, fields: Dict[str, Any]) -> bool:
    """MERGE additional profile fields into the Users entity."""
    if not app_user_id or not fields:
        return False
    tc = _get_users_client()
    entity = {"PartitionKey": "user", "RowKey": str(app_user_id)}
    entity.update(fields)
    try:
        tc.upsert_entity(mode=UpdateMode.MERGE, entity=entity)
        return True
    except Exception as e:
        logging.error(f"update_user_profile_fields error app_user_id={app_user_id}: {e}")
        return False


def get_interview_status(app_user_id: str) -> str:
    """Return InterviewStatus for a user: 'not_started' | 'in_progress' | 'completed'."""
    user = get_registered_user(app_user_id)
    if not user:
        return "not_started"
    return str(user.get("InterviewStatus", "not_started") or "not_started")


def complete_interview(app_user_id: str) -> bool:
    """Mark interview as completed."""
    return update_user_profile_fields(app_user_id, {
        "InterviewStatus": "completed",
        "InterviewStep": "done",
        "InterviewUpdatedAt": _utc_now_iso(),
    })


# =============================================================================
# Telegram registration state (passcode flow)
# =============================================================================
def _get_register_state_client() -> TableClient:
    if not TABLE_CONN_STR:
        raise RuntimeError("AzureWebJobsStorage is missing (Storage connection string).")

    return create_table_client(TABLE_CONN_STR, TELEGRAM_REGISTER_STATE_TABLE)


def save_register_withings_state(chat_id, app_user_id: str) -> bool:
    try:
        tc = _get_register_state_client()
        entity = {
            "PartitionKey": "Telegram",
            "RowKey": str(chat_id),
            "AppUserId": str(app_user_id),
            "Status": "awaiting_passcode",
            "UpdatedAt": _utc_now_iso(),
        }
        tc.upsert_entity(mode=UpdateMode.REPLACE, entity=entity)
        return True
    except Exception as e:
        logging.error(f"Register state save error: {e}")
        return False


def get_register_withings_state(
    chat_id, *, raise_on_storage_error: bool = False
) -> Optional[Dict[str, Any]]:
    try:
        tc = _get_register_state_client()
        return tc.get_entity(partition_key="Telegram", row_key=str(chat_id))
    except ResourceNotFoundError:
        return None
    except Exception as exc:
        if raise_on_storage_error:
            raise TelegramStateStorageError(
                "Telegram Withings registration state is unavailable."
            ) from exc
        return None


def clear_register_withings_state(chat_id) -> None:
    try:
        tc = _get_register_state_client()
        tc.delete_entity(partition_key="Telegram", row_key=str(chat_id))
    except Exception:
        pass


# =============================================================================
# Passcode attempt throttling
# =============================================================================
_PASSCODE_ATTEMPT_PARTITION = "PasscodeAttempt"
_PASSCODE_ACCOUNT_ATTEMPT_PARTITION = "PasscodeAttemptAccount"
_PASSCODE_UPDATE_MAX_ATTEMPTS = 8


def _passcode_attempt_row_key(chat_id, app_user_id: str) -> str:
    material = f"{chat_id}:{str(app_user_id or '').strip()}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _passcode_account_attempt_row_key(app_user_id: str) -> str:
    material = str(app_user_id or "").strip().encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _get_passcode_attempt_entity(
    table_client, partition_key: str, row_key: str
) -> Optional[Dict[str, Any]]:
    try:
        return table_client.get_entity(
            partition_key=partition_key,
            row_key=row_key,
        )
    except ResourceNotFoundError:
        return None


def _passcode_status_from_entity(
    entity: Optional[Dict[str, Any]], current_time: int
) -> Dict[str, Any]:
    entity = entity or {}
    attempts = max(0, int(entity.get("Attempts") or 0))
    window_started = int(entity.get("WindowStartedAt") or 0)
    locked_until = int(entity.get("LockedUntil") or 0)

    if locked_until > current_time:
        return {
            "allowed": False,
            "attempts": attempts,
            "retry_after_seconds": locked_until - current_time,
        }
    if not window_started or current_time - window_started >= TELEGRAM_PASSCODE_LOCKOUT_SECONDS:
        attempts = 0
    return {"allowed": True, "attempts": attempts, "retry_after_seconds": 0}


def _combine_passcode_statuses(*statuses: Dict[str, Any]) -> Dict[str, Any]:
    locked = [status for status in statuses if not status["allowed"]]
    if locked:
        return {
            "allowed": False,
            "attempts": max(int(status.get("attempts") or 0) for status in locked),
            "retry_after_seconds": max(
                int(status.get("retry_after_seconds") or 0) for status in locked
            ),
        }
    return {
        "allowed": True,
        "attempts": max(int(status.get("attempts") or 0) for status in statuses),
        "retry_after_seconds": 0,
    }


def get_passcode_attempt_status(
    chat_id, app_user_id: str, *, now: Optional[int] = None
) -> Dict[str, Any]:
    """Return the distributed login-attempt status for a chat/account pair.

    Storage errors intentionally propagate so authentication fails closed instead
    of silently disabling the production brute-force boundary.
    """

    current_time = int(time.time() if now is None else now)
    table_client = _get_register_state_client()
    chat_status = _passcode_status_from_entity(
        _get_passcode_attempt_entity(
            table_client,
            _PASSCODE_ATTEMPT_PARTITION,
            _passcode_attempt_row_key(chat_id, app_user_id),
        ),
        current_time,
    )
    account_status = _passcode_status_from_entity(
        _get_passcode_attempt_entity(
            table_client,
            _PASSCODE_ACCOUNT_ATTEMPT_PARTITION,
            _passcode_account_attempt_row_key(app_user_id),
        ),
        current_time,
    )
    return _combine_passcode_statuses(chat_status, account_status)


def _entity_etag(entity: Dict[str, Any]) -> str:
    metadata = getattr(entity, "metadata", None)
    if isinstance(metadata, dict) and metadata.get("etag"):
        return str(metadata["etag"])
    for key in ("etag", "odata.etag", "_etag"):
        if entity.get(key):
            return str(entity[key])
    raise RuntimeError("Passcode attempt entity is missing its storage ETag.")


def _next_passcode_failure_entity(
    partition_key: str,
    row_key: str,
    existing: Optional[Dict[str, Any]],
    current_time: int,
) -> Dict[str, Any]:
    existing = existing or {}
    window_started = int(existing.get("WindowStartedAt") or 0)
    locked_until = int(existing.get("LockedUntil") or 0)
    if (
        not window_started
        or current_time - window_started >= TELEGRAM_PASSCODE_LOCKOUT_SECONDS
        or (locked_until and locked_until <= current_time)
    ):
        window_started = current_time
        attempts = 0
    else:
        attempts = max(0, int(existing.get("Attempts") or 0))

    attempts += 1
    locked_until = (
        current_time + TELEGRAM_PASSCODE_LOCKOUT_SECONDS
        if attempts >= TELEGRAM_PASSCODE_MAX_ATTEMPTS
        else 0
    )
    return {
        "PartitionKey": partition_key,
        "RowKey": row_key,
        "Attempts": attempts,
        "WindowStartedAt": window_started,
        "LockedUntil": locked_until,
        "UpdatedAt": _utc_now_iso(),
    }


def _record_passcode_failure_atomic(
    table_client, partition_key: str, row_key: str, current_time: int
) -> Dict[str, Any]:
    for _ in range(_PASSCODE_UPDATE_MAX_ATTEMPTS):
        existing = _get_passcode_attempt_entity(table_client, partition_key, row_key)
        entity = _next_passcode_failure_entity(
            partition_key, row_key, existing, current_time
        )
        try:
            if existing is None:
                table_client.create_entity(entity=entity)
            else:
                table_client.update_entity(
                    entity=entity,
                    mode=UpdateMode.REPLACE,
                    etag=_entity_etag(existing),
                    match_condition=MatchConditions.IfNotModified,
                )
            return _passcode_status_from_entity(entity, current_time)
        except (ResourceExistsError, ResourceModifiedError, ResourceNotFoundError):
            # Another invocation changed this counter between read and write.
            # Re-read and apply this failure to the new value.
            continue
    raise RuntimeError("Passcode attempt counter update contention exceeded its retry limit.")


def record_passcode_failure(
    chat_id, app_user_id: str, *, now: Optional[int] = None
) -> Dict[str, Any]:
    """Persist a failed passcode attempt and start a lockout at the limit."""

    current_time = int(time.time() if now is None else now)
    table_client = _get_register_state_client()
    chat_status = _record_passcode_failure_atomic(
        table_client,
        _PASSCODE_ATTEMPT_PARTITION,
        _passcode_attempt_row_key(chat_id, app_user_id),
        current_time,
    )
    account_status = _record_passcode_failure_atomic(
        table_client,
        _PASSCODE_ACCOUNT_ATTEMPT_PARTITION,
        _passcode_account_attempt_row_key(app_user_id),
        current_time,
    )
    return _combine_passcode_statuses(chat_status, account_status)


def clear_passcode_attempts(chat_id, app_user_id: str) -> None:
    """Clear the failure counter after successful verification."""

    table_client = _get_register_state_client()
    targets = (
        (_PASSCODE_ATTEMPT_PARTITION, _passcode_attempt_row_key(chat_id, app_user_id)),
        (
            _PASSCODE_ACCOUNT_ATTEMPT_PARTITION,
            _passcode_account_attempt_row_key(app_user_id),
        ),
    )
    for partition_key, row_key in targets:
        try:
            table_client.delete_entity(partition_key=partition_key, row_key=row_key)
        except ResourceNotFoundError:
            continue


# =============================================================================
# Generic per-chat "pending input" state (stateful command follow-up)
# =============================================================================
# A value-needing command (/update_timezone, /set_threshold, ...) opens a flow:
# the next message is interpreted in that command's context. Stored in the same
# table as the Withings flow but under a SEPARATE partition ("PendingInput") so
# the two never collide.
_PENDING_INPUT_PARTITION = "PendingInput"


def save_pending_input_state(chat_id, kind: str, data: Optional[Dict[str, Any]] = None) -> bool:
    """Open/refresh a pending-input flow for this chat. ``kind`` selects the flow
    (e.g. "timezone", "set_threshold"); ``data`` carries optional sub-state."""
    try:
        tc = _get_register_state_client()
        entity = {
            "PartitionKey": _PENDING_INPUT_PARTITION,
            "RowKey": str(chat_id),
            "Kind": str(kind),
            "DataJson": json.dumps(data or {}, ensure_ascii=False),
            "UpdatedAt": _utc_now_iso(),
        }
        tc.upsert_entity(mode=UpdateMode.REPLACE, entity=entity)
        return True
    except Exception as e:
        logging.warning(f"Pending-input state save error chat_id={chat_id}: {e}")
        return False


def get_pending_input_state(
    chat_id, *, raise_on_storage_error: bool = False
) -> Optional[Dict[str, Any]]:
    """Return ``{"kind", "data", "updated_at"}`` for an open flow, or None."""
    try:
        tc = _get_register_state_client()
        entity = tc.get_entity(partition_key=_PENDING_INPUT_PARTITION, row_key=str(chat_id))
    except ResourceNotFoundError:
        return None
    except Exception as exc:
        if raise_on_storage_error:
            raise TelegramStateStorageError(
                "Telegram pending-input state is unavailable."
            ) from exc
        return None
    data: Dict[str, Any] = {}
    raw = entity.get("DataJson")
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                data = parsed
        except Exception:
            pass
    return {
        "kind": str(entity.get("Kind", "") or ""),
        "data": data,
        "updated_at": str(entity.get("UpdatedAt", "") or ""),
    }


def clear_pending_input_state(chat_id) -> None:
    try:
        tc = _get_register_state_client()
        tc.delete_entity(partition_key=_PENDING_INPUT_PARTITION, row_key=str(chat_id))
    except Exception:
        pass


# =============================================================================
# Dedup cache (Azure Table Storage)
# =============================================================================
def _get_dedup_client() -> TableClient:
    return create_table_client(TABLE_CONN_STR, DEDUP_TABLE_NAME)


def _sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _dedup_should_ingest(app_user_id: str, data_type: str, record_key: str, raw_data: str) -> bool:
    if not app_user_id:
        return True
    if data_type not in DEDUP_DATA_TYPES:
        return True

    tc = _get_dedup_client()
    row_key = f"{data_type}:{record_key}"
    raw_hash = _sha1(raw_data)

    try:
        ent = tc.get_entity(partition_key=app_user_id, row_key=row_key)
        prev = ent.get("RawHash")
        if prev == raw_hash:
            try:
                ent["LastSeenAt"] = int(time.time())
                tc.update_entity(mode=UpdateMode.MERGE, entity=ent)
            except Exception:
                pass
            return False

        ent["RawHash"] = raw_hash
        ent["LastSeenAt"] = int(time.time())
        tc.upsert_entity(mode=UpdateMode.REPLACE, entity=ent)
        return True

    except ResourceNotFoundError:
        entity = {
            "PartitionKey": app_user_id,
            "RowKey": row_key,
            "RawHash": raw_hash,
            "FirstSeenAt": int(time.time()),
            "LastSeenAt": int(time.time()),
        }
        tc.upsert_entity(mode=UpdateMode.REPLACE, entity=entity)
        return True

    except Exception as e:
        logging.warning(f"[DEDUP] Error (fail-open): {e}")
        return True


# =============================================================================
# Webhook per-user lease (best-effort lock)
# =============================================================================
def _get_lease_client() -> TableClient:
    return create_table_client(TABLE_CONN_STR, WEBHOOK_LEASE_TABLE)


def try_acquire_webhook_lease(app_user_id: str) -> bool:
    if not WEBHOOK_LEASE_ENABLE:
        return True
    if not app_user_id:
        return True

    tc = _get_lease_client()
    pk = "lease"
    rk = str(app_user_id)

    now = int(time.time())
    lease_until = now + WEBHOOK_LEASE_SECONDS
    new_ent = {"PartitionKey": pk, "RowKey": rk, "LeaseUntil": lease_until, "UpdatedAt": now}

    try:
        tc.create_entity(new_ent)
        return True
    except Exception:
        pass

    try:
        ent = tc.get_entity(partition_key=pk, row_key=rk)
        cur_until = int(ent.get("LeaseUntil", 0))
        if cur_until > now:
            return False

        try:
            tc.delete_entity(partition_key=pk, row_key=rk)
        except Exception:
            pass

        try:
            tc.create_entity(new_ent)
            return True
        except Exception:
            return False

    except Exception as e:
        logging.warning(f"[LEASE] Error (fail-open): {e}")
        return True


# =============================================================================
# Webhook queue client (for delayed requeue)
# =============================================================================
def _get_webhook_queue_client() -> QueueClient:
    global _WEBHOOK_QUEUE_CLIENT
    if _WEBHOOK_QUEUE_CLIENT:
        return _WEBHOOK_QUEUE_CLIENT

    if not TABLE_CONN_STR:
        raise RuntimeError("AzureWebJobsStorage is missing (Storage connection string).")

    _WEBHOOK_QUEUE_CLIENT = create_queue_client(TABLE_CONN_STR, WITHINGS_WEBHOOK_QUEUE)
    return _WEBHOOK_QUEUE_CLIENT


# =============================================================================
# Per-user sync cursor (incremental fetch windows)
# =============================================================================
def _get_sync_state_client() -> TableClient:
    return create_table_client(TABLE_CONN_STR, SYNC_STATE_TABLE_NAME)


def _get_sync_last_ts(app_user_id: str, row_key: str):
    if not app_user_id:
        return None
    tc = _get_sync_state_client()
    try:
        ent = tc.get_entity(partition_key=app_user_id, row_key=row_key)
        ts = _safe_int(ent.get("LastSyncTs"))
        return ts if ts and ts > 0 else None
    except ResourceNotFoundError:
        return None
    except Exception as e:
        logging.warning(f"[SYNC] get cursor error app_user_id={app_user_id} key={row_key}: {e}")
        return None


def _update_sync_last_ts_max(app_user_id: str, row_key: str, new_ts: int) -> None:
    if not app_user_id:
        return
    if new_ts is None:
        return
    new_ts = int(new_ts)
    if new_ts <= 0:
        return

    now = int(time.time())
    max_allowed = now + 300
    if new_ts > max_allowed:
        new_ts = max_allowed

    tc = _get_sync_state_client()
    try:
        ent = tc.get_entity(partition_key=app_user_id, row_key=row_key)
        prev = _safe_int(ent.get("LastSyncTs")) or 0
        if new_ts <= prev:
            return

        ent["LastSyncTs"] = new_ts
        ent["UpdatedAt"] = now
        tc.upsert_entity(mode=UpdateMode.REPLACE, entity=ent)
        return
    except ResourceNotFoundError:
        pass
    except Exception as e:
        logging.warning(f"[SYNC] update cursor error app_user_id={app_user_id} key={row_key}: {e}")
        return

    try:
        tc.upsert_entity(
            mode=UpdateMode.REPLACE,
            entity={"PartitionKey": app_user_id, "RowKey": row_key, "LastSyncTs": new_ts, "UpdatedAt": now},
        )
    except Exception as e:
        logging.warning(f"[SYNC] create cursor error app_user_id={app_user_id} key={row_key}: {e}")


def _compute_sync_window_ts(
    app_user_id: str,
    row_key: str,
    now_ts: int,
    default_lookback_sec: int,
    max_lookback_sec: int,
    overlap_sec: int,
) -> Tuple[int, int]:
    now_ts = int(now_ts)
    cursor_ts = _get_sync_last_ts(app_user_id, row_key)
    if cursor_ts:
        start_ts = cursor_ts - int(overlap_sec)
    else:
        start_ts = now_ts - int(default_lookback_sec)

    start_ts = max(int(start_ts), now_ts - int(max_lookback_sec))
    start_ts = max(0, start_ts)
    end_ts = now_ts
    if start_ts > end_ts:
        start_ts = end_ts
    return start_ts, end_ts


# =============================================================================
# Token refresh
# =============================================================================
def ensure_valid_token(entity, table_client):
    if int(time.time()) < int(entity["expires_at"]) - 300:
        return entity["access_token"]
    try:
        res = withings_post(
            "https://wbsapi.withings.net/v2/oauth2",
            headers={},
            data={
                "action": "requesttoken",
                "grant_type": "refresh_token",
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "refresh_token": entity["refresh_token"],
            },
        )
        if res.get("status") == 0:
            body = res["body"]
            entity.update(
                {
                    "access_token": body["access_token"],
                    "refresh_token": body["refresh_token"],
                    "expires_at": int(time.time()) + int(body["expires_in"]),
                }
            )
            table_client.update_entity(mode=UpdateMode.REPLACE, entity=entity)
            return body["access_token"]
        logging.warning(f"[Withings] Token refresh failed status={res.get('status')} userid={entity.get('RowKey')}")
    except Exception as e:
        logging.warning(f"[Withings] Token refresh error userid={entity.get('RowKey')}: {e}")
    return None


# =============================================================================
# ADX ingestion
# =============================================================================
def _running_in_azure_function() -> bool:
    return any(
        os.environ.get(key)
        for key in ("IDENTITY_ENDPOINT", "MSI_ENDPOINT", "WEBSITE_INSTANCE_ID", "WEBSITE_SITE_NAME")
    )


def _build_adx_credential():
    if _running_in_azure_function():
        logging.info("ADX ingest authentication: using Function App managed identity.")
        return ManagedIdentityCredential()

    logging.info(
        "ADX ingest authentication: using DefaultAzureCredential for local development "
        "(environment credential excluded)."
    )
    return DefaultAzureCredential(exclude_environment_credential=True)


def ingest_raw_to_adx(records):
    kcsb = KustoConnectionStringBuilder.with_azure_token_credential(ADX_INGEST_URI, _build_adx_credential())
    client = QueuedIngestClient(kcsb)

    ingest_data = []
    for r in records:
        ingest_data.append(
            {
                "IngestedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "AppUserId": r["AppUserId"],
                "DataType": r["DataType"],
                "RawData": r["RawData"],
            }
        )

    with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".json", encoding="utf-8") as tf:
        for row in ingest_data:
            tf.write(canonical_json(row) + "\n")
        temp_path = tf.name

    try:
        size = os.path.getsize(temp_path)
        client.ingest_from_file(
            FileDescriptor(temp_path, size=size),
            ingestion_properties=IngestionProperties(
                database=ADX_DATABASE,
                table=ADX_TABLE,
                data_format=DataFormat.MULTIJSON,
            ),
        )
    finally:
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except Exception:
            pass
