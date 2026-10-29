"""Resolve each incoming message to an episode-specific conversation context."""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from . import config
from .models import (
    ConversationEpisode,
    SystemInitiatedContext,
    UserConversationState,
    dataclass_dict,
    ensure_utc,
    utc_now,
)


@dataclass
class ResolvedContext:
    app_user_id: str
    chat_id: str
    episode_id: str
    thread_id: str
    context_mode: str
    confidence: float
    active_episode_summary: Optional[str] = None
    related_episode_summaries: List[Dict[str, Any]] = field(default_factory=list)
    system_context: Optional[Dict[str, Any]] = None
    recent_turns: List[Any] = field(default_factory=list)
    long_term_memory: List[Any] = field(default_factory=list)
    selected_episode: Optional[ConversationEpisode] = None
    memory_load_success: bool = True


_FOLLOWUP_PHRASES = [
    "why",
    "why was",
    "why did",
    "what about",
    "how about",
    "compare",
    "compared",
    "same",
    "again",
    "previous",
    "earlier",
    "that",
    "this",
    "it",
    "them",
    "those",
    "tell me more",
    "show me",
    "go ahead",
    "yes",
    "sure",
    "okay",
    "ok",
]

_RESUME_PHRASES = [
    "back to",
    "return to",
    "go back",
    "resume",
    "previous question",
    "earlier question",
    "what we discussed",
]

_NEW_TOPIC_HINTS = [
    "new question",
    "different question",
    "unrelated",
]

_STOPWORDS = {
    "the",
    "and",
    "for",
    "with",
    "about",
    "this",
    "that",
    "what",
    "when",
    "where",
    "why",
    "how",
    "can",
    "you",
    "please",
    "today",
    "yesterday",
    "last",
    "week",
    "month",
}

_TOPIC_ALIASES = {
    "sleep": {"sleep", "slept", "wake", "woke", "bed"},
    "activity": {"steps", "step", "walk", "activity", "distance", "calorie"},
    "heart": {"heart", "hrv", "pulse", "bpm"},
    "alert": {"alert", "notification", "reminder"},
    "condition": {"condition", "status", "tired", "fatigue"},
}


def _lower(text: str) -> str:
    return str(text or "").strip().lower()


def _tokens(text: str) -> set[str]:
    raw = _lower(text)
    words = set(re.findall(r"[a-z0-9_]+", raw))
    result = {word for word in words if word not in _STOPWORDS and len(word) > 1}
    for topic, aliases in _TOPIC_ALIASES.items():
        if any(alias in raw for alias in aliases):
            result.add(topic)
    return result


def _looks_like_followup(message_text: str) -> bool:
    q = _lower(message_text)
    if not q:
        return False
    return any(_contains_phrase(q, phrase) for phrase in _FOLLOWUP_PHRASES)


def _looks_like_resume(message_text: str) -> bool:
    q = _lower(message_text)
    return any(_contains_phrase(q, phrase) for phrase in _RESUME_PHRASES)


def _explicit_new_topic(message_text: str) -> bool:
    q = _lower(message_text)
    return any(_contains_phrase(q, phrase) for phrase in _NEW_TOPIC_HINTS)


def _contains_phrase(text: str, phrase: str) -> bool:
    phrase = str(phrase or "").strip().lower()
    if not phrase:
        return False
    if re.fullmatch(r"[a-z0-9 ]+", phrase):
        pattern = r"(?<![a-z0-9])" + re.escape(phrase) + r"(?![a-z0-9])"
        return re.search(pattern, text) is not None
    return phrase in text


def _topic_score(message_text: str, episode: ConversationEpisode) -> float:
    query_tokens = _tokens(message_text)
    episode_text = " ".join(
        [
            episode.topic_label,
            episode.topic_embedding_text,
            episode.summary,
            episode.open_question,
        ]
    )
    episode_tokens = _tokens(episode_text)
    if not query_tokens or not episode_tokens:
        return 0.0
    overlap = len(query_tokens & episode_tokens)
    return overlap / max(1, min(len(query_tokens), len(episode_tokens)))


def _derive_topic_label(message_text: str) -> str:
    q = _lower(message_text)
    for label, aliases in _TOPIC_ALIASES.items():
        if any(alias in q for alias in aliases):
            return label
    words = [word for word in re.findall(r"[a-z0-9_]+", q) if word not in _STOPWORDS]
    return " ".join(words[:5]) or "general conversation"


