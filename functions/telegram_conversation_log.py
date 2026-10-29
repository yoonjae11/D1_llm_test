# -*- coding: utf-8 -*-
"""
Best-effort Telegram conversation log staging and ADX flush helpers.

The chat path only writes small events to Azure Table Storage. A timer
trigger flushes pending rows to ADX in batches.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import logging
import os
import re
import tempfile
import uuid
from typing import Any, Iterable, List, Optional, Tuple, Union

from azure.core import MatchConditions
from azure.core.exceptions import (
    HttpResponseError,
    ResourceExistsError,
    ResourceModifiedError,
    ResourceNotFoundError,
)
from azure.data.tables import TableClient, UpdateMode
from azure.kusto.data import DataFormat, KustoConnectionStringBuilder
from azure.kusto.ingest import FileDescriptor, IngestionProperties, QueuedIngestClient
from azure_storage_resources import create_table_client

from functions.config import (
    ADX_DATABASE,
    ADX_INGEST_URI,
    ADX_TELEGRAM_CONVERSATION_MAPPING,
    ADX_TELEGRAM_CONVERSATION_TABLE,
    TABLE_CONN_STR,
    TELEGRAM_CONVERSATION_BATCH_SIZE,
    TELEGRAM_CONVERSATION_BUFFER_TABLE,
    TELEGRAM_CONVERSATION_CHECKPOINT_TABLE,
    TELEGRAM_CONVERSATION_EXPORT_LEASE_SECONDS,
    TELEGRAM_CONVERSATION_FLUSH_LOOKBACK_DAYS,
    TELEGRAM_CONVERSATION_HASH_SALT,
    TELEGRAM_CONVERSATION_LOG_ENABLE,
    TELEGRAM_CONVERSATION_MAX_ATTEMPTS,
    TELEGRAM_CONVERSATION_PAYLOAD_MAX_CHARS,
    TELEGRAM_CONVERSATION_SCHEMA_VERSION,
    TELEGRAM_CONVERSATION_STORE_TEXT,
    TELEGRAM_CONVERSATION_TEXT_MAX_CHARS,
)
from functions.shared import _build_adx_credential, canonical_json


_SHARDS = "0123456789abcdef"
_PARTITION_SEPARATOR = "_"
_REDACTED = "[REDACTED_CREDENTIAL_OR_REGISTRATION_COMMAND]"
_SENSITIVE_COMMANDS = {
    "/login",
    "/register_user",
    "/register_withings",
    "/regirster_withings",
}
_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?\d[\d\s().-]{7,}\d)(?!\d)")
_TOKEN_RE = re.compile(
    r"(?i)\b(access_token|refresh_token|client_secret|authorization|bearer|password|passcode|token)\b"
)
_OAUTH_URL_RE = re.compile(r"(?i)https?://\S*(oauth|authorize|callback|code=)\S*")
_PAIRING_CODE_RE = re.compile(r"^[0-9]{6}$")


def _utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _iso(dt: Optional[Union[datetime.datetime, str]]) -> str:
    if isinstance(dt, str):
        return dt
    if dt is None:
        dt = _utc_now()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc).isoformat()


def _parse_iso(value: Any) -> Optional[datetime.datetime]:
    if isinstance(value, datetime.datetime):
        dt = value
    else:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            dt = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc)


def _truncate(text: str, max_chars: int) -> str:
    text = str(text or "")
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 15)] + "...[truncated]"


def hash_text(text: str) -> str:
    value = f"{TELEGRAM_CONVERSATION_HASH_SALT}|{text or ''}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def hash_chat_id(chat_id: Union[str, int, None]) -> str:
    if chat_id is None:
        return ""
    return hash_text(str(chat_id))


def redact_message_text(text: str, route: str = "", command: str = "") -> Tuple[str, bool]:
    raw = str(text or "")
    cmd = str(command or "").strip().lower()
    route_text = str(route or "").strip().lower()

    if not TELEGRAM_CONVERSATION_STORE_TEXT:
        return "", True

    if cmd in _SENSITIVE_COMMANDS or route_text in {
        "login",
        "register",
        "register_withings",
        "withings_auth",
    }:
        return _REDACTED, bool(raw)

    if route_text == "pairing" and _PAIRING_CODE_RE.fullmatch(raw.strip()):
        return "[REDACTED_PAIRING_CODE]", True

    if not raw:
        return "", False

    redacted = raw
    if _OAUTH_URL_RE.search(redacted) or _TOKEN_RE.search(redacted):
        return _REDACTED, True

    redacted = _EMAIL_RE.sub("[REDACTED_EMAIL]", redacted)
    redacted = _PHONE_RE.sub("[REDACTED_PHONE]", redacted)

    was_redacted = redacted != raw
    return _truncate(redacted, TELEGRAM_CONVERSATION_TEXT_MAX_CHARS), was_redacted


def json_dumps_safe(obj: Any, max_chars: Optional[int] = None) -> str:
    try:
        text = json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    except Exception:
        text = json.dumps(str(obj), ensure_ascii=False)
    if max_chars is not None and max_chars > 0 and len(text) > max_chars:
        return text[: max(0, max_chars - 15)] + "...[truncated]"
    return text


def _payload_safe(payload: Optional[dict]) -> dict:
    if not isinstance(payload, dict):
        return {}
    payload_json = json_dumps_safe(payload)
    if len(payload_json) <= TELEGRAM_CONVERSATION_PAYLOAD_MAX_CHARS:
        try:
            return json.loads(payload_json)
        except Exception:
            return {"value": str(payload)}
    return {
        "_truncated": True,
        "preview": payload_json[: max(0, TELEGRAM_CONVERSATION_PAYLOAD_MAX_CHARS - 80)],
    }


def _event_id(parts: Iterable[Any]) -> str:
    joined = "|".join("" if part is None else str(part) for part in parts)
    return hash_text(joined)


def build_conversation_event(
    *,
    event_type: str,
    role: str,
    direction: str,
    event_time_utc: Optional[datetime.datetime],
    app_user_id: str,
    telegram_chat_id: Union[str, int, None],
    telegram_update_id: Optional[int] = None,
    telegram_message_id: Optional[int] = None,
    conversation_id: Optional[str] = None,
    turn_id: Optional[str] = None,
    parent_event_id: str = "",
    response_ordinal: int = 0,
    message_text: str = "",
    route: str = "",
    intent: str = "",
    command: str = "",
    execution_status: str = "",
    error_message: str = "",
    plan: Optional[dict] = None,
) -> dict:
    chat_hash = hash_chat_id(telegram_chat_id)
    event_time = _iso(event_time_utc)
    conversation_id = conversation_id or (f"telegram:{chat_hash}" if chat_hash else "")
    stored_text, redacted = redact_message_text(message_text, route=route, command=command)
    error_text, error_redacted = redact_message_text(error_message, route="", command="")
    identity_text = _REDACTED if redacted else stored_text
    if not turn_id:
        if telegram_update_id is not None and chat_hash:
            turn_id = f"telegram:{chat_hash}:{telegram_update_id}"
        elif chat_hash:
            seed = hash_text(
                f"{event_time}|{event_type}|{role}|{direction}|{identity_text}"
            )[:12]
            turn_id = f"telegram:{chat_hash}:event:{seed}"
        else:
            turn_id = ""

    # Never derive a persisted identifier from a credential.  Otherwise an
    # attacker with log access could use EventId as an offline passcode oracle,
    # even though MessageText itself was redacted.
    text_hash = hash_text(identity_text)
    normalized_command = str(command or "").strip().lower()
    sensitive_context = (
        normalized_command in _SENSITIVE_COMMANDS
        or str(route or "").strip().lower()
        in {"login", "register", "register_withings", "withings_auth", "pairing"}
    )
    stored_command = normalized_command
    if sensitive_context and normalized_command not in _SENSITIVE_COMMANDS:
        stored_command = ""

    # Plan is the only structured analysis context kept (for multi-turn study).
    plan_safe = _payload_safe(plan) if isinstance(plan, dict) and plan else {}

    event_id_parts = [
        TELEGRAM_CONVERSATION_SCHEMA_VERSION,
        chat_hash,
        event_type,
        role,
        direction,
        telegram_update_id,
        telegram_message_id,
        turn_id,
        parent_event_id,
        text_hash,
    ]
    # Text-free logging deliberately gives every bubble the same text hash.
    # Add only a deterministic, non-sensitive ordinal for later replies in the
    # same Telegram update. The first reply keeps the historic identifier,
    # while retrying the update produces the same sequence of identifiers.
    try:
        normalized_response_ordinal = max(0, int(response_ordinal or 0))
    except (TypeError, ValueError):
        normalized_response_ordinal = 0
    if normalized_response_ordinal:
        event_id_parts.append(f"response:{normalized_response_ordinal}")
    event_id = _event_id(event_id_parts)

    # Lean schema (study essentials only): chat time, who sent it (Role), the
    # message itself, the analysis Plan (for multi-turn), and minimal routing/
    # status. Conversation/Turn/Parent ids preserve multi-turn threading.
    return {
        "EventTimeUtc": event_time,
        "AppUserId": str(app_user_id or ""),
        "ConversationId": conversation_id,
        "TurnId": turn_id or "",
        "EventId": event_id,
        "ParentEventId": str(parent_event_id or ""),
        "EventType": str(event_type or ""),
        "Role": str(role or ""),
        "MessageText": stored_text,
        "Intent": str(intent or ""),
        "Command": stored_command,
        "Plan": plan_safe,
        "ExecutionStatus": str(execution_status or ""),
        "ErrorMessage": error_text if error_redacted else _truncate(str(error_message or ""), 1000),
        "SchemaVersion": int(TELEGRAM_CONVERSATION_SCHEMA_VERSION),
    }


def _buffer_client() -> TableClient:
    if not TABLE_CONN_STR:
        raise RuntimeError("AzureWebJobsStorage is missing.")
    return create_table_client(TABLE_CONN_STR, TELEGRAM_CONVERSATION_BUFFER_TABLE)


def _checkpoint_client() -> TableClient:
    if not TABLE_CONN_STR:
        raise RuntimeError("AzureWebJobsStorage is missing.")
    return create_table_client(TABLE_CONN_STR, TELEGRAM_CONVERSATION_CHECKPOINT_TABLE)


def _partition_key(event: dict) -> str:
    dt = _parse_iso(event.get("EventTimeUtc")) or _utc_now()
    event_id = str(event.get("EventId") or "")
    shard = event_id[:1].lower() if event_id[:1].lower() in _SHARDS else "0"
    # Azure Table Storage disallows '/', '\\', '#', '?' in PartitionKey/RowKey.
    return f"{dt.strftime('%Y%m%d')}{_PARTITION_SEPARATOR}{shard}"


def stage_conversation_event(event: dict) -> bool:
    if not TELEGRAM_CONVERSATION_LOG_ENABLE:
        logging.info("Telegram conversation staging skipped: disabled")
        return False
    if not isinstance(event, dict):
        logging.warning("Telegram conversation staging skipped: event is not a dict")
        return False

    try:
        event_id = str(event.get("EventId") or "").strip()
        if not event_id:
            logging.warning("Telegram conversation staging skipped: missing EventId")
            return False

        now_iso = _iso(_utc_now())
        partition_key = _partition_key(event)
        entity = {
            "PartitionKey": partition_key,
            "RowKey": event_id,
            "EventJson": canonical_json(event),
            "EventTimeUtc": str(event.get("EventTimeUtc") or ""),
            "AppUserId": str(event.get("AppUserId") or ""),
            "TelegramChatIdHash": str(event.get("TelegramChatIdHash") or ""),
            "ConversationId": str(event.get("ConversationId") or ""),
            "TurnId": str(event.get("TurnId") or ""),
            "EventId": event_id,
            "EventType": str(event.get("EventType") or ""),
            "Role": str(event.get("Role") or ""),
            "ExportStatus": "pending",
            "AttemptCount": 0,
            "NextAttemptAtUtc": now_iso,
            "CreatedAtUtc": now_iso,
            "UpdatedAtUtc": now_iso,
            "ExportedAtUtc": "",
            "BatchId": "",
            "LastError": "",
        }
        _buffer_client().create_entity(entity)
        logging.info(
            "Telegram conversation staged event_type=%s role=%s pk=%s event_id=%s turn_id=%s",
            event.get("EventType"),
            event.get("Role"),
            partition_key,
            event_id,
            event.get("TurnId"),
        )
        return True
    except ResourceExistsError:
        logging.info("Telegram conversation staging duplicate event_id=%s", event.get("EventId"))
        return True
    except Exception as exc:
        logging.warning(
            "Telegram conversation event staging failed event_type=%s role=%s event_id=%s error=%s",
            event.get("EventType"),
            event.get("Role"),
            event.get("EventId"),
            exc,
        )
        return False


def _iter_partition_keys(now: datetime.datetime) -> Iterable[str]:
    days = max(1, int(TELEGRAM_CONVERSATION_FLUSH_LOOKBACK_DAYS))
    for offset in range(days):
        day = now - datetime.timedelta(days=offset)
        for shard in _SHARDS:
            yield f"{day.strftime('%Y%m%d')}{_PARTITION_SEPARATOR}{shard}"


def _entity_attempt_count(entity: dict) -> int:
    try:
        return int(entity.get("AttemptCount") or 0)
    except Exception:
        return 0


def _is_due(entity: dict, now: datetime.datetime) -> bool:
    status = str(entity.get("ExportStatus") or "").lower()
    if status == "pending":
        return True
    if status == "exporting":
        claimed_at = _parse_iso(entity.get("ExportingAtUtc") or entity.get("UpdatedAtUtc"))
        if claimed_at is None:
            return True
        lease_seconds = max(1, int(TELEGRAM_CONVERSATION_EXPORT_LEASE_SECONDS))
        return claimed_at + datetime.timedelta(seconds=lease_seconds) <= now
    if status != "failed":
        return False
    if _entity_attempt_count(entity) >= int(TELEGRAM_CONVERSATION_MAX_ATTEMPTS):
        return False
    next_attempt = _parse_iso(entity.get("NextAttemptAtUtc"))
    return next_attempt is None or next_attempt <= now


def _pending_entities(limit: int) -> List[dict]:
    tc = _buffer_client()
    now = _utc_now()
    selected: List[dict] = []

    for pk in _iter_partition_keys(now):
        if len(selected) >= limit:
            break
        for status in ("pending", "failed", "exporting"):
            if len(selected) >= limit:
                break
            filter_query = f"PartitionKey eq '{pk}' and ExportStatus eq '{status}'"
            try:
                for entity in tc.query_entities(filter_query):
                    if _is_due(entity, now):
                        selected_entity = dict(entity)
                        metadata = getattr(entity, "metadata", None)
                        etag = metadata.get("etag") if isinstance(metadata, dict) else None
                        etag = etag or selected_entity.get("etag") or selected_entity.get("odata.etag")
                        if etag:
                            selected_entity["_source_etag"] = str(etag)
                        selected.append(selected_entity)
                        if len(selected) >= limit:
                            break
            except Exception as exc:
                logging.warning("Telegram conversation buffer query failed pk=%s status=%s: %s", pk, status, exc)
    return selected


def _merge_entity(tc: TableClient, entity: dict, values: dict) -> None:
    merged = {
        "PartitionKey": entity["PartitionKey"],
        "RowKey": entity["RowKey"],
        **values,
        "UpdatedAtUtc": _iso(_utc_now()),
    }
    tc.upsert_entity(mode=UpdateMode.MERGE, entity=merged)


def _claim_entity(tc: TableClient, entity: dict, batch_id: str, claimed_at: str) -> bool:
    """Claim one due row, using optimistic concurrency when an ETag is available."""

    values = {
        "ExportStatus": "exporting",
        "BatchId": batch_id,
        "ExportingAtUtc": claimed_at,
        "UpdatedAtUtc": claimed_at,
    }
    claimed = {
        "PartitionKey": entity["PartitionKey"],
        "RowKey": entity["RowKey"],
        **values,
    }
    etag = str(entity.get("_source_etag") or "").strip()
    if etag:
        try:
            tc.update_entity(
                mode=UpdateMode.MERGE,
                entity=claimed,
                etag=etag,
                match_condition=MatchConditions.IfNotModified,
            )
            return True
        except (ResourceModifiedError, ResourceNotFoundError):
            return False
        except HttpResponseError as exc:
            if getattr(exc, "status_code", None) in {404, 409, 412}:
                return False
            raise

    # Lightweight test doubles and old stored entities may not expose ETags.
    # Keep those clients compatible, then verify that this batch still owns the
    # row before allowing it into the ADX payload.
    _merge_entity(tc, entity, values)
    get_entity = getattr(tc, "get_entity", None)
    if not callable(get_entity):
        return True
    current = get_entity(partition_key=entity["PartitionKey"], row_key=entity["RowKey"])
    return (
        str(current.get("ExportStatus") or "").lower() == "exporting"
        and str(current.get("BatchId") or "") == batch_id
    )


def _classify_claimed_entities(
    entities: List[dict],
    batch_id: str,
    exported_at: str,
) -> Tuple[List[Tuple[dict, dict]], List[Tuple[dict, str]]]:
    """Return valid entity/event pairs and malformed entity/error pairs."""

    valid = []
    invalid = []
    for entity in entities:
        try:
            event_json = entity.get("EventJson")
            if event_json is None or not str(event_json).strip():
                raise ValueError("EventJson is missing")
            event = json.loads(str(event_json))
            if not isinstance(event, dict):
                raise ValueError("EventJson must decode to a JSON object")
            event["ExportedAt"] = exported_at
            event["BatchId"] = batch_id
            valid.append((entity, event))
        except Exception as exc:
            logging.warning("Invalid Telegram conversation EventJson row=%s: %s", entity.get("RowKey"), exc)
            invalid.append((entity, _truncate(f"Invalid EventJson: {exc}", 1000)))
    return valid, invalid


def _ingest_events_to_adx(events: List[dict]) -> None:
    if not events:
        return
    if not ADX_INGEST_URI or not ADX_DATABASE:
        raise RuntimeError("ADX_INGEST_URI or ADX_DATABASE is missing.")

    kcsb = KustoConnectionStringBuilder.with_azure_token_credential(ADX_INGEST_URI, _build_adx_credential())
    client = QueuedIngestClient(kcsb)

    with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".json", encoding="utf-8") as tf:
        for event in events:
            tf.write(canonical_json(event) + "\n")
        temp_path = tf.name

    try:
        size = os.path.getsize(temp_path)
        client.ingest_from_file(
            FileDescriptor(temp_path, size=size),
            ingestion_properties=IngestionProperties(
                database=ADX_DATABASE,
                table=ADX_TELEGRAM_CONVERSATION_TABLE,
                data_format=DataFormat.MULTIJSON,
                ingestion_mapping_reference=ADX_TELEGRAM_CONVERSATION_MAPPING,
            ),
        )
    finally:
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except Exception:
            pass


def _backoff_seconds(attempt_count: int) -> int:
    return min(3600, 60 * (2 ** max(0, attempt_count - 1)))


def _mark_entity_failed(tc: TableClient, entity: dict, batch_id: str, error: str) -> None:
    attempts = _entity_attempt_count(entity) + 1
    next_attempt = _iso(_utc_now() + datetime.timedelta(seconds=_backoff_seconds(attempts)))
    _merge_entity(
        tc,
        entity,
        {
            "ExportStatus": "failed",
            "AttemptCount": attempts,
            "NextAttemptAtUtc": next_attempt,
            "BatchId": batch_id,
            "LastError": _truncate(error, 1000),
        },
    )


def _update_checkpoint(values: dict) -> None:
    try:
        now_iso = _iso(_utc_now())
        entity = {
            "PartitionKey": "telegram_conversation_log",
            "RowKey": "flush_timer",
            **values,
            "UpdatedAtUtc": now_iso,
        }
        _checkpoint_client().upsert_entity(mode=UpdateMode.MERGE, entity=entity)
    except Exception as exc:
        logging.warning("Telegram conversation checkpoint update failed: %s", exc)


def flush_pending_conversation_events_to_adx(limit: Optional[int] = None) -> dict:
    if not TELEGRAM_CONVERSATION_LOG_ENABLE:
        return {"enabled": False, "selected": 0, "exported": 0, "failed": 0, "skipped": 0}

    limit = int(limit or TELEGRAM_CONVERSATION_BATCH_SIZE)
    limit = max(1, min(limit, 5000))
    batch_id = f"{_utc_now().strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    exported_at = _iso(_utc_now())
    tc = _buffer_client()
    entities = _pending_entities(limit)

    if not entities:
        _update_checkpoint({"LastAttemptUtc": exported_at, "LastError": ""})
        return {
            "enabled": True,
            "batch_id": batch_id,
            "selected": 0,
            "exported": 0,
            "failed": 0,
            "skipped": 0,
        }

    claimed_entities = []
    for entity in entities:
        try:
            if _claim_entity(tc, entity, batch_id, exported_at):
                claimed_entities.append(entity)
        except Exception as exc:
            logging.warning("Failed to mark Telegram event exporting row=%s: %s", entity.get("RowKey"), exc)

    skipped = len(entities) - len(claimed_entities)

    if not claimed_entities:
        _update_checkpoint({"LastAttemptUtc": exported_at, "LastError": ""})
        return {
            "enabled": True,
            "batch_id": batch_id,
            "selected": len(entities),
            "exported": 0,
            "failed": 0,
            "skipped": skipped,
        }

    valid_pairs, invalid_pairs = _classify_claimed_entities(
        claimed_entities,
        batch_id=batch_id,
        exported_at=exported_at,
    )
    exportable_entities = [entity for entity, _event in valid_pairs]
    events = [event for _entity, event in valid_pairs]
    invalid_failed = 0
    invalid_unmarked = 0
    for entity, parse_error in invalid_pairs:
        try:
            _mark_entity_failed(tc, entity, batch_id, parse_error)
            invalid_failed += 1
        except Exception as exc:
            invalid_unmarked += 1
            logging.warning(
                "Failed to mark invalid Telegram event failed row=%s: %s",
                entity.get("RowKey"),
                exc,
            )
    skipped += invalid_unmarked
    invalid_summary = (
        f"{len(invalid_pairs)} claimed row(s) had invalid EventJson"
        if invalid_pairs
        else ""
    )

    if not exportable_entities:
        _update_checkpoint(
            {
                "LastBatchId": batch_id,
                "LastAttemptUtc": exported_at,
                "LastError": invalid_summary,
            }
        )
        result = {
            "enabled": True,
            "batch_id": batch_id,
            "selected": len(entities),
            "exported": 0,
            "failed": invalid_failed,
            "skipped": skipped,
        }
        if invalid_summary:
            result["error"] = invalid_summary
        return result

    try:
        _ingest_events_to_adx(events)
        for entity in exportable_entities:
            try:
                _merge_entity(
                    tc,
                    entity,
                    {
                        "ExportStatus": "exported",
                        "ExportedAtUtc": exported_at,
                        "BatchId": batch_id,
                        "LastError": "",
                    },
                )
            except Exception as exc:
                logging.warning("Failed to mark Telegram event exported row=%s: %s", entity.get("RowKey"), exc)

        _update_checkpoint(
            {
                "LastSuccessfulFlushUtc": exported_at,
                "LastBatchId": batch_id,
                "LastExportedEventCount": len(events),
                "LastAttemptUtc": exported_at,
                "LastError": invalid_summary,
            }
        )
        result = {
            "enabled": True,
            "batch_id": batch_id,
            "selected": len(entities),
            "exported": len(events),
            "failed": invalid_failed,
            "skipped": skipped,
        }
        if invalid_summary:
            result["error"] = invalid_summary
        return result
    except Exception as exc:
        error = _truncate(str(exc), 1000)
        failed = invalid_failed
        for entity in exportable_entities:
            try:
                _mark_entity_failed(tc, entity, batch_id, error)
                failed += 1
            except Exception as mark_exc:
                logging.warning("Failed to mark Telegram event failed row=%s: %s", entity.get("RowKey"), mark_exc)

        _update_checkpoint(
            {
                "LastBatchId": batch_id,
                "LastAttemptUtc": exported_at,
                "LastError": error,
            }
        )
        logging.error("Telegram conversation ADX flush failed batch=%s: %s", batch_id, error)
        return {
            "enabled": True,
            "batch_id": batch_id,
            "selected": len(entities),
            "exported": 0,
            "failed": failed,
            "skipped": skipped,
            "error": error,
        }


def _count_status_sample(status: str, max_count: int = 1000) -> int:
    try:
        tc = _buffer_client()
        count = 0
        for pk in _iter_partition_keys(_utc_now()):
            filter_query = f"PartitionKey eq '{pk}' and ExportStatus eq '{status}'"
            for _ in tc.query_entities(filter_query):
                count += 1
                if count >= max_count:
                    return count
        return count
    except Exception:
        return 0


def _last_checkpoint() -> dict:
    try:
        ent = _checkpoint_client().get_entity(
            partition_key="telegram_conversation_log",
            row_key="flush_timer",
        )
        return dict(ent)
    except ResourceNotFoundError:
        return {}
    except Exception:
        return {}


def get_conversation_log_diagnostics() -> dict:
    return {
        "enabled": bool(TELEGRAM_CONVERSATION_LOG_ENABLE),
        "buffer_table": TELEGRAM_CONVERSATION_BUFFER_TABLE,
        "checkpoint_table": TELEGRAM_CONVERSATION_CHECKPOINT_TABLE,
        "adx_table": ADX_TELEGRAM_CONVERSATION_TABLE,
        "mapping": ADX_TELEGRAM_CONVERSATION_MAPPING,
        "batch_size": int(TELEGRAM_CONVERSATION_BATCH_SIZE),
        "lookback_days": int(TELEGRAM_CONVERSATION_FLUSH_LOOKBACK_DAYS),
        "max_attempts": int(TELEGRAM_CONVERSATION_MAX_ATTEMPTS),
        "export_lease_seconds": int(TELEGRAM_CONVERSATION_EXPORT_LEASE_SECONDS),
        "pending_count_sample": _count_status_sample("pending"),
        "failed_count_sample": _count_status_sample("failed"),
        "last_checkpoint": _last_checkpoint(),
    }
