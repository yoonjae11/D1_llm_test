"""Deterministic, allowlisted coaching context with an explicit evidence boundary."""

from hashlib import sha256
import json
import time
from chatbot.nodes.common.support import conversation_context
from chatbot.resources.coaching.bct_catalog import BCTCatalog, load_bct_catalog
from chatbot.resources.coaching.d2_source_registry import load_d2_source_registry


def build_coaching_context(state: dict) -> dict:
    context = conversation_context(state)
    context["concrete_plan_requested"] = bool(
        (state.get("domain_execution_plan") or {}).get("concrete_plan_requested")
    )
    # The executor supplies only this domain's declared dependencies. Never read
    # stale query_result/last_plan data or other domains' undelivered results.
    dependencies = state.get("dependency_results") or {}
    context["dependency_results"] = {
        domain: {key: result[key] for key in (
            "domain", "status", "summary", "structured_result", "missing_information"
        ) if key in result}
        for domain, result in dependencies.items()
    }
    context["sensor_evidence"] = context["dependency_results"].get("D1")
    allowed = ["USER_CURRENT_TURN"]
    d1 = context["dependency_results"].get("D1")
    if d1 and d1.get("status") == "success":
        digest = sha256(json.dumps(d1.get("structured_result") or {}, sort_keys=True, default=str).encode()).hexdigest()[:16]
        allowed.append(f"D1:{digest}")
    d3 = context["dependency_results"].get("D3")
    if d3 and d3.get("status") == "success":
        allowed.extend(
            f"D3:{item['evidence_id']}" for item in (d3.get("structured_result") or {}).get("evidence", [])
            if item.get("evidence_id")
        )
    d4 = context["dependency_results"].get("D4")
    if d4 and d4.get("status") == "success":
        allowed.extend(
            f"D4:{turn_id}" for turn_id in (d4.get("structured_result") or {}).get("source_turn_ids", [])
        )
    context["allowed_evidence_ids"] = list(dict.fromkeys(allowed))
    context["health_safety"] = (d3 or {}).get("structured_result") if d3 else None
    context["resolved_conversation_context"] = (d4 or {}).get("structured_result") if d4 else None
    return context


def prepare_coaching_context(state: dict) -> dict:
    started = time.perf_counter()
    failures = []
    try:
        catalogue = load_bct_catalog()
    except Exception as exc:
        catalogue = BCTCatalog()
        failures.append(f"bct_catalogue:{type(exc).__name__}")
    try:
        source_registry = load_d2_source_registry()
    except Exception as exc:
        source_registry = None
        failures.append(f"source_registry:{type(exc).__name__}")
    catalogue_status = (
        "error"
        if any(item.startswith("bct_catalogue:") for item in failures)
        else "available"
        if catalogue.entries
        else "missing"
    )
    return {
        "coaching_context": build_coaching_context(state["request"]),
        "bct_catalogue": catalogue.model_dump(),
        "catalogue_status": catalogue_status,
        "source_registry": (
            source_registry.model_dump() if source_registry is not None else {}
        ),
        "source_registry_status": (
            "available" if source_registry is not None else "error"
        ),
        "coaching_status": "error" if failures else "success",
        "failure_type": ";".join(failures) or None,
        "llm_calls": 0,
        "started_at": started,
    }