def _episode_summary_dict(episode: ConversationEpisode) -> Dict[str, Any]:
    return {
        "episode_id": episode.episode_id,
        "initiated_by": episode.initiated_by,
        "trigger_type": episode.trigger_type,
        "trigger_id": episode.trigger_id,
        "topic_label": episode.topic_label,
        "status": episode.status,
        "summary": episode.summary,
        "last_turn_at_utc": ensure_utc(episode.last_turn_at_utc).isoformat(),
    }


class ContextResolver:
    """Rule-based resolver for episode-aware multi-turn routing."""

    def __init__(
        self,
        store,
        *,
        max_recent_turns: int = config.MULTITURN_MAX_RECENT_TURNS,
        max_related_episodes: int = config.MULTITURN_MAX_RELATED_EPISODES,
        system_reply_window_hours: int = config.MULTITURN_SYSTEM_REPLY_WINDOW_HOURS,
    ) -> None:
        self.store = store
        self.max_recent_turns = max_recent_turns
        self.max_related_episodes = max_related_episodes
        self.system_reply_window = dt.timedelta(hours=system_reply_window_hours)

    def resolve_for_user_message(
        self,
        *,
        app_user_id: str,
        chat_id: str,
        chat_id_hash: str,
        message_text: str,
        now_utc: Optional[dt.datetime] = None,
    ) -> ResolvedContext:
        now = ensure_utc(now_utc or utc_now())
        memory_load_success = True
        try:
            state = self.store.get_user_state(app_user_id)
            recent_episodes = self.store.list_recent_episodes(app_user_id, limit=12)
            system_context = self.store.get_recent_system_context(app_user_id)
        except Exception:
            memory_load_success = False
            state = UserConversationState(app_user_id=app_user_id)
            recent_episodes = []
            system_context = None

        episode_by_id = {episode.episode_id: episode for episode in recent_episodes}
        active_episode = episode_by_id.get(state.active_episode_id)
        if active_episode and active_episode.status not in {"active", "paused"}:
            active_episode = None

        selected_episode: Optional[ConversationEpisode] = None
        mode = "create_new_episode"
        confidence = 0.5

        selected_system_context = self._eligible_system_context(system_context, now)
        if selected_system_context and not _explicit_new_topic(message_text):
            candidate = episode_by_id.get(selected_system_context.episode_id) or self.store.get_episode(
                app_user_id, selected_system_context.episode_id
            )
            if candidate:
                selected_episode = candidate
                mode = "continue_system_initiated_episode"
                confidence = 0.92

        if selected_episode is None and _looks_like_resume(message_text):
            candidate = self._best_paused_match(message_text, recent_episodes)
            if candidate:
                selected_episode = candidate
                mode = "resume_paused_episode"
                confidence = max(0.72, _topic_score(message_text, candidate))

        if selected_episode is None and active_episode and _looks_like_followup(message_text):
            selected_episode = active_episode
            mode = "continue_active_episode"
            confidence = 0.78

        if selected_episode is None:
            candidate = self._best_paused_match(message_text, recent_episodes, min_score=0.55)
            if candidate:
                selected_episode = candidate
                mode = "resume_paused_episode"
                confidence = max(0.65, _topic_score(message_text, candidate))

        if selected_episode is None:
            if active_episode and self._same_topic(message_text, active_episode):
                selected_episode = active_episode
                mode = "continue_active_episode"
                confidence = max(0.67, _topic_score(message_text, active_episode))
            else:
                selected_episode = self._create_user_episode(
                    app_user_id=app_user_id,
                    chat_id_hash=chat_id_hash,
                    message_text=message_text,
                )
                mode = "create_new_episode"
                confidence = 0.68

        self._activate_episode(
            state=state,
            selected=selected_episode,
            active=active_episode,
            recent_episodes=recent_episodes,
            system_context=selected_system_context if mode == "continue_system_initiated_episode" else None,
        )

        recent_turns = self.store.get_recent_turns(selected_episode.episode_id, self.max_recent_turns)
        long_term = self.store.get_long_term_memory(app_user_id, limit=10)
        related = self._related_summaries(selected_episode, recent_episodes)

        context_dict = None
        if selected_system_context:
            context_dict = dataclass_dict(selected_system_context)
            context_dict["evidence"] = selected_system_context.evidence()

        return ResolvedContext(
            app_user_id=app_user_id,
            chat_id=str(chat_id),
            episode_id=selected_episode.episode_id,
            thread_id=f"telegram:{chat_id}:episode:{selected_episode.episode_id}",
            context_mode=mode,
            confidence=round(float(confidence), 3),
            active_episode_summary=selected_episode.summary or selected_episode.topic_label or None,
            related_episode_summaries=related,
            system_context=context_dict,
            recent_turns=recent_turns,
            long_term_memory=long_term,
            selected_episode=selected_episode,
            memory_load_success=memory_load_success,
        )

    def _eligible_system_context(
        self,
        system_context: Optional[SystemInitiatedContext],
        now: dt.datetime,
    ) -> Optional[SystemInitiatedContext]:
        if not system_context or system_context.status != "open":
            return None
        last_interaction = ensure_utc(system_context.last_interaction_at_utc)
        if now - last_interaction > self.system_reply_window:
            return None
        if system_context.expires_at_utc and ensure_utc(system_context.expires_at_utc) <= now:
            return None
        return system_context

    def _best_paused_match(
        self,
        message_text: str,
        recent_episodes: List[ConversationEpisode],
        *,
        min_score: float = 0.2,
    ) -> Optional[ConversationEpisode]:
        candidates = [
            episode
            for episode in recent_episodes
            if episode.status in {"paused", "active"} and episode.initiated_by in {"user", "system"}
        ]
        scored = [(episode, _topic_score(message_text, episode)) for episode in candidates]
        scored.sort(key=lambda item: (item[1], ensure_utc(item[0].last_turn_at_utc)), reverse=True)
        if scored and scored[0][1] >= min_score:
            return scored[0][0]
        return None

    def _same_topic(self, message_text: str, episode: ConversationEpisode) -> bool:
        return _topic_score(message_text, episode) >= 0.5

    def _create_user_episode(
        self,
        *,
        app_user_id: str,
        chat_id_hash: str,
        message_text: str,
    ) -> ConversationEpisode:
        topic = _derive_topic_label(message_text)
        return self.store.create_episode(
            app_user_id=app_user_id,
            telegram_chat_id_hash=chat_id_hash,
            initiated_by="user",
            trigger_type="user_message",
            topic_label=topic,
            topic_embedding_text=message_text[:2000],
            status="active",
            priority=0,
        )

    def _activate_episode(
        self,
        *,
        state: UserConversationState,
        selected: ConversationEpisode,
        active: Optional[ConversationEpisode],
        recent_episodes: List[ConversationEpisode],
        system_context: Optional[SystemInitiatedContext],
    ) -> None:
        now = utc_now()
        if active and active.episode_id != selected.episode_id and active.status == "active":
            active.status = "paused"
            active.updated_at_utc = now
            self.store.update_episode(active)

        selected.status = "active"
        selected.updated_at_utc = now
        selected.last_turn_at_utc = now
        self.store.update_episode(selected)

        state.active_episode_id = selected.episode_id
        if selected.initiated_by == "system":
            state.last_system_initiated_episode_id = selected.episode_id
        else:
            state.last_user_initiated_episode_id = selected.episode_id
        if selected.trigger_type == "proactive_alert" or selected.last_alert_id:
            state.last_alert_episode_id = selected.episode_id
        recent_ids = [selected.episode_id]
        for episode in recent_episodes:
            if episode.episode_id not in recent_ids:
                recent_ids.append(episode.episode_id)
        state.recent_episode_ids = recent_ids[:30]
        self.store.upsert_user_state(state)

        if system_context:
            system_context.last_interaction_at_utc = now
            self.store.update_system_context(system_context)

    def _related_summaries(
        self,
        selected: ConversationEpisode,
        recent_episodes: List[ConversationEpisode],
    ) -> List[Dict[str, Any]]:
        related: List[Dict[str, Any]] = []
        for episode in recent_episodes:
            if episode.episode_id == selected.episode_id:
                continue
            if not episode.summary and not episode.topic_label:
                continue
            related.append(_episode_summary_dict(episode))
            if len(related) >= self.max_related_episodes:
                break
        return related
