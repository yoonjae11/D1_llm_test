"""Load the sourced BCTTv1 subset allowed for production D2 planning."""

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class BCTEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    definition: str = Field(min_length=1)

    @field_validator("id", "name", "definition")
    @classmethod
    def require_visible_text(cls, value: str) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("BCT entry fields cannot be blank")
        return normalized


class BCTCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid")
    catalogue_id: str = ""
    version: str = ""
    source_id: str = ""
    source: str = ""
    source_url: str = ""
    catalogue_scope: str = ""
    redistribution_note: str = ""
    entries: list[BCTEntry] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_reference(self):
        if self.entries and not self.source.strip():
            raise ValueError("Catalogue entries require an authoritative source")
        if len({item.id for item in self.entries}) != len(self.entries):
            raise ValueError("Duplicate BCT catalogue IDs")
        return self

    @property
    def supported_ids(self) -> set[str]:
        return {item.id for item in self.entries}


def load_bct_catalog(path: Path | None = None) -> BCTCatalog:
    path = Path(path) if path is not None else Path(__file__).with_name("bct_catalog.json")
    if not path.exists():
        return BCTCatalog()
    catalogue = BCTCatalog.model_validate(json.loads(path.read_text(encoding="utf-8")))
    rationale_path = path.with_name("bct_subset_rationale.json")
    if rationale_path.is_file():
        rationale = json.loads(rationale_path.read_text(encoding="utf-8"))
        if rationale.get("subset_id") != catalogue.catalogue_id:
            raise ValueError("BCT subset rationale and catalogue IDs differ")
        if set(rationale.get("allowed_ids") or []) != catalogue.supported_ids:
            raise ValueError("BCT subset rationale and catalogue entries differ")
        rationale_ids = {item.get("bct_id") for item in rationale.get("technique_rationales", [])}
        if rationale_ids != catalogue.supported_ids:
            raise ValueError("Every BCT subset entry requires an explicit rationale")
    subset_path = path.with_name("sensei_bct_allowed_subset.json")
    if subset_path.is_file():
        subset = json.loads(subset_path.read_text(encoding="utf-8"))
        if subset.get("subset_id") != catalogue.catalogue_id or set(subset.get("allowed_ids") or []) != catalogue.supported_ids:
            raise ValueError("Sensei BCT allowlist and catalogue entries differ")
    return catalogue


__all__ = ["BCTCatalog", "BCTEntry", "load_bct_catalog"]
