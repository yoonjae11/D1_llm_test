"""Validate and load only approved, manifest-declared frozen documents."""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def canonical_content_hash(document: dict[str, Any]) -> str:
    content = "\n".join(str(item.get("text") or "") for item in document.get("chunks", []))
    return sha256(content.encode("utf-8")).hexdigest()


def load_approved_corpus(root: Path | None = None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    base = Path(root) if root is not None else ROOT
    registry = _read_json(base / "source_registry.json")
    manifest = _read_json(base / "corpus_manifest.json")
    allowed = {
        source["source_id"] for source in registry["sources"]
        if source.get("runtime_allowed") and source.get("license_status") == "APPROVED"
    }
    documents = []
    for entry in manifest["documents"]:
        if entry["source_id"] not in allowed:
            continue
        candidate = (base / entry["path"]).resolve()
        candidate.relative_to(base.resolve())
        document = _read_json(candidate)
        if document["document_id"] != entry["document_id"] or document["source_id"] != entry["source_id"]:
            raise ValueError("Health corpus manifest identity mismatch")
        if (
            document.get("licence_review_status") != "APPROVED"
            or document.get("status") != "ACTIVE"
            or document.get("allowed_for_research_rag") is not True
        ):
            continue
        expected = canonical_content_hash(document)
        if document.get("content_hash") != expected or document.get("content_hash_sha256") != expected:
            raise ValueError(f"Health corpus hash mismatch: {document['document_id']}")
        for chunk in document.get("chunks", []):
            chunk_hash = sha256(str(chunk.get("text") or "").encode("utf-8")).hexdigest()
            if chunk.get("content_hash_sha256") != chunk_hash:
                raise ValueError(f"Health chunk hash mismatch: {chunk.get('chunk_id')}")
        documents.append(document)
    return {**manifest, "source_registry_version": registry["registry_version"]}, documents


__all__ = ["canonical_content_hash", "load_approved_corpus"]
