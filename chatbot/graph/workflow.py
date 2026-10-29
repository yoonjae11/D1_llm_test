"""Canonical six-stage domain orchestration graph."""
from langgraph.graph import END, StateGraph
from chatbot.graph.state import AgentState
from chatbot.nodes.common.prepare_context import prepare_context
from chatbot.nodes.common.aggregate_domain_results import aggregate_domain_results
from chatbot.nodes.common.generate_final_response import generate_final_response
from chatbot.nodes.common.persist_trace_and_context import persist_trace_and_context
from chatbot.nodes.planning.plan_domain_execution import plan_domain_execution
from chatbot.nodes.orchestration.execute_domains import execute_domains


def build_graph(checkpointer=None):
    graph = StateGraph(AgentState)
    nodes = [
        ("prepare_context", prepare_context),
        ("plan_domain_execution", plan_domain_execution),
        ("execute_domains", execute_domains),
        ("aggregate_domain_results", aggregate_domain_results),
        ("generate_final_response", generate_final_response),
        ("persist_trace_and_context", persist_trace_and_context),
    ]
    for name, node in nodes:
        graph.add_node(name, node)
    graph.set_entry_point(nodes[0][0])
    for (source, _), (target, _) in zip(nodes, nodes[1:]):
        graph.add_edge(source, target)
    graph.add_edge(nodes[-1][0], END)
    return graph.compile(checkpointer=checkpointer)


__all__ = ["build_graph"]
