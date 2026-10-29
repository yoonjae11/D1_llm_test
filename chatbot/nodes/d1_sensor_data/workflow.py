"""D1 subgraph wiring, transitions and runner."""
import re
from typing import Any, Mapping

from langgraph.graph import END, StateGraph
from chatbot.config import get_settings, normalize_kql_generate_method
from chatbot.graph.state import AgentState
from chatbot.policies.evidence_feedback import feedback_message, result_has_no_evidence, unobserved_selection
from chatbot.policies.kql_errors import is_query_validation_error
from chatbot.schemas.domain_execution_result import DomainExecutionResult, DomainRunOutput
from .plan_sensor_query import plan_sensor_query
from .generate_sensor_query import generate_sensor_query
from .guard_sensor_query import guard_sensor_query
from .repair_sensor_query import repair_attempt_limit, repair_sensor_query
from .execute_sensor_query import execute_sensor_query
from .produce_sensor_result import produce_sensor_result

D1_STATE_KEYS = (
    "plan", "last_plan", "prev_plan", "raw_plan", "sanitized_plan", "kql_query",
    "kql_source", "kql_generation_status", "kql_generation_trace", "kql_guard",
    "guard_status", "execution_status", "query_result", "coverage_context",
    "error", "error_type", "adx_error", "mcp_error", "failed_kql", "kql_issues",
    "repair_attempts", "d1_result", "sensor_plan_trace",
)


def _can_repair_with_llm(state: Mapping[str, Any]) -> bool:
    trace = state.get("kql_generation_trace")
    traced_method = trace.get("method") if isinstance(trace, Mapping) else None
    method = normalize_kql_generate_method(
        get_settings().kql_generate_method if traced_method is None else traced_method
    )
    return method == "llm" and int(state.get("repair_attempts") or 0) < repair_attempt_limit(state)


def _is_synthetic_source_rejection(state: Mapping[str, Any]) -> bool:
    """Permit removal of literal/scalar pseudo-sources, not unknown data reads."""
    guard = state.get("kql_guard")
    issues = guard.get("issues") if isinstance(guard, Mapping) else None
    if not isinstance(issues, list) or not issues:
        return False
    for issue in issues:
        if issue == "KQL uses an unsupported or dynamically resolved tabular source.":
            if "datatable(" not in str(state.get("kql_query") or "").lower():
                return False
            continue
        match = re.fullmatch(
            r"KQL uses a direct tabular source outside the health-chatbot allowlist: (.+)\.",
            str(issue),
        )
        if not match:
            return False
        names = {name.strip().lower() for name in match.group(1).split(",")}
        if not names or not names <= {"print", "iff", "iif", "datatable"}:
            return False
    # Only regeneration is permitted. The replacement must pass the full guard.
    return True


def guard_decision(state: Mapping[str, Any]) -> str:
    """Execute safe KQL, re-guard one repair, or return a blocked result."""
    status = state.get("guard_status")
    if status == "execute":
        return "execute_sensor_query"
    if _can_repair_with_llm(state) and (
        status == "repair" or (status == "error" and _is_synthetic_source_rejection(state))
    ):
        return "repair_sensor_query"
    return "produce_sensor_result"


def _unobserved_selection(state: Mapping[str, Any]):
    """An empty result from records this user does not have (offered once per run)."""
    trace = state.get("kql_generation_trace")
    traced_method = trace.get("method") if isinstance(trace, Mapping) else None
    method = normalize_kql_generate_method(
        get_settings().kql_generate_method if traced_method is None else traced_method)
    if (method != "llm" or state.get("evidence_feedback_used")
            or not result_has_no_evidence(state.get("query_result"))):
        return None
    review = trace.get("source_review") if isinstance(trace, Mapping) else None
    observations = review.get("schema_observations") if isinstance(review, Mapping) else None
    return unobserved_selection(str(state.get("kql_query") or ""), observations)


