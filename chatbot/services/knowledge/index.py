"""Deterministic lexical index over a frozen D3 corpus."""

from __future__ import annotations

import re

from chatbot.schemas.d3_health_knowledge_result import RetrievedEvidence


_TOKEN = re.compile(r"[a-z0-9]+|[가-힣]+", re.I)
_STOP = {"the", "a", "an", "is", "are", "and", "or", "to", "of", "for", "my", "i", "me", "what", "how"}


class FrozenLexicalIndex:
    retriever_id = "frozen_lexical_index"
    retriever_version = "1.0.0"
    ranking_method = "token_overlap"
    ranking_version = "1.0.0"

    def __init__(self, manifest: dict, documents: list[dict]):
        self.manifest = manifest
        self.documents = documents

    def search(self, query: str, *, top_k: int, allowed_source_ids=None, corpus_version=None):
        if corpus_version and corpus_version != self.manifest.get("corpus_version"):
            return []
        allowed = set(allowed_source_ids or [])
        query_terms = {token.lower() for token in _TOKEN.findall(query) if token.lower() not in _STOP}
        results = []
        for document in self.documents:
            if allowed and document["source_id"] not in allowed:
                continue
            for chunk in document["chunks"]:
                terms = set(token.lower() for token in _TOKEN.findall(document["title"] + " " + chunk["text"]))
                overlap = len(query_terms & terms)
                if not overlap:
                    continue
                score = min(1.0, 0.45 + overlap * 0.1)
                results.append(RetrievedEvidence(
                    evidence_id=f"{document['document_id']}:{chunk['chunk_id']}",
                    source_id=document["source_id"], document_id=document["document_id"],
                    chunk_id=chunk["chunk_id"], title=document["title"], source_url=document["source_url"],
                    retrieved_at=document["retrieved_at"], content_hash=document["content_hash"],
                    text=chunk["text"], score=score, organisation=document.get("organisation", ""),
                    document_version=document.get("version"), snapshot_date=document["retrieved_at"],
                    content_hash_sha256=document["content_hash"], retrieval_score=score,
                ))
        return sorted(results, key=lambda item: (-item.score, item.evidence_id))[:top_k]


__all__ = ["FrozenLexicalIndex"]
