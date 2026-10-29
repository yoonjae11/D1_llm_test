"""Port implemented by production and evaluation knowledge indexes."""

from typing import Protocol

from chatbot.schemas.d3_health_knowledge_result import RetrievedEvidence


class KnowledgeRetriever(Protocol):
    def search(
        self, query: str, *, top_k: int,
        allowed_source_ids: list[str] | None = None,
        corpus_version: str | None = None,
    ) -> list[RetrievedEvidence]: ...


__all__ = ["KnowledgeRetriever"]
