"""Within-session candidate construction and provenance-preserving resolution."""

from __future__ import annotations

from hashlib import sha256
import re
import time

from chatbot.graph.state import latest_user_message
from chatbot.nodes.common.support import elapsed_ms
from chatbot.schemas.d4_context_result import ContextCandidate, ContextEvidenceSpan, D4ContextResult, ResolvedContextItem
from chatbot.schemas.domain_execution_result import DomainExecutionResult


D4_WORKFLOW_VERSION = "d4_within_session_context_v1"
_REFERENCE_CUES = re.compile(
    r"\b(it|that|this|those|them|there|then|same|one|yes|no|yesterday|today|earlier|previous|prior)\b|"
    r"(그거|그것|그때|아까|같은|그걸|저거|그 부분|응|네|아니)", re.I,
)
_GOAL_CUE = re.compile(r"\b(goal|target|steps?)\b|(목표|걸음)", re.I)
_STEP_VALUE = re.compile(r"(?P<value>\d[\d,]*)\s*(?:steps?|걸음)", re.I)
_AMBIGUOUS_TIME = re.compile(r"\bthen\b|(그때|그 당시)", re.I)
_PRIOR_CLAIM = re.compile(r"\b(i said|i told you|as i mentioned)\b|(내가.*말|전에.*말)", re.I)
_BARRIER_QUERY = re.compile(r"\b(barrier|difficulty|difficult|hard|stopped me)\b|(장벽|어려|힘들|방해)", re.I)
_BARRIER_VALUE = re.compile(r"\b(can't|cannot|difficult|hard|because|no time|too tired)\b|(못하|어려|힘들|때문|시간이 없|피곤)", re.I)
_PREFERENCE_QUERY = re.compile(r"\b(preference|prefer|which one did i like)\b|(선호|좋아한다고|원한다고)", re.I)
_PREFERENCE_VALUE = re.compile(r"\b(i prefer|i like|works better for me|i would rather)\b|(선호|좋아|더 낫|원해)", re.I)
_QUERY_CONDITION = re.compile(r"\b(same period|same range|previous condition|prior query)\b|(같은 기간|이전 조건|아까 기간)", re.I)


def _turn_id(turn: dict, index: int) -> str:
    explicit = str(turn.get("turn_id") or "").strip()
    if explicit:
        return explicit
    raw = f"{index}|{turn.get('role')}|{turn.get('event_time_utc')}|{turn.get('message_text')}"
    return "turn_" + sha256(raw.encode("utf-8")).hexdigest()[:16]


def prepare_context_candidates(state: dict) -> dict:
    request = state["request"]
    candidates = []
    recent = request.get("conversation_recent_turns") or []
    evidence_spans = []
    for rank, (index, turn) in enumerate(reversed(list(enumerate(recent)))):
        text = str(turn.get("message_text") or "").strip()
        if not text:
            continue
        source_id = _turn_id(turn, index)
        candidates.append(ContextCandidate(
            candidate_id=f"candidate_{source_id}", source_turn_ids=[source_id],
            source_kind="RECENT_TURN", text=text[:1200], recency_rank=rank,
        ).model_dump())
        role = str(turn.get("role") or "user").lower()
        if role not in {"user", "assistant", "system"}:
            role = "system"
        evidence_spans.append(ContextEvidenceSpan(
            turn_id=source_id, role=role, text_excerpt=text[:400],
            timestamp=str(turn.get("event_time_utc") or "") or None,
        ).model_dump())
    reply = str(request.get("reply_to_text") or "").strip()
    if reply:
        source_id = "reply_" + sha256(reply.encode("utf-8")).hexdigest()[:16]
        candidates.insert(0, ContextCandidate(
            candidate_id=f"candidate_{source_id}", source_turn_ids=[source_id],
            source_kind="REPLY_TO", text=reply[:1200], recency_rank=0,
        ).model_dump())
        role = str(request.get("reply_to_role") or "assistant").lower()
        if role not in {"user", "assistant", "system"}:
            role = "assistant"
        evidence_spans.insert(0, ContextEvidenceSpan(
            turn_id=source_id, role=role, text_excerpt=reply[:400], timestamp=None,
        ).model_dump())
    active = request.get("conversation_active_context")
    if isinstance(active, dict) and active:
        text = str(active)[:1600]
        source_id = "active_" + sha256(text.encode("utf-8")).hexdigest()[:16]
        candidates.append(ContextCandidate(
            candidate_id=f"candidate_{source_id}", source_turn_ids=[source_id],
            source_kind="ACTIVE_CONTEXT", text=text, recency_rank=len(candidates),
        ).model_dump())
    rolling = str(request.get("conversation_rolling_summary") or "").strip()
    if rolling:
        source_id = "summary_" + sha256(rolling.encode("utf-8")).hexdigest()[:16]
        candidates.append(ContextCandidate(
            candidate_id=f"candidate_{source_id}", source_turn_ids=[source_id],
            source_kind="ROLLING_SUMMARY", text=rolling[:1600], recency_rank=len(candidates),
        ).model_dump())
    return {
        "question": latest_user_message(request.get("messages") or []),
        "context_candidates": candidates[:16], "context_evidence_spans": evidence_spans[:16],
        "started_at": time.perf_counter(),
    }


