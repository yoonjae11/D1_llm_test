"""Typed, evidence-grounded output for D3 Health Knowledge & Safety."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class D3Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RetrievedEvidence(D3Model):
    evidence_id: str
    source_id: str
    document_id: str
    chunk_id: str
    title: str
    source_url: str
    retrieved_at: str
    content_hash: str
    text: str
    score: float = Field(ge=0, le=1)
    organisation: str = ""
    document_version: str | None = None
    snapshot_date: str = ""
    content_hash_sha256: str = ""
    retrieval_score: float | None = Field(default=None, ge=0, le=1)


class GroundedClaim(D3Model):
    text: str
    evidence_ids: list[str] = Field(min_length=1)


class D3HealthKnowledgePlan(D3Model):
    knowledge_applicable: bool
    safety_applicable: bool
    topic: str
    retrieval_required: bool
    retrieval_query: str | None = None


class D3HealthKnowledgeResult(D3Model):
    status: Literal["SUCCESS", "INSUFFICIENT_EVIDENCE", "SAFETY_ESCALATION"]
    answer_scope: str
    evidence: list[RetrievedEvidence] = Field(default_factory=list)
    grounded_summary: str = ""
    safety_level: Literal["ROUTINE", "URGENT", "EMERGENCY"] = "ROUTINE"
    safety_message: str = ""
    limitations: list[str] = Field(default_factory=list)
    knowledge_applicable: bool = True
    safety_applicable: bool = False
    topic: str = ""
    retrieved_evidence: list[RetrievedEvidence] = Field(default_factory=list)
    grounded_claims: list[GroundedClaim] = Field(default_factory=list)
    unsupported_claims_detected: bool = False
    uncertainty_required: bool = False
    abstain: bool = False
    escalation_required: bool = False
    response_constraints: list[str] = Field(default_factory=list)
    safety_status: Literal[
        "ROUTINE_INFORMATION", "CAUTION", "ESCALATION_REQUIRED", "INSUFFICIENT_EVIDENCE"
    ] = "ROUTINE_INFORMATION"


__all__ = ["D3HealthKnowledgePlan", "D3HealthKnowledgeResult", "GroundedClaim", "RetrievedEvidence"]
