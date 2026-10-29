"""Azure Table Storage backed conversation memory store."""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Iterable, List, Optional

try:
    from azure.core.exceptions import ResourceNotFoundError
    from azure.data.tables import TableClient, UpdateMode
except Exception:  # pragma: no cover - local unit tests can use in-memory store.
    ResourceNotFoundError = Exception
    TableClient = None
    UpdateMode = None

from . import config
from azure_storage_resources import create_table_client
from .models import (
    ConversationEpisode,
    ConversationTurn,
    SystemInitiatedContext,
    UserConversationState,
    UserLongTermMemory,
    ensure_utc,
    utc_now,
)

logger = logging.getLogger(__name__)


def _escape_filter_value(value: str) -> str:
    return str(value or "").replace("'", "''")


def _sort_desc_by_updated(episodes: Iterable[ConversationEpisode]) -> List[ConversationEpisode]:
    return sorted(
        list(episodes),
        key=lambda item: ensure_utc(item.updated_at_utc),
        reverse=True,
    )


class ConversationMemoryStore:
    """Operational memory store using Azure Table Storage.

    Methods are intentionally fail-soft by default. Telegram handling should not
    fail just because memory load/save is temporarily unavailable.
    """

    def __init__(
        self,
        connection_string: Optional[str] = None,
        *,
        enabled: Optional[bool] = None,
        fail_soft: bool = True,
    ) -> None:
        self.connection_string = connection_string or os.environ.get("AzureWebJobsStorage", "")
        self.enabled = config.MULTITURN_MEMORY_ENABLE if enabled is None else bool(enabled)
        self.fail_soft = fail_soft
        self._clients: Dict[str, Any] = {}
        self.last_error: str = ""

    @property
    def available(self) -> bool:
        return bool(self.enabled and self.connection_string and TableClient is not None)

    def _client(self, table_name: str):
        if not self.available:
            raise RuntimeError("conversation memory store is disabled or storage connection is missing")
        if table_name in self._clients:
            return self._clients[table_name]
        client = create_table_client(self.connection_string, table_name)
        self._clients[table_name] = client
        return client

    def _handle_error(self, operation: str, exc: Exception) -> None:
        self.last_error = f"{operation}: {exc}"
        if self.fail_soft:
            logger.warning("conversation memory %s failed: %s", operation, exc)
            return
        raise exc

    def get_user_state(self, app_user_id: str) -> UserConversationState:
        if not app_user_id:
            return UserConversationState(app_user_id="")
        try:
            entity = self._client(config.USER_CONVERSATION_STATE_TABLE).get_entity(
                partition_key=str(app_user_id),
                row_key="state",
            )
            return UserConversationState.from_entity(entity)
        except ResourceNotFoundError:
            return UserConversationState(app_user_id=str(app_user_id))
        except Exception as exc:
            self._handle_error("get_user_state", exc)
            return UserConversationState(app_user_id=str(app_user_id))

    def upsert_user_state(self, state: UserConversationState) -> bool:
        state.updated_at_utc = utc_now()
        try:
            self._client(config.USER_CONVERSATION_STATE_TABLE).upsert_entity(
                mode=UpdateMode.REPLACE,
                entity=state.to_entity(),
            )
            return True
        except Exception as exc:
            self._handle_error("upsert_user_state", exc)
            return False

    def create_episode(
        self,
        *,
        app_user_id: str,
        telegram_chat_id_hash: str = "",
        initiated_by: str = "user",
        trigger_type: str = "user_message",
        trigger_id: str = "",
        topic_label: str = "",
        topic_embedding_text: str = "",
        priority: int = 0,
        status: str = "active",
        last_alert_id: str = "",
        evidence_ref: str = "",
    ) -> ConversationEpisode:
        now = utc_now()
        episode = ConversationEpisode(
            app_user_id=str(app_user_id),
            telegram_chat_id_hash=str(telegram_chat_id_hash or ""),
            initiated_by=str(initiated_by or "user"),
            trigger_type=str(trigger_type or "user_message"),
            trigger_id=str(trigger_id or ""),
            topic_label=str(topic_label or ""),
            topic_embedding_text=str(topic_embedding_text or topic_label or ""),
            status=str(status or "active"),
            priority=int(priority or 0),
            created_at_utc=now,
            updated_at_utc=now,
            last_turn_at_utc=now,
            last_alert_id=str(last_alert_id or ""),
            evidence_ref=str(evidence_ref or ""),
        )
        try:
            self._client(config.CONVERSATION_EPISODE_TABLE).upsert_entity(
                mode=UpdateMode.REPLACE,
                entity=episode.to_entity(),
            )
        except Exception as exc:
            self._handle_error("create_episode", exc)
        return episode

    def get_episode(self, app_user_id: str, episode_id: str) -> Optional[ConversationEpisode]:
        if not app_user_id or not episode_id:
            return None
        try:
            entity = self._client(config.CONVERSATION_EPISODE_TABLE).get_entity(
                partition_key=str(app_user_id),
                row_key=str(episode_id),
            )
            return ConversationEpisode.from_entity(entity)
        except ResourceNotFoundError:
            return None
        except Exception as exc:
            self._handle_error("get_episode", exc)
            return None

    def list_recent_episodes(self, app_user_id: str, limit: int = 10) -> List[ConversationEpisode]:
        if not app_user_id:
            return []
        try:
            escaped = _escape_filter_value(app_user_id)
            entities = self._client(config.CONVERSATION_EPISODE_TABLE).query_entities(
                query_filter=f"PartitionKey eq '{escaped}'"
            )
            episodes = [ConversationEpisode.from_entity(entity) for entity in entities]
            return _sort_desc_by_updated(episodes)[: max(0, int(limit or 10))]
        except Exception as exc:
            self._handle_error("list_recent_episodes", exc)
            return []

    def update_episode(self, episode: ConversationEpisode) -> bool:
        episode.updated_at_utc = utc_now()
        try:
            self._client(config.CONVERSATION_EPISODE_TABLE).upsert_entity(
                mode=UpdateMode.REPLACE,
                entity=episode.to_entity(),
            )
            return True
        except Exception as exc:
            self._handle_error("update_episode", exc)
            return False

    def set_episode_status(self, app_user_id: str, episode_id: str, status: str) -> bool:
        episode = self.get_episode(app_user_id, episode_id)
        if not episode:
            return False
        episode.status = str(status or episode.status)
        return self.update_episode(episode)

    def append_turn(self, episode_id: str, turn: ConversationTurn) -> bool:
        if not episode_id:
            return False
        try:
            self._client(config.CONVERSATION_TURN_TABLE).upsert_entity(
                mode=UpdateMode.REPLACE,
                entity=turn.to_entity(),
            )
            return True
        except Exception as exc:
            self._handle_error("append_turn", exc)
            return False

    def get_recent_turns(self, episode_id: str, limit: int = 8) -> List[ConversationTurn]:
        if not episode_id:
            return []
        try:
            escaped = _escape_filter_value(episode_id)
            entities = self._client(config.CONVERSATION_TURN_TABLE).query_entities(
                query_filter=f"PartitionKey eq '{escaped}'"
            )
            turns = [ConversationTurn.from_entity(entity) for entity in entities]
            turns.sort(key=lambda item: (ensure_utc(item.event_time_utc), item.row_key))
            return turns[-max(0, int(limit or 8)) :]
        except Exception as exc:
            self._handle_error("get_recent_turns", exc)
            return []

    def get_long_term_memory(self, app_user_id: str, limit: int = 10) -> List[UserLongTermMemory]:
        if not app_user_id:
            return []
        try:
            escaped = _escape_filter_value(app_user_id)
            entities = self._client(config.USER_LONG_TERM_MEMORY_TABLE).query_entities(
                query_filter=f"PartitionKey eq '{escaped}'"
            )
            memories = [UserLongTermMemory.from_entity(entity) for entity in entities]
            now = utc_now()
            memories = [
                item
                for item in memories
                if item.expires_at_utc is None or ensure_utc(item.expires_at_utc) > now
            ]
            memories.sort(key=lambda item: (item.confidence, ensure_utc(item.updated_at_utc)), reverse=True)
            return memories[: max(0, int(limit or 10))]
        except Exception as exc:
            self._handle_error("get_long_term_memory", exc)
            return []

    def upsert_long_term_memory(self, memory: UserLongTermMemory) -> bool:
        memory.updated_at_utc = utc_now()
        try:
            self._client(config.USER_LONG_TERM_MEMORY_TABLE).upsert_entity(
                mode=UpdateMode.REPLACE,
                entity=memory.to_entity(),
            )
            return True
        except Exception as exc:
            self._handle_error("upsert_long_term_memory", exc)
            return False

    def create_system_context(
        self,
        *,
        app_user_id: str,
        episode_id: str,
        context_type: str = "proactive_alert",
        trigger_id: str = "",
        initial_message: str = "",
        evidence_json: str = "",
        status: str = "open",
        expires_at_utc=None,
    ) -> SystemInitiatedContext:
        now = utc_now()
        context = SystemInitiatedContext(
            app_user_id=str(app_user_id),
            episode_id=str(episode_id),
            context_type=str(context_type or "proactive_alert"),
            trigger_id=str(trigger_id or ""),
            initial_message=str(initial_message or ""),
            evidence_json=str(evidence_json or ""),
            status=str(status or "open"),
            created_at_utc=now,
            last_interaction_at_utc=now,
            expires_at_utc=expires_at_utc,
        )
        try:
            self._client(config.SYSTEM_INITIATED_CONTEXT_TABLE).upsert_entity(
                mode=UpdateMode.REPLACE,
                entity=context.to_entity(),
            )
        except Exception as exc:
            self._handle_error("create_system_context", exc)
        return context

    def update_system_context(self, context: SystemInitiatedContext) -> bool:
        context.last_interaction_at_utc = utc_now()
        try:
            self._client(config.SYSTEM_INITIATED_CONTEXT_TABLE).upsert_entity(
                mode=UpdateMode.REPLACE,
                entity=context.to_entity(),
            )
            return True
        except Exception as exc:
            self._handle_error("update_system_context", exc)
            return False

    def get_recent_system_context(self, app_user_id: str) -> Optional[SystemInitiatedContext]:
        if not app_user_id:
            return None
        try:
            escaped = _escape_filter_value(app_user_id)
            entities = self._client(config.SYSTEM_INITIATED_CONTEXT_TABLE).query_entities(
                query_filter=f"PartitionKey eq '{escaped}'"
            )
            contexts = [SystemInitiatedContext.from_entity(entity) for entity in entities]
            now = utc_now()
            contexts = [
                item
                for item in contexts
                if item.status == "open"
                and (item.expires_at_utc is None or ensure_utc(item.expires_at_utc) > now)
            ]
            contexts.sort(key=lambda item: ensure_utc(item.last_interaction_at_utc), reverse=True)
            return contexts[0] if contexts else None
        except Exception as exc:
            self._handle_error("get_recent_system_context", exc)
            return None