def request_evidence_feedback(state: Mapping[str, Any]) -> dict:
    """Hand query generation the observed fact; the model decides what it means."""
    kql = str(state.get("kql_query") or "")
    finding = _unobserved_selection(state)
    if not finding:
        return {"evidence_feedback_used": True}
    return {"evidence_feedback_request": {
        "finding": finding, "earlier_kql": kql,
        "message": feedback_message(finding) + "\nEarlier candidate:\n" + kql,
    }}


def execution_decision(state: Mapping[str, Any]) -> str:
    """Repair a clear Kusto compiler rejection once; never retry auth/outages.

    An empty answer that selected only records this user does not have goes
    back to query generation once, with that observed fact as feedback.
    """
    if (state.get("execution_status") == "success" and state.get("guard_status") == "execute"
            and _unobserved_selection(state)):
        return "request_evidence_feedback"
    if (
        state.get("execution_status") != "error"
        or state.get("guard_status") != "execute"
        or not _can_repair_with_llm(state)
    ):
        return "produce_sensor_result"
    error = str(state.get("adx_error") or state.get("error") or "")
    if is_query_validation_error(error):
        return "repair_sensor_query"
    return "produce_sensor_result"


def plan_decision(state: Mapping[str, Any]) -> str:
    plan = state.get("plan")
    if isinstance(plan, Mapping) and plan.get("execution_ready") is False:
        return "produce_sensor_result"
    return "generate_sensor_query"


def query_generation_decision(state: Mapping[str, Any]) -> str:
    return "guard_sensor_query" if str(state.get("kql_query") or "").strip() else "produce_sensor_result"


def build_d1_graph():
    graph = StateGraph(AgentState)
    for name, node in (
        ("plan_sensor_query", plan_sensor_query), ("generate_sensor_query", generate_sensor_query),
        ("guard_sensor_query", guard_sensor_query), ("repair_sensor_query", repair_sensor_query),
        ("execute_sensor_query", execute_sensor_query), ("produce_sensor_result", produce_sensor_result),
        ("request_evidence_feedback", request_evidence_feedback),
    ):
        graph.add_node(name, node)
    graph.set_entry_point("plan_sensor_query")
    graph.add_conditional_edges("plan_sensor_query", plan_decision, {
        "generate_sensor_query": "generate_sensor_query", "produce_sensor_result": "produce_sensor_result",
    })
    graph.add_conditional_edges("generate_sensor_query", query_generation_decision, {
        "guard_sensor_query": "guard_sensor_query", "produce_sensor_result": "produce_sensor_result",
    })
    graph.add_conditional_edges("guard_sensor_query", guard_decision, {
        "execute_sensor_query": "execute_sensor_query", "repair_sensor_query": "repair_sensor_query",
        "produce_sensor_result": "produce_sensor_result",
    })
    # No query exists after a failed API/empty repair. Preserve that diagnostic;
    # every actual replacement still goes through the full authorization guard.
    graph.add_conditional_edges("repair_sensor_query", query_generation_decision, {
        "guard_sensor_query": "guard_sensor_query", "produce_sensor_result": "produce_sensor_result",
    })
    graph.add_conditional_edges("execute_sensor_query", execution_decision, {
        "request_evidence_feedback": "request_evidence_feedback",
        "repair_sensor_query": "repair_sensor_query",
        "produce_sensor_result": "produce_sensor_result",
    })
    graph.add_edge("request_evidence_feedback", "generate_sensor_query")
    graph.add_edge("produce_sensor_result", END)
    return graph.compile()


def run_d1(state: dict) -> DomainRunOutput:
    if state.get("onboarding_required"):
        return DomainRunOutput(result=DomainExecutionResult(
            domain="D1", status="insufficient_information",
            summary="Timezone onboarding required before sensor analysis",
            missing_information=["Resolved user timezone"],
        ))
    # The evidence-feedback step is per question; never inherit it from a prior turn.
    result = build_d1_graph().invoke({**state, "evidence_feedback_used": None,
                                      "evidence_feedback_request": None})
    return DomainRunOutput(
        result=DomainExecutionResult.model_validate(result["d1_result"]),
        state_updates={key: result[key] for key in D1_STATE_KEYS if key in result},
    )
