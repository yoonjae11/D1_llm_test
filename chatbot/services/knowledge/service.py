"""Production constructor for the approved frozen index."""

from chatbot.resources.health_knowledge import load_approved_corpus
from .index import FrozenLexicalIndex


class AuthoritativeKnowledgeService:
    def __init__(self, retriever=None):
        if retriever is None:
            manifest, documents = load_approved_corpus()
            retriever = FrozenLexicalIndex(manifest, documents)
        self.retriever = retriever

    @property
    def manifest(self):
        return getattr(self.retriever, "manifest", {})

    def search(self, query: str, *, top_k: int = 3):
        return self.retriever.search(
            query, top_k=top_k, allowed_source_ids=["NHS_UK"],
            corpus_version=self.manifest.get("corpus_version"),
        )


__all__ = ["AuthoritativeKnowledgeService"]