class InMemoryConversationMemoryStore:
    """Small in-memory store used by unit tests and local dry runs."""

    def __init__(self) -> None:
        self.states: Dict[str, UserConversationState] = {}
        self.episodes: Dict[tuple[str, str], ConversationEpisode] = {}
        self.turns: Dict[str, List[ConversationTurn]] = {}
        self.long_term: Dict[str, List[UserLongTermMemory]] = {}
        self.system_contexts: Dict[str, List[SystemInitiatedContext]] = {}
        self.last_error = ""

    @property
    def available(self) -> bool:
        return True

    def get_user_state(self, app_user_id: str) -> UserConversationState:
        return self.states.get(str(app_user_id), UserConversationState(app_user_id=str(app_user_id)))

    def upsert_user_state(self, state: UserConversationState) -> bool:
        state.updated_at_utc = utc_now()
        self.states[state.app_user_id] = state
        return True

    def create_episode(self, **kwargs) -> ConversationEpisode:
        episode = ConversationEpisode(**kwargs)
        self.episodes[(episode.app_user_id, episode.episode_id)] = episode
        return episode

    def get_episode(self, app_user_id: str, episode_id: str) -> Optional[ConversationEpisode]:
        return self.episodes.get((str(app_user_id), str(episode_id)))

    def list_recent_episodes(self, app_user_id: str, limit: int = 10) -> List[ConversationEpisode]:
        episodes = [
            episode
            for (uid, _), episode in self.episodes.items()
            if uid == str(app_user_id)
        ]
        return _sort_desc_by_updated(episodes)[: max(0, int(limit or 10))]

    def update_episode(self, episode: ConversationEpisode) -> bool:
        episode.updated_at_utc = utc_now()
        self.episodes[(episode.app_user_id, episode.episode_id)] = episode
        return True

    def set_episode_status(self, app_user_id: str, episode_id: str, status: str) -> bool:
        episode = self.get_episode(app_user_id, episode_id)
        if not episode:
            return False
        episode.status = str(status)
        return self.update_episode(episode)

    def append_turn(self, episode_id: str, turn: ConversationTurn) -> bool:
        self.turns.setdefault(str(episode_id), []).append(turn)
        self.turns[str(episode_id)].sort(key=lambda item: (ensure_utc(item.event_time_utc), item.row_key))
        return True

    def get_recent_turns(self, episode_id: str, limit: int = 8) -> List[ConversationTurn]:
        return list(self.turns.get(str(episode_id), []))[-max(0, int(limit or 8)) :]

    def get_long_term_memory(self, app_user_id: str, limit: int = 10) -> List[UserLongTermMemory]:
        now = utc_now()
        memories = [
            item
            for item in self.long_term.get(str(app_user_id), [])
            if item.expires_at_utc is None or ensure_utc(item.expires_at_utc) > now
        ]
        memories.sort(key=lambda item: (item.confidence, ensure_utc(item.updated_at_utc)), reverse=True)
        return memories[: max(0, int(limit or 10))]

    def upsert_long_term_memory(self, memory: UserLongTermMemory) -> bool:
        items = self.long_term.setdefault(memory.app_user_id, [])
        items[:] = [item for item in items if item.memory_key != memory.memory_key]
        items.append(memory)
        return True

    def create_system_context(self, **kwargs) -> SystemInitiatedContext:
        context = SystemInitiatedContext(**kwargs)
        self.system_contexts.setdefault(context.app_user_id, []).append(context)
        return context

    def update_system_context(self, context: SystemInitiatedContext) -> bool:
        items = self.system_contexts.setdefault(context.app_user_id, [])
        items[:] = [item for item in items if item.context_id != context.context_id]
        items.append(context)
        return True

    def get_recent_system_context(self, app_user_id: str) -> Optional[SystemInitiatedContext]:
        now = utc_now()
        items = [
            item
            for item in self.system_contexts.get(str(app_user_id), [])
            if item.status == "open"
            and (item.expires_at_utc is None or ensure_utc(item.expires_at_utc) > now)
        ]
        items.sort(key=lambda item: ensure_utc(item.last_interaction_at_utc), reverse=True)
        return items[0] if items else None