def resolve_context_reference(state: dict) -> dict:
    question = state["question"]
    candidates = state.get("context_candidates") or []
    needs_context = bool(_REFERENCE_CUES.search(question))
    if not needs_context:
        return {"resolution_status": "NOT_REQUIRED", "resolved_items": [], "unresolved_references": []}
    reply = next((item for item in candidates if item["source_kind"] == "REPLY_TO"), None)
    if _PRIOR_CLAIM.search(question):
        claimed_values = {match.group("value").replace(",", "") for match in _STEP_VALUE.finditer(question)}
        supported = any(
            any(value in candidate["text"].replace(",", "") for value in claimed_values)
            for candidate in candidates if candidate["source_kind"] == "RECENT_TURN"
        ) if claimed_values else False
        if not supported:
            return {
                "resolution_status": "INSUFFICIENT_CONTEXT", "resolved_items": [],
                "unresolved_references": [question[:160]], "hallucinated_context_detected": True,
            }
    typed_patterns = (
        ("BARRIER", "barrier", _BARRIER_QUERY, _BARRIER_VALUE),
        ("PREFERENCE", "preference", _PREFERENCE_QUERY, _PREFERENCE_VALUE),
    )
    for item_type, key, query_pattern, value_pattern in typed_patterns:
        if not query_pattern.search(question):
            continue
        selected_typed = next(
            (item for item in candidates if item["source_kind"] == "RECENT_TURN" and value_pattern.search(item["text"])),
            None,
        )
        if selected_typed:
            item = ResolvedContextItem(
                item_type=item_type, key=key, value=selected_typed["text"],
                source_turn_ids=selected_typed["source_turn_ids"], resolution="LATEST_MENTION",
                confidence=0.9,
            )
            return {"resolution_status": "RESOLVED", "resolved_items": [item.model_dump()], "unresolved_references": []}
    if _QUERY_CONDITION.search(question):
        active_candidate = next((item for item in candidates if item["source_kind"] == "ACTIVE_CONTEXT"), None)
        if active_candidate:
            item = ResolvedContextItem(
                item_type="QUERY_CONDITION", key="prior_query_condition",
                value=active_candidate["text"], source_turn_ids=active_candidate["source_turn_ids"],
                resolution="ACTIVE_CONTEXT", confidence=1.0,
            )
            return {"resolution_status": "RESOLVED", "resolved_items": [item.model_dump()], "unresolved_references": []}
    # Explicit goal lookup uses the newest supported value and records older
    # values as superseded. Ordering and comparison remain deterministic.
    if _GOAL_CUE.search(question):
        mentions = []
        span_roles = {span["turn_id"]: span["role"] for span in state.get("context_evidence_spans") or []}
        for candidate in candidates:
            if candidate["source_kind"] != "RECENT_TURN":
                continue
            match = _STEP_VALUE.search(candidate["text"])
            if match and _GOAL_CUE.search(candidate["text"]):
                mentions.append((candidate, int(match.group("value").replace(",", ""))))
        if mentions:
            user_mentions = [pair for pair in mentions if span_roles.get(pair[0]["source_turn_ids"][0]) == "user"]
            mentions = user_mentions or mentions
            latest, value = mentions[0]
            item = ResolvedContextItem(
                item_type="GOAL", key="daily_steps_goal", value=value,
                source_turn_ids=latest["source_turn_ids"], resolution="LATEST_MENTION",
                supersedes_turn_ids=[source for old, _ in mentions[1:] for source in old["source_turn_ids"]],
                confidence=1.0,
            )
            return {"resolution_status": "RESOLVED", "resolved_items": [item.model_dump()], "unresolved_references": []}
    if _AMBIGUOUS_TIME.search(question) and not reply:
        return {"resolution_status": "AMBIGUOUS", "resolved_items": [], "unresolved_references": [question[:160]]}
    selected = reply or next((item for item in candidates if item["source_kind"] == "RECENT_TURN"), None)
    if not selected:
        return {
            "resolution_status": "INSUFFICIENT_CONTEXT", "resolved_items": [],
            "unresolved_references": [question[:160]],
            "hallucinated_context_detected": bool(_PRIOR_CLAIM.search(question)),
        }
    resolution = "EXPLICIT_REPLY" if selected["source_kind"] == "REPLY_TO" else "LATEST_MENTION"
    item = ResolvedContextItem(
        item_type="OTHER", key="conversation_referent", value=selected["text"],
        source_turn_ids=selected["source_turn_ids"], resolution=resolution,
        confidence=1.0 if reply else 0.75,
    )
    return {"resolution_status": "RESOLVED", "resolved_items": [item.model_dump()], "unresolved_references": []}


