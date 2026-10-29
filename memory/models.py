"""Dataclasses for operational conversation memory tables."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

SCHEMA_VERSION = 1


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def ensure_utc(value: Any) -> dt.datetime:
    if isinstance(value, dt.datetime):
        parsed = value
    elif isinstance(value, str) and value:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        parsed = utc_now()
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def optional_utc(value: Any) -> Optional[dt.datetime]:
    if value in (None, ""):
        return None
    try:
        return ensure_utc(value)
    except Exception:
        return None


def new_episode_id() -> str:
    return f"ep_{uuid.uuid4().hex}"


def new_context_id() -> str:
    return f"ctx_{uuid.uuid4().hex}"


def new_turn_row_key(event_time_utc: Optional[dt.datetime] = None) -> str:
    event_time_utc = ensure_utc(event_time_utc or utc_now())
    return f"{event_time_utc.strftime('%Y%m%dT%H%M%S%fZ')}_{uuid.uuid4().hex[:8]}"


def sha256_text(text: Any) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


def json_loads(value: Any, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(str(value))
    except Exception:
        return default


def _trim(value: Any, limit: int = 32000) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


@dataclass
class ConversationEpisode:
    app_user_id: str
    episode_id: str = field(default_factory=new_episode_id)
    telegram_chat_id_hash: str = ""
    initiated_by: str = "user"
    trigger_type: str = "user_message"
    trigger_id: str = ""
    topic_label: str = ""
    topic_embedding_text: str = ""
    status: str = "active"
    priority: int = 0
    created_at_utc: dt.datetime = field(default_factory=utc_now)
    updated_at_utc: dt.datetime = field(default_factory=utc_now)
    last_turn_at_utc: dt.datetime = field(default_factory=utc_now)
    summary: str = ""
    open_question: str = ""
    last_intent: str = ""
    last_route: str = ""
    last_kql_query_hash: str = ""
    last_query_result_summary: str = ""
    last_alert_id: str = ""
    evidence_ref: str = ""
    schema_version: int = SCHEMA_VERSION

    @property
    def partition_key(self) -> str:
        return self.app_user_id

    @property
    def row_key(self) -> str:
        return self.episode_id

    def to_entity(self) -> Dict[str, Any]:
        return {
            "PartitionKey": self.partition_key,
            "RowKey": self.row_key,
            "EpisodeId": self.episode_id,
            "TelegramChatIdHash": self.telegram_chat_id_hash,
            "InitiatedBy": self.initiated_by,
            "TriggerType": self.trigger_type,
            "TriggerId": self.trigger_id,
            "TopicLabel": _trim(self.topic_label, 1024),
            "TopicEmbeddingText": _trim(self.topic_embedding_text, 4096),
            "Status": self.status,
            "Priority": int(self.priority or 0),
            "CreatedAtUtc": ensure_utc(self.created_at_utc),
            "UpdatedAtUtc": ensure_utc(self.updated_at_utc),
            "LastTurnAtUtc": ensure_utc(self.last_turn_at_utc),
            "Summary": _trim(self.summary, 16000),
            "OpenQuestion": _trim(self.open_question, 2048),
            "LastIntent": self.last_intent,
            "LastRoute": self.last_route,
            "LastKqlQueryHash": self.last_kql_query_hash,
            "LastQueryResultSummary": _trim(self.last_query_result_summary, 12000),
            "LastAlertId": self.last_alert_id,
            "EvidenceRef": _trim(self.evidence_ref, 4096),
            "SchemaVersion": int(self.schema_version or SCHEMA_VERSION),
        }

    @classmethod
    def from_entity(cls, entity: Dict[str, Any]) -> "ConversationEpisode":
        return cls(
            app_user_id=str(entity.get("PartitionKey") or ""),
            episode_id=str(entity.get("EpisodeId") or entity.get("RowKey") or ""),
            telegram_chat_id_hash=str(entity.get("TelegramChatIdHash") or ""),
            initiated_by=str(entity.get("InitiatedBy") or "user"),
            trigger_type=str(entity.get("TriggerType") or "user_message"),
            trigger_id=str(entity.get("TriggerId") or ""),
            topic_label=str(entity.get("TopicLabel") or ""),
            topic_embedding_text=str(entity.get("TopicEmbeddingText") or ""),
            status=str(entity.get("Status") or "active"),
            priority=int(entity.get("Priority") or 0),
            created_at_utc=ensure_utc(entity.get("CreatedAtUtc")),
            updated_at_utc=ensure_utc(entity.get("UpdatedAtUtc")),
            last_turn_at_utc=ensure_utc(entity.get("LastTurnAtUtc")),
            summary=str(entity.get("Summary") or ""),
            open_question=str(entity.get("OpenQuestion") or ""),
            last_intent=str(entity.get("LastIntent") or ""),
            last_route=str(entity.get("LastRoute") or ""),
            last_kql_query_hash=str(entity.get("LastKqlQueryHash") or ""),
            last_query_result_summary=str(entity.get("LastQueryResultSummary") or ""),
            last_alert_id=str(entity.get("LastAlertId") or ""),
            evidence_ref=str(entity.get("EvidenceRef") or ""),
            schema_version=int(entity.get("SchemaVersion") or SCHEMA_VERSION),
        )


@dataclass
class ConversationTurn:
    episode_id: str
    app_user_id: str
    role: str
    direction: str
    message_text: str = ""
    row_key: str = ""
    message_text_hash: str = ""
    event_time_utc: dt.datetime = field(default_factory=utc_now)
    intent: str = ""
    route: str = ""
    kql_query_hash: str = ""
    query_result_summary: str = ""
    payload_json: str = ""
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not self.row_key:
            self.row_key = new_turn_row_key(self.event_time_utc)
        if not self.message_text_hash:
            self.message_text_hash = sha256_text(self.message_text)

    @property
    def partition_key(self) -> str:
        return self.episode_id

    def to_entity(self) -> Dict[str, Any]:
        return {
            "PartitionKey": self.partition_key,
            "RowKey": self.row_key,
            "AppUserId": self.app_user_id,
            "Role": self.role,
            "Direction": self.direction,
            "MessageText": _trim(self.message_text, 16000),
            "MessageTextHash": self.message_text_hash or sha256_text(self.message_text),
            "EventTimeUtc": ensure_utc(self.event_time_utc),
            "Intent": self.intent,
            "Route": self.route,
            "KqlQueryHash": self.kql_query_hash,
            "QueryResultSummary": _trim(self.query_result_summary, 12000),
            "PayloadJson": _trim(self.payload_json, 32000),
            "SchemaVersion": int(self.schema_version or SCHEMA_VERSION),
        }

    @classmethod
    def from_entity(cls, entity: Dict[str, Any]) -> "ConversationTurn":
        return cls(
            episode_id=str(entity.get("PartitionKey") or ""),
            row_key=str(entity.get("RowKey") or ""),
            app_user_id=str(entity.get("AppUserId") or ""),
            role=str(entity.get("Role") or ""),
            direction=str(entity.get("Direction") or ""),
            message_text=str(entity.get("MessageText") or ""),
            message_text_hash=str(entity.get("MessageTextHash") or ""),
            event_time_utc=ensure_utc(entity.get("EventTimeUtc")),
            intent=str(entity.get("Intent") or ""),
            route=str(entity.get("Route") or ""),
            kql_query_hash=str(entity.get("KqlQueryHash") or ""),
            query_result_summary=str(entity.get("QueryResultSummary") or ""),
            payload_json=str(entity.get("PayloadJson") or ""),
            schema_version=int(entity.get("SchemaVersion") or SCHEMA_VERSION),
        )


@dataclass
class UserConversationState:
    app_user_id: str
    active_episode_id: str = ""
    last_user_initiated_episode_id: str = ""
    last_system_initiated_episode_id: str = ""
    last_alert_episode_id: str = ""
    updated_at_utc: dt.datetime = field(default_factory=utc_now)
    conversation_summary: str = ""
    recent_episode_ids: List[str] = field(default_factory=list)
    schema_version: int = SCHEMA_VERSION

    @property
    def partition_key(self) -> str:
        return self.app_user_id

    @property
    def row_key(self) -> str:
        return "state"

    def to_entity(self) -> Dict[str, Any]:
        return {
            "PartitionKey": self.partition_key,
            "RowKey": self.row_key,
            "ActiveEpisodeId": self.active_episode_id,
            "LastUserInitiatedEpisodeId": self.last_user_initiated_episode_id,
            "LastSystemInitiatedEpisodeId": self.last_system_initiated_episode_id,
            "LastAlertEpisodeId": self.last_alert_episode_id,
            "UpdatedAtUtc": ensure_utc(self.updated_at_utc),
            "ConversationSummary": _trim(self.conversation_summary, 16000),
            "RecentEpisodeIdsJson": json_dumps(self.recent_episode_ids[:30]),
            "SchemaVersion": int(self.schema_version or SCHEMA_VERSION),
        }

    @classmethod
    def from_entity(cls, entity: Dict[str, Any]) -> "UserConversationState":
        return cls(
            app_user_id=str(entity.get("PartitionKey") or ""),
            active_episode_id=str(entity.get("ActiveEpisodeId") or ""),
            last_user_initiated_episode_id=str(entity.get("LastUserInitiatedEpisodeId") or ""),
            last_system_initiated_episode_id=str(entity.get("LastSystemInitiatedEpisodeId") or ""),
            last_alert_episode_id=str(entity.get("LastAlertEpisodeId") or ""),
            updated_at_utc=ensure_utc(entity.get("UpdatedAtUtc")),
            conversation_summary=str(entity.get("ConversationSummary") or ""),
            recent_episode_ids=[
                str(item)
                for item in json_loads(entity.get("RecentEpisodeIdsJson"), [])
                if str(item)
            ],
            schema_version=int(entity.get("SchemaVersion") or SCHEMA_VERSION),
        )


@dataclass
class UserLongTermMemory:
    app_user_id: str
    memory_key: str
    memory_type: str
    content: str
    source_episode_id: str = ""
    confidence: float = 0.0
    created_at_utc: dt.datetime = field(default_factory=utc_now)
    updated_at_utc: dt.datetime = field(default_factory=utc_now)
    expires_at_utc: Optional[dt.datetime] = None
    schema_version: int = SCHEMA_VERSION

    def to_entity(self) -> Dict[str, Any]:
        entity = {
            "PartitionKey": self.app_user_id,
            "RowKey": self.memory_key,
            "MemoryType": self.memory_type,
            "Content": _trim(self.content, 8000),
            "SourceEpisodeId": self.source_episode_id,
            "Confidence": float(self.confidence or 0.0),
            "CreatedAtUtc": ensure_utc(self.created_at_utc),
            "UpdatedAtUtc": ensure_utc(self.updated_at_utc),
            "SchemaVersion": int(self.schema_version or SCHEMA_VERSION),
        }
        if self.expires_at_utc:
            entity["ExpiresAtUtc"] = ensure_utc(self.expires_at_utc)
        return entity

    @classmethod
    def from_entity(cls, entity: Dict[str, Any]) -> "UserLongTermMemory":
        return cls(
            app_user_id=str(entity.get("PartitionKey") or ""),
            memory_key=str(entity.get("RowKey") or ""),
            memory_type=str(entity.get("MemoryType") or ""),
            content=str(entity.get("Content") or ""),
            source_episode_id=str(entity.get("SourceEpisodeId") or ""),
            confidence=float(entity.get("Confidence") or 0.0),
            created_at_utc=ensure_utc(entity.get("CreatedAtUtc")),
            updated_at_utc=ensure_utc(entity.get("UpdatedAtUtc")),
            expires_at_utc=optional_utc(entity.get("ExpiresAtUtc")),
            schema_version=int(entity.get("SchemaVersion") or SCHEMA_VERSION),
        )


@dataclass
class SystemInitiatedContext:
    app_user_id: str
    context_id: str = field(default_factory=new_context_id)
    episode_id: str = ""
    context_type: str = "proactive_alert"
    trigger_id: str = ""
    initial_message: str = ""
    evidence_json: str = ""
    status: str = "open"
    created_at_utc: dt.datetime = field(default_factory=utc_now)
    last_interaction_at_utc: dt.datetime = field(default_factory=utc_now)
    expires_at_utc: Optional[dt.datetime] = None
    schema_version: int = SCHEMA_VERSION

    def to_entity(self) -> Dict[str, Any]:
        entity = {
            "PartitionKey": self.app_user_id,
            "RowKey": self.context_id,
            "EpisodeId": self.episode_id,
            "ContextType": self.context_type,
            "TriggerId": self.trigger_id,
            "InitialMessage": _trim(self.initial_message, 12000),
            "EvidenceJson": _trim(self.evidence_json, 32000),
            "Status": self.status,
            "CreatedAtUtc": ensure_utc(self.created_at_utc),
            "LastInteractionAtUtc": ensure_utc(self.last_interaction_at_utc),
            "SchemaVersion": int(self.schema_version or SCHEMA_VERSION),
        }
        if self.expires_at_utc:
            entity["ExpiresAtUtc"] = ensure_utc(self.expires_at_utc)
        return entity

    @classmethod
    def from_entity(cls, entity: Dict[str, Any]) -> "SystemInitiatedContext":
        return cls(
            app_user_id=str(entity.get("PartitionKey") or ""),
            context_id=str(entity.get("RowKey") or ""),
            episode_id=str(entity.get("EpisodeId") or ""),
            context_type=str(entity.get("ContextType") or "proactive_alert"),
            trigger_id=str(entity.get("TriggerId") or ""),
            initial_message=str(entity.get("InitialMessage") or ""),
            evidence_json=str(entity.get("EvidenceJson") or ""),
            status=str(entity.get("Status") or "open"),
            created_at_utc=ensure_utc(entity.get("CreatedAtUtc")),
            last_interaction_at_utc=ensure_utc(entity.get("LastInteractionAtUtc")),
            expires_at_utc=optional_utc(entity.get("ExpiresAtUtc")),
            schema_version=int(entity.get("SchemaVersion") or SCHEMA_VERSION),
        )

    def evidence(self) -> Any:
        return json_loads(self.evidence_json, {})


def dataclass_dict(value: Any) -> Dict[str, Any]:
    data = asdict(value)
    for key, item in list(data.items()):
        if isinstance(item, dt.datetime):
            data[key] = ensure_utc(item).isoformat()
    return data
