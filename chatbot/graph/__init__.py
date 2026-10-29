"""Public LangGraph API for Sensei conversations."""

from .state import AgentState


def build_graph(checkpointer=None):
    """Load graph composition lazily to keep state imports dependency-light."""

    from .workflow import build_graph as _build_graph

    return _build_graph(checkpointer=checkpointer)


__all__ = ["AgentState", "build_graph"]
