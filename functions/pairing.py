"""Secure, one-time chat-account pairing through a six-digit code.

The Smartphone Function App creates pairing-code and active-pointer entities in
the shared ``PairingRequests`` table.  This module validates and atomically
consumes those two entities before a transport creates its durable UserMapping.
Raw pairing codes and raw external chat identifiers are never persisted here.
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

from azure.core import MatchConditions
from azure.core.exceptions import (
    HttpResponseError,
    ResourceExistsError,
    ResourceModifiedError,
    ResourceNotFoundError,
)
from azure.data.tables import TableTransactionError, UpdateMode

from azure_storage_resources import create_table_client
from functions.config import (
    PAIRING_ATTEMPT_WINDOW_SECONDS,
    PAIRING_CODE_TTL_SECONDS,
    PAIRING_HMAC_SECRET,
    PAIRING_LOCKOUT_SECONDS,
    PAIRING_MAX_ATTEMPTS,
    PAIRING_REQUESTS_TABLE_NAME,
    PAIRING_TABLE_CONN_STR,
)


PAIRING_CODE_RE = re.compile(r"^[0-9]{6}$")
_PAIRING_REQUEST_PARTITION = "PairingRequest"
_PAIRING_ATTEMPT_PARTITION = "PairingAttempt"
_SUPPORTED_CHANNELS = {"ConversationalCare", "Telegram"}
_ENTITY_UPDATE_MAX_ATTEMPTS = 8
_MIN_SECRET_BYTES = 32


class PairingConfigurationError(RuntimeError):
    """Raised when shared pairing storage or its HMAC secret is unavailable."""


class PairingStorageError(RuntimeError):
    """Raised when pairing state cannot be read or changed safely."""


@dataclass(frozen=True)
class PairingConsumeResult:
    status: str
    app_user_id: str = ""
    retry_after_seconds: int = 0

    @property
    def paired(self) -> bool:
        return self.status == "paired" and bool(self.app_user_id)


def is_pairing_code(value: Any) -> bool:
    """Return True only for an exact, bare six-character ASCII digit code."""

    return bool(PAIRING_CODE_RE.fullmatch(str(value or "")))


def _configured_secret(secret: Optional[str] = None) -> str:
    value = PAIRING_HMAC_SECRET if secret is None else str(secret)
    if len(value.encode("utf-8")) < _MIN_SECRET_BYTES:
        raise PairingConfigurationError(
            "PAIRING_HMAC_SECRET must contain at least 32 UTF-8 bytes."
        )
    return value


def pairing_code_hash(code: str, *, secret: Optional[str] = None) -> str:
    """Return the cross-app HMAC row key without ever storing the raw PIN."""

    normalized = str(code or "")
    if not is_pairing_code(normalized):
        raise ValueError("Pairing code must contain exactly six ASCII digits.")
    return hmac.new(
        _configured_secret(secret).encode("utf-8"),
        normalized.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()


def active_pointer_row_key(app_user_id: str, *, secret: Optional[str] = None) -> str:
    """Return the row key used by the issuer to select a user's newest code."""

    normalized = str(app_user_id or "").strip()
    if not normalized:
        raise ValueError("AppUserId is required.")
    digest = hmac.new(
        _configured_secret(secret).encode("utf-8"),
        f"user:{normalized}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"active_{digest}"


def _identity_hash(channel: str, external_user_id: Any, purpose: str) -> str:
    material = f"{purpose}:{channel}:{str(external_user_id)}".encode("utf-8")
    return hmac.new(
        _configured_secret().encode("utf-8"), material, hashlib.sha256
    ).hexdigest()


def _utc_iso(epoch_seconds: int) -> str:
    return datetime.datetime.fromtimestamp(
        int(epoch_seconds), tz=datetime.timezone.utc
    ).isoformat()


def _get_pairing_client():
    _configured_secret()
    if not PAIRING_TABLE_CONN_STR:
        raise PairingConfigurationError(
            "AzureWebJobsAppStorage or AzureWebJobsStorage is missing."
        )
    return create_table_client(
        PAIRING_TABLE_CONN_STR, PAIRING_REQUESTS_TABLE_NAME
    )


def _entity_etag(entity: Dict[str, Any]) -> str:
    metadata = getattr(entity, "metadata", None)
    if isinstance(metadata, dict) and metadata.get("etag"):
        return str(metadata["etag"])
    for key in ("etag", "odata.etag", "_etag"):
        if entity.get(key):
            return str(entity[key])
    raise PairingStorageError("Pairing entity is missing its storage ETag.")


def _read_entity(table_client, partition_key: str, row_key: str):
    try:
        return table_client.get_entity(
            partition_key=partition_key, row_key=row_key
        )
    except ResourceNotFoundError:
        return None
    except Exception as exc:
        raise PairingStorageError("Pairing storage read failed.") from exc


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _parse_iso_epoch(value: Any) -> int:
    try:
        parsed = datetime.datetime.fromisoformat(
            str(value or "").replace("Z", "+00:00")
        )
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=datetime.timezone.utc)
        return int(parsed.timestamp())
    except (TypeError, ValueError, OverflowError):
        return 0