def validate_context_resolution(state: dict) -> dict:
    candidates = {item["candidate_id"]: item for item in state.get("context_candidates") or []}
    source_ids = {source for item in candidates.values() for source in item["source_turn_ids"]}
    valid = []
    for item in state.get("resolved_items") or []:
        if set(item.get("source_turn_ids") or []) <= source_ids and str(item.get("value") or "").strip():
            valid.append(item)
    status = state.get("resolution_status")
    if status == "RESOLVED" and not valid:
        status = "AMBIGUOUS"
    return {"resolution_status": status, "resolved_items": valid}


def produce_context_result(state: dict) -> dict:
    status = state.get("resolution_status", "INSUFFICIENT_CONTEXT")
    items = state.get("resolved_items") or []
    source_ids = list(dict.fromkeys(source for item in items for source in item["source_turn_ids"]))
    clarification = (
        "Which earlier message or health topic do you mean?"
        if status in {"AMBIGUOUS", "INSUFFICIENT_CONTEXT"} else ""
    )
    result = D4ContextResult(
        status=status, resolved_items=items, source_turn_ids=source_ids,
        unresolved_references=state.get("unresolved_references") or [],
        clarification_question=clarification,
        context_required=status != "NOT_REQUIRED",
        latest_value_applied=any(item.get("resolution") == "LATEST_MENTION" for item in items),
        stale_context_detected=False,
        hallucinated_context_detected=bool(state.get("hallucinated_context_detected")),
        evidence_spans=[
            span for span in (state.get("context_evidence_spans") or [])
            if span.get("turn_id") in source_ids or span.get("turn_id") in {
                source for item in items for source in item.get("supersedes_turn_ids", [])
            }
        ],
        response_constraints=[
            "Use only resolved items supported by listed source turn IDs.",
            "Do not infer missing prior statements or cross-session memory.",
            "Ask for clarification when a reference remains unresolved.",
        ],
    )
    wrapper_status = "success" if status in {"RESOLVED", "NOT_REQUIRED"} else "insufficient_information"
    return {"d4_result": DomainExecutionResult(
        domain="D4", status=wrapper_status,
        summary=(f"Resolved context from {len(source_ids)} source turn(s)." if status == "RESOLVED" else clarification or "No extra context was required."),
        structured_result=result.model_dump(),
        missing_information=([clarification] if clarification else []),
        confidence=1.0 if status in {"RESOLVED", "NOT_REQUIRED"} else 0.0,
        provenance={
            "workflow_version": D4_WORKFLOW_VERSION,
            "workflow": ["prepare_context_candidates", "resolve_context_reference", "validate_context_resolution", "produce_context_result"],
            "candidate_count": len(state.get("context_candidates") or []),
            "source_turn_ids": source_ids, "resolution_status": status,
            "superseded_turn_ids": [source for item in items for source in item.get("supersedes_turn_ids", [])],
            "session_id": str((state.get("request") or {}).get("thread_id") or "local"),
            "context_visibility": "conversation_recent_turns<=16; explicit reply-to; current session",
            "validation_repairs": [], "failure_type": None,
            "prompt_id": None, "prompt_version": None, "prompt_sha256": None,
            "llm_calls": 0, "latency_ms": elapsed_ms(state["started_at"]),
            "scope": "current_session_only",
        },
    ).model_dump()}


__all__ = ["prepare_context_candidates", "produce_context_result", "resolve_context_reference", "validate_context_resolution"]
