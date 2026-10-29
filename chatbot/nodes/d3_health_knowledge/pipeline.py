"""Deterministic retrieval and fail-closed safety stages for D3."""

from __future__ import annotations

from hashlib import sha256
import time

from chatbot.graph.state import latest_user_message
from chatbot.nodes.common.support import elapsed_ms
from chatbot.schemas.d3_health_knowledge_result import D3HealthKnowledgePlan, D3HealthKnowledgeResult, GroundedClaim
from chatbot.schemas.domain_execution_result import DomainExecutionResult
from chatbot.services.knowledge import AuthoritativeKnowledgeService


D3_WORKFLOW_VERSION = "d3_health_knowledge_v1"
_EMERGENCY = (
    "chest pressure", "does not go away", "pain spreading", "shortness of breath",
    "sweating", "light-headed", "lightheaded", "가슴 압박", "퍼지는 통증",
    "호흡곤란", "숨이 안", "식은땀", "어지러",
)
_CHEST = ("chest", "heart attack", "가슴", "흉통", "심장")
_ACTIVITY = ("exercise", "activity", "active", "walking", "workout", "운동", "활동", "걷")
_FATIGUE = ("sleep", "tired", "fatigue", "exhaust", "수면", "잠", "피곤", "피로")


def prepare_health_knowledge_request(state: dict) -> dict:
    request = state["request"]
    question = latest_user_message(request.get("messages") or [])
    return {"question": question, "started_at": time.perf_counter(), "d3_issues": []}


def classify_knowledge_and_safety_need(state: dict) -> dict:
    lowered = state["question"].lower()
    safety = "EMERGENCY" if any(token in lowered for token in _EMERGENCY) and any(token in lowered for token in _CHEST) else "ROUTINE"
    if any(token in lowered for token in _CHEST):
        topics = ["chest_pain"]
    elif any(token in lowered for token in _ACTIVITY):
        topics = ["physical_activity"]
    elif any(token in lowered for token in _FATIGUE):
        topics = ["fatigue"]
    else:
        topics = []
    plan = D3HealthKnowledgePlan(
        knowledge_applicable=bool(topics), safety_applicable=safety != "ROUTINE",
        topic=topics[0] if topics else "", retrieval_required=bool(topics),
    )
    return {"health_topics": topics, "safety_level": safety, "knowledge_plan": plan.model_dump()}


def formulate_retrieval_query(state: dict) -> dict:
    expansions = {
        "chest_pain": "chest pain pressure spreading shortness breath emergency",
        "physical_activity": "adult physical activity exercise weekly strengthening",
        "fatigue": "tiredness fatigue sleep daily life GP",
    }
    query = " ".join([state["question"]] + [expansions[item] for item in state.get("health_topics") or []])[:1000]
    plan = dict(state.get("knowledge_plan") or {})
    plan["retrieval_query"] = query or None
    return {"knowledge_query": query, "knowledge_plan": plan}


def retrieve_authoritative_evidence(state: dict) -> dict:
    try:
        service = AuthoritativeKnowledgeService()
        evidence = [item.model_dump() for item in service.search(state.get("knowledge_query") or "", top_k=3)]
        manifest = service.manifest
    except Exception as exc:
        return {"corpus_manifest": {}, "retrieved_evidence": [], "d3_issues": [f"corpus:{type(exc).__name__}"]}
    return {"corpus_manifest": manifest, "retrieved_evidence": evidence}


def validate_and_rank_evidence(state: dict) -> dict:
    valid = [item for item in state.get("retrieved_evidence", []) if item.get("score", 0) >= 0.5]
    issues = list(state.get("d3_issues") or [])
    if not valid:
        issues.append("no_approved_evidence")
    return {"retrieved_evidence": valid, "d3_issues": issues}


def apply_scope_and_safety_policy(state: dict) -> dict:
    if state.get("safety_level") == "EMERGENCY":
        return {
            "safety_message": (
                "Your message may describe emergency chest-pain warning signs. Call your local emergency "
                "number now (999 in the UK); do not wait for chatbot guidance."
            ),
            "safety_level": "EMERGENCY",
        }
    return {"safety_message": "", "safety_level": "ROUTINE"}