def _attempt_status(entity: Optional[Dict[str, Any]], now: int):
    entity = entity or {}
    attempts = max(0, _safe_int(entity.get("Attempts")))
    window_started = _safe_int(entity.get("WindowStartedAt"))
    locked_until = _safe_int(entity.get("LockedUntil"))
    if locked_until > now:
        return (
            PairingConsumeResult(
                "rate_limited", retry_after_seconds=locked_until - now
            ),
            attempts,
        )
    if not window_started or now - window_started >= PAIRING_ATTEMPT_WINDOW_SECONDS:
        attempts = 0
    return PairingConsumeResult("allowed", retry_after_seconds=0), attempts


def _attempt_row_key(channel: str, external_user_id: Any) -> str:
    return _identity_hash(channel, external_user_id, "attempt")


def _get_attempt_status(table_client, channel: str, external_user_id: Any, now: int):
    entity = _read_entity(
        table_client,
        _PAIRING_ATTEMPT_PARTITION,
        _attempt_row_key(channel, external_user_id),
    )
    status, _attempts = _attempt_status(entity, now)
    return status


def _next_failure_entity(
    row_key: str, existing: Optional[Dict[str, Any]], now: int
) -> Dict[str, Any]:
    existing = existing or {}
    window_started = _safe_int(existing.get("WindowStartedAt"))
    locked_until = _safe_int(existing.get("LockedUntil"))
    if (
        not window_started
        or now - window_started >= PAIRING_ATTEMPT_WINDOW_SECONDS
        or (locked_until and locked_until <= now)
    ):
        window_started = now
        attempts = 0
    else:
        attempts = max(0, _safe_int(existing.get("Attempts")))
    attempts += 1
    return {
        "PartitionKey": _PAIRING_ATTEMPT_PARTITION,
        "RowKey": row_key,
        "EntityType": "PairingAttempt",
        "Attempts": attempts,
        "WindowStartedAt": window_started,
        "LockedUntil": now + PAIRING_LOCKOUT_SECONDS
        if attempts >= PAIRING_MAX_ATTEMPTS
        else 0,
        "UpdatedAt": _utc_iso(now),
    }


def _record_failure(
    table_client, channel: str, external_user_id: Any, now: int
) -> PairingConsumeResult:
    row_key = _attempt_row_key(channel, external_user_id)
    for _ in range(_ENTITY_UPDATE_MAX_ATTEMPTS):
        existing = _read_entity(table_client, _PAIRING_ATTEMPT_PARTITION, row_key)
        entity = _next_failure_entity(row_key, existing, now)
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
            status, _attempts = _attempt_status(entity, now)
            return status
        except (ResourceExistsError, ResourceModifiedError, ResourceNotFoundError):
            continue
        except PairingStorageError:
            raise
        except Exception as exc:
            raise PairingStorageError("Pairing attempt update failed.") from exc
    raise PairingStorageError("Pairing attempt update contention exceeded its limit.")


def _clear_attempts(table_client, channel: str, external_user_id: Any) -> None:
    try:
        table_client.delete_entity(
            partition_key=_PAIRING_ATTEMPT_PARTITION,
            row_key=_attempt_row_key(channel, external_user_id),
        )
    except ResourceNotFoundError:
        pass
    except Exception:
        # Pairing has already succeeded; stale throttling state for an identity
        # that is now mapped must not turn success into a false failure.
        pass


def _invalid_result(
    table_client,
    channel: str,
    external_user_id: Any,
    now: int,
    status: str = "invalid",
) -> PairingConsumeResult:
    attempt_status = _record_failure(table_client, channel, external_user_id, now)
    if attempt_status.status == "rate_limited":
        return attempt_status
    return PairingConsumeResult(status)


