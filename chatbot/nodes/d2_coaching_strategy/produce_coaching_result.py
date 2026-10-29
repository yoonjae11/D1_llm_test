"""Wrap the validated Advisor strategy, without generating a chat response."""
from chatbot.nodes.common.support import elapsed_ms
from chatbot.schemas.domain_execution_result import DomainExecutionResult
from .plan_coaching_strategy import D2_ADVISOR_POLICY_SHA256, D2_PROMPT_HASHES
from .validate_coaching_plan import D2_VALIDATION_POLICY_IDS


def produce_coaching_result(state: dict) -> dict:
    plan = state["coaching_plan"]
    dependencies = state.get("dependency_results") or {}
    d1 = dependencies.get("D1")
    catalogue = state.get("bct_catalogue") or {}
    source_registry = state.get("source_registry") or {}
    policy_source_ids = [
        source.get("source_id")
        for source in source_registry.get("sources", [])
        if source.get("source_id")
    ]
    missing = [
        f"Optional dependency {domain} returned {result['status']}"
        for domain, result in dependencies.items() if result["status"] != "success"
    ]
    return {"d2_result": DomainExecutionResult(
        domain="D2", status=state["coaching_status"], summary=plan["dialogue_objective"],
        structured_result=plan, missing_information=missing,
        provenance={
            "workflow": ["prepare_coaching_context", "plan_coaching_strategy", "validate_coaching_plan", "produce_coaching_result"],
            "workflow_version": "d2_coaching_strategy_v3",
            "prompt_id": "d2_advisor_policy_bundle",
            "prompt_version": "d2_advisor_v3",
            "prompt_resource_hashes": D2_PROMPT_HASHES,
            "policy_bundle_id": source_registry.get("bundle_id", "unavailable"),
            "policy_bundle_sha256": D2_ADVISOR_POLICY_SHA256,
            "prompt_sha256": D2_ADVISOR_POLICY_SHA256,
            "policy_source_ids": policy_source_ids,
            "source_registry_version": source_registry.get("registry_version"),
            "source_registry_status": state.get("source_registry_status", "error"),
            "llm_calls": state["llm_calls"],
            "latency_ms": elapsed_ms(state["started_at"]),
            "validation_repairs": state["validation_repairs"],
            "validation_policy_ids": D2_VALIDATION_POLICY_IDS,
            "bct_catalogue_status": state["catalogue_status"],
            "bct_catalogue_id": catalogue.get("catalogue_id"),
            "bct_catalogue_version": catalogue.get("version"),
            "bct_catalogue_source_id": catalogue.get("source_id"),
            "bct_catalogue_entry_count": len(catalogue.get("entries", [])),
            "bct_subset_rationale_version": "1.0.0",
            "bct_subset_is_complete_taxonomy": False,
            "bct_source": catalogue.get("source", ""),
            "d1_evidence_received": d1 is not None,
            "d1_status": d1.get("status") if d1 is not None else None,
            "dependency_statuses": {key: value["status"] for key, value in dependencies.items()},
            "failure_type": state.get("failure_type"),
        },
    ).model_dump()}