def produce_health_knowledge_result(state: dict) -> dict:
    evidence = state.get("retrieved_evidence") or []
    safety_level = state.get("safety_level", "ROUTINE")
    if safety_level == "EMERGENCY":
        status = "SAFETY_ESCALATION"
    elif evidence:
        status = "SUCCESS"
    else:
        status = "INSUFFICIENT_EVIDENCE"
    grounded = " ".join(item["text"] for item in evidence)
    claims = [GroundedClaim(text=item["text"], evidence_ids=[item["evidence_id"]]) for item in evidence]
    result = D3HealthKnowledgeResult(
        status=status,
        answer_scope=", ".join(state.get("health_topics") or []) or "unresolved health topic",
        evidence=evidence,
        grounded_summary=grounded,
        safety_level=safety_level,
        safety_message=state.get("safety_message", ""),
        limitations=(
            ["No approved corpus evidence matched the request; do not answer from model memory."]
            if not evidence else ["General information only; this does not diagnose or prescribe treatment."]
        ),
        knowledge_applicable=bool((state.get("knowledge_plan") or {}).get("knowledge_applicable")),
        safety_applicable=safety_level != "ROUTINE",
        topic=(state.get("knowledge_plan") or {}).get("topic", ""),
        retrieved_evidence=evidence, grounded_claims=claims,
        uncertainty_required=not bool(evidence), abstain=not bool(evidence),
        escalation_required=safety_level == "EMERGENCY",
        response_constraints=[
            "Use only retrieved evidence for health claims.",
            "Do not diagnose, prescribe, or direct medication changes.",
            "Safety escalation takes precedence over coaching and style.",
        ],
        safety_status=(
            "ESCALATION_REQUIRED" if safety_level == "EMERGENCY" else
            "INSUFFICIENT_EVIDENCE" if not evidence else "ROUTINE_INFORMATION"
        ),
    )
    manifest = state.get("corpus_manifest") or {}
    wrapper_status = "success" if status in {"SUCCESS", "SAFETY_ESCALATION"} else "insufficient_information"
    return {"d3_result": DomainExecutionResult(
        domain="D3", status=wrapper_status,
        summary=result.safety_message or result.grounded_summary or "No approved evidence matched the request.",
        structured_result=result.model_dump(),
        missing_information=result.limitations if not evidence else [],
        confidence=1.0 if safety_level == "EMERGENCY" else (evidence[0]["score"] if evidence else 0.0),
        provenance={
            "workflow_version": D3_WORKFLOW_VERSION,
            "workflow": ["prepare_health_knowledge_request", "classify_knowledge_and_safety_need", "formulate_retrieval_query", "retrieve_authoritative_evidence", "validate_and_rank_evidence", "apply_scope_and_safety_policy", "produce_health_knowledge_result"],
            "corpus_id": manifest.get("corpus_id"), "corpus_version": manifest.get("corpus_version"),
            "source_registry_version": manifest.get("source_registry_version"),
            "retriever_id": "frozen_lexical_index", "retriever_version": "1.0.0",
            "ranking_method": "token_overlap", "ranking_version": "1.0.0", "requested_top_k": 3,
            "retrieved_document_ids": [item["document_id"] for item in evidence],
            "retrieved_chunk_ids": [item["chunk_id"] for item in evidence],
            "retrieved_content_hashes": [item["content_hash"] for item in evidence],
            "query_hash": sha256(str(state.get("knowledge_query") or "").encode()).hexdigest(),
            "prompt_id": None, "prompt_version": None, "prompt_sha256": None,
            "safety_policy": "sensei_d3_safety_v1_provisional",
            "llm_calls": 0, "latency_ms": elapsed_ms(state["started_at"]),
            "issues": state.get("d3_issues") or [],
            "failure_type": None,
        },
    ).model_dump()}


__all__ = [name for name in globals() if name.startswith(("prepare_", "classify_", "formulate_", "retrieve_", "validate_", "apply_", "produce_"))]
