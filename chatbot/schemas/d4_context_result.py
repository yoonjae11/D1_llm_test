"""Typed, source-turn-grounded output for D4 within-session resolution."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class D4Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ContextCandidate(D4Model):
    candidate_id: str
    source_turn_ids: list[str]
    source_kind: Literal["RECENT_TURN", "REPLY_TO", "ROLLING_SUMMARY", "ACTIVE_CONTEXT"]
    text: str
    recency_rank: int = Field(ge=0)


class ContextEvidenceSpan(D4Model):
    turn_id: str
    role: Literal["user", "assistant", "system"]
    text_excerpt: str
    timestamp: str | None = None


class ResolvedContextItem(D4Model):
    item_type: Literal["GOAL", "PREFERENCE", "BARRIER", "QUERY_CONDITION", "USER_CORRECTION", "OTHER"] = "OTHER"
    key: str
    value: Any
    source_turn_ids: list[str]
    resolution: Literal["EXPLICIT_REPLY", "LATEST_MENTION", "ACTIVE_CONTEXT", "SEMANTIC"]
    supersedes_turn_ids: list[str] = Field(default_factory=list)
    confidence: float | None = Field(default=None, ge=0, le=1)


class D4ContextResult(D4Model):
    status: Literal["RESOLVED", "NOT_REQUIRED", "AMBIGUOUS", "INSUFFICIENT_CONTEXT"]
    resolved_items: list[ResolvedContextItem] = Field(default_factory=list)
    source_turn_ids: list[str] = Field(default_factory=list)
    unresolved_references: list[str] = Field(default_factory=list)
    clarification_question: str = ""
    context_required: bool = False
    latest_value_applied: bool = False
    stale_context_detected: bool = False
    hallucinated_context_detected: bool = False
    evidence_spans: list[ContextEvidenceSpan] = Field(default_factory=list)
    response_constraints: list[str] = Field(default_factory=list)


__all__ = ["ContextCandidate", "ContextEvidenceSpan", "D4ContextResult", "ResolvedContextItem"]