def _transaction_was_contended(exc: Exception) -> bool:
    return isinstance(exc, (ResourceModifiedError, ResourceNotFoundError)) or (
        isinstance(exc, (TableTransactionError, HttpResponseError))
        and getattr(exc, "status_code", None) in {404, 409, 412}
    )


def consume_pairing_code(
    code: str,
    channel: str,
    external_user_id: Any,
    *,
    now: Optional[int] = None,
    table_client=None,
) -> PairingConsumeResult:
    """Validate and atomically consume a pairing code.

    The caller must invoke this only for an otherwise-unmapped channel identity.
    A successful result authorizes creation of that channel's UserMapping.  The
    code row is claimed and its active pointer is deleted in one Table Storage
    transaction, preventing reuse from another identity or concurrent request.
    After saving UserMapping, the caller completes the two-phase operation with
    :func:`finalize_pairing_code`.  A same-identity retry can resume an unexpired
    claim if the mapping write previously failed.
    """

    if not is_pairing_code(code):
        return PairingConsumeResult("not_code")
    normalized_channel = str(channel or "").strip()
    if normalized_channel not in _SUPPORTED_CHANNELS:
        raise ValueError("Unsupported pairing channel.")
    if not str(external_user_id or "").strip():
        raise ValueError("External user id is required.")

    current_time = int(time.time() if now is None else now)
    client = table_client or _get_pairing_client()
    # Dependency-injected clients still require the same configured HMAC secret.
    _configured_secret()

    attempt_status = _get_attempt_status(
        client, normalized_channel, external_user_id, current_time
    )
    if attempt_status.status == "rate_limited":
        return attempt_status

    code_hash = pairing_code_hash(code)
    code_entity = _read_entity(client, _PAIRING_REQUEST_PARTITION, code_hash)
    if code_entity is None:
        return _invalid_result(
            client, normalized_channel, external_user_id, current_time
        )

    app_user_id = str(code_entity.get("AppUserId") or "").strip()
    expires_at = _safe_int(code_entity.get("ExpiresAt"))
    created_at = _parse_iso_epoch(code_entity.get("CreatedAt"))
    entity_type = str(code_entity.get("EntityType") or "")
    code_status = str(code_entity.get("Status") or "").lower()
    if (
        entity_type != "PairingCode"
        or not app_user_id
        or not created_at
        or expires_at <= created_at
        or expires_at - created_at > PAIRING_CODE_TTL_SECONDS
    ):
        return _invalid_result(
            client, normalized_channel, external_user_id, current_time, "used"
        )
    if expires_at <= current_time:
        return _invalid_result(
            client, normalized_channel, external_user_id, current_time, "expired"
        )

    consumer_hash = _identity_hash(
        normalized_channel, external_user_id, "consumer"
    )
    if code_status == "claimed":
        if (
            str(code_entity.get("ClaimedByChannel") or "") == normalized_channel
            and hmac.compare_digest(
                str(code_entity.get("ClaimedByExternalUserIdHash") or ""),
                consumer_hash,
            )
        ):
            return PairingConsumeResult("paired", app_user_id=app_user_id)
        return _invalid_result(
            client, normalized_channel, external_user_id, current_time, "used"
        )
    if code_status != "active":
        return _invalid_result(
            client, normalized_channel, external_user_id, current_time, "used"
        )

    expected_pointer_key = active_pointer_row_key(app_user_id)
    if str(code_entity.get("ActivePointerRowKey") or "") != expected_pointer_key:
        return _invalid_result(
            client, normalized_channel, external_user_id, current_time
        )
    pointer_entity = _read_entity(
        client, _PAIRING_REQUEST_PARTITION, expected_pointer_key
    )
    if pointer_entity is None:
        return _invalid_result(
            client, normalized_channel, external_user_id, current_time, "used"
        )

    pointer_code_hash = str(pointer_entity.get("CodeHash") or "")
    pointer_expires_at = _safe_int(pointer_entity.get("ExpiresAt"))
    if (
        str(pointer_entity.get("EntityType") or "") != "ActivePointer"
        or str(pointer_entity.get("AppUserId") or "") != app_user_id
        or not hmac.compare_digest(pointer_code_hash, code_hash)
        or pointer_expires_at <= current_time
        or pointer_expires_at != expires_at
    ):
        return _invalid_result(
            client, normalized_channel, external_user_id, current_time
        )

    code_update = {
        "PartitionKey": _PAIRING_REQUEST_PARTITION,
        "RowKey": code_hash,
        "Status": "claimed",
        "ClaimedAt": current_time,
        "ClaimedAtUtc": _utc_iso(current_time),
        "ClaimedByChannel": normalized_channel,
        "ClaimedByExternalUserIdHash": consumer_hash,
    }
    pointer_delete = {
        "PartitionKey": _PAIRING_REQUEST_PARTITION,
        "RowKey": expected_pointer_key,
    }
    operations = [
        (
            "update",
            code_update,
            {
                "mode": UpdateMode.MERGE,
                "etag": _entity_etag(code_entity),
                "match_condition": MatchConditions.IfNotModified,
            },
        ),
        (
            "delete",
            pointer_delete,
            {
                "etag": _entity_etag(pointer_entity),
                "match_condition": MatchConditions.IfNotModified,
            },
        ),
    ]
    try:
        client.submit_transaction(operations)
    except Exception as exc:
        if _transaction_was_contended(exc):
            return _invalid_result(
                client, normalized_channel, external_user_id, current_time, "used"
            )
        raise PairingStorageError("Pairing code consumption failed.") from exc

    _clear_attempts(client, normalized_channel, external_user_id)
    return PairingConsumeResult("paired", app_user_id=app_user_id)


