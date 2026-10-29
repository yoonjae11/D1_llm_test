"""D2 graph wiring: preparation, one strategy call, validation, result wrapping."""
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph
from chatbot.schemas.domain_execution_result import DomainExecutionResult, DomainRunOutput
from .prepare_coaching_context import prepare_coaching_context
from .plan_coaching_strategy import plan_coaching_strategy
from .validate_coaching_plan import validate_coaching_plan
from .produce_coaching_result import produce_coaching_result


class D2State(TypedDict, total=False):
    request: dict[str, Any]
    dependency_results: dict[str, Any]
    coaching_context: dict[str, Any]
    bct_catalogue: dict[str, Any]
    catalogue_status: str
    source_registry: dict[str, Any]
    source_registry_status: str
    coaching_plan: dict[str, Any]
    coaching_status: str
    validation_repairs: list[str]
    llm_calls: int
    failure_type: str | None
    started_at: float
    d2_result: dict[str, Any]


def build_d2_graph():
    graph = StateGraph(D2State)
    nodes = [
        ("prepare_coaching_context", prepare_coaching_context),
        ("plan_coaching_strategy", plan_coaching_strategy),
        ("validate_coaching_plan", validate_coaching_plan),
        ("produce_coaching_result", produce_coaching_result),
    ]
    for name, node in nodes:
        graph.add_node(name, node)
    graph.set_entry_point(nodes[0][0])
    for (source, _), (target, _) in zip(nodes, nodes[1:]):
        graph.add_edge(source, target)
    graph.add_edge(nodes[-1][0], END)
    return graph.compile()


def run_d2(state: dict) -> DomainRunOutput:
    result = build_d2_graph().invoke({
        "request": state, "dependency_results": state.get("dependency_results") or {},
    })
    return DomainRunOutput(result=DomainExecutionResult.model_validate(result["d2_result"]))
