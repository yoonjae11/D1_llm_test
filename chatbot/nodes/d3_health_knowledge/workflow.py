"""D3 evidence retrieval and health-safety graph."""
from typing import Any, TypedDict
from langgraph.graph import END, StateGraph
from chatbot.schemas.domain_execution_result import DomainExecutionResult, DomainRunOutput
from .pipeline import (
    apply_scope_and_safety_policy, classify_knowledge_and_safety_need, formulate_retrieval_query,
    prepare_health_knowledge_request, produce_health_knowledge_result, retrieve_authoritative_evidence,
    validate_and_rank_evidence,
)


class D3State(TypedDict, total=False):
    request: dict[str, Any]
    dependency_results: dict[str, Any]
    d3_result: dict[str, Any]
    question: str
    started_at: float
    health_topics: list[str]
    safety_level: str
    safety_message: str
    knowledge_query: str
    knowledge_plan: dict[str, Any]
    corpus_manifest: dict[str, Any]
    retrieved_evidence: list[dict[str, Any]]
    d3_issues: list[str]


def build_d3_graph():
    graph = StateGraph(D3State)
    nodes = [
        ("prepare_health_knowledge_request", prepare_health_knowledge_request),
        ("classify_knowledge_and_safety_need", classify_knowledge_and_safety_need),
        ("formulate_retrieval_query", formulate_retrieval_query),
        ("retrieve_authoritative_evidence", retrieve_authoritative_evidence),
        ("validate_and_rank_evidence", validate_and_rank_evidence),
        ("apply_scope_and_safety_policy", apply_scope_and_safety_policy),
        ("produce_health_knowledge_result", produce_health_knowledge_result),
    ]
    for name, node in nodes:
        graph.add_node(name, node)
    graph.set_entry_point(nodes[0][0])
    for (source, _), (target, _) in zip(nodes, nodes[1:]):
        graph.add_edge(source, target)
    graph.add_edge(nodes[-1][0], END)
    return graph.compile()


def run_d3(state: dict) -> DomainRunOutput:
    result = build_d3_graph().invoke({
        "request": state, "dependency_results": state.get("dependency_results") or {},
    })
    return DomainRunOutput(result=DomainExecutionResult.model_validate(result["d3_result"]))
