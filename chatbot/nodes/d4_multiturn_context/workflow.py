"""D4 within-session context-resolution graph."""
from typing import Any, TypedDict
from langgraph.graph import END, StateGraph
from chatbot.schemas.domain_execution_result import DomainExecutionResult, DomainRunOutput
from .pipeline import (
    prepare_context_candidates, produce_context_result,
    resolve_context_reference, validate_context_resolution,
)


class D4State(TypedDict, total=False):
    request: dict[str, Any]
    dependency_results: dict[str, Any]
    d4_result: dict[str, Any]
    question: str
    context_candidates: list[dict[str, Any]]
    context_evidence_spans: list[dict[str, Any]]
    resolution_status: str
    resolved_items: list[dict[str, Any]]
    unresolved_references: list[str]
    hallucinated_context_detected: bool
    started_at: float


def build_d4_graph():
    graph = StateGraph(D4State)
    nodes = [
        ("prepare_context_candidates", prepare_context_candidates),
        ("resolve_context_reference", resolve_context_reference),
        ("validate_context_resolution", validate_context_resolution),
        ("produce_context_result", produce_context_result),
    ]
    for name, node in nodes:
        graph.add_node(name, node)
    graph.set_entry_point(nodes[0][0])
    for (source, _), (target, _) in zip(nodes, nodes[1:]):
        graph.add_edge(source, target)
    graph.add_edge(nodes[-1][0], END)
    return graph.compile()


def run_d4(state: dict) -> DomainRunOutput:
    result = build_d4_graph().invoke({
        "request": state, "dependency_results": state.get("dependency_results") or {},
    })
    return DomainRunOutput(result=DomainExecutionResult.model_validate(result["d4_result"]))