def finalize_pairing_code(
    code: str,
    channel: str,
    external_user_id: Any,
    *,
    now: Optional[int] = None,
    table_client=None,
) -> bool:
    """Best-effort transition of this identity's claimed code to consumed.

    UserMapping is the durable authorization record.  This final marker is an
    audit/cleanup state and is deliberately performed only after that mapping
    has been saved.  Failures are safe: the claim remains bound to one external
    identity until it expires and cannot authorize another identity.
    """

    if not is_pairing_code(code):
        return False
    normalized_channel = str(channel or "").strip()
    if normalized_channel not in _SUPPORTED_CHANNELS:
        raise ValueError("Unsupported pairing channel.")
    if not str(external_user_id or "").strip():
        raise ValueError("External user id is required.")

    current_time = int(time.time() if now is None else now)
    client = table_client or _get_pairing_client()
    _configured_secret()
    code_hash = pairing_code_hash(code)
    entity = _read_entity(client, _PAIRING_REQUEST_PARTITION, code_hash)
    if entity is None:
        return False

    expected_consumer_hash = _identity_hash(
        normalized_channel, external_user_id, "consumer"
    )
    status = str(entity.get("Status") or "").lower()
    stored_channel = str(
        entity.get("ClaimedByChannel")
        or entity.get("ConsumedByChannel")
        or ""
    )
    stored_consumer_hash = str(
        entity.get("ClaimedByExternalUserIdHash")
        or entity.get("ConsumedByExternalUserIdHash")
        or ""
    )
    if (
        str(entity.get("EntityType") or "") != "PairingCode"
        or stored_channel != normalized_channel
        or not hmac.compare_digest(stored_consumer_hash, expected_consumer_hash)
    ):
        return False
    if status == "consumed":
        return True
    if status != "claimed":
        return False

    update = {
        "PartitionKey": _PAIRING_REQUEST_PARTITION,
        "RowKey": code_hash,
        "Status": "consumed",
        "ConsumedAt": current_time,
        "ConsumedAtUtc": _utc_iso(current_time),
        "ConsumedByChannel": normalized_channel,
        "ConsumedByExternalUserIdHash": expected_consumer_hash,
    }
    try:
        client.update_entity(
            entity=update,
            mode=UpdateMode.MERGE,
            etag=_entity_etag(entity),
            match_condition=MatchConditions.IfNotModified,
        )
        return True
    except (ResourceModifiedError, ResourceNotFoundError):
        # A duplicate request may have finalized it first. Verify ownership
        # instead of treating ordinary idempotency as a storage failure.
        current = _read_entity(client, _PAIRING_REQUEST_PARTITION, code_hash)
        if current is None or str(current.get("Status") or "").lower() != "consumed":
            return False
        return (
            str(current.get("ConsumedByChannel") or "") == normalized_channel
            and hmac.compare_digest(
                str(current.get("ConsumedByExternalUserIdHash") or ""),
                expected_consumer_hash,
            )
        )
    except Exception as exc:
        raise PairingStorageError("Pairing code finalization failed.") from exc


__all__ = [
    "PairingConfigurationError",
    "PairingConsumeResult",
    "PairingStorageError",
    "active_pointer_row_key",
    "consume_pairing_code",
    "finalize_pairing_code",
    "is_pairing_code",
    "pairing_code_hash",
]
