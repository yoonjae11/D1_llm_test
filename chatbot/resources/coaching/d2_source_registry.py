"""Validated source provenance for the production D2 Advisor policy."""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


Framework = Literal[
    "NHS_HWB_COACH",
    "NHS_SUPPORTED_SELF_MANAGEMENT",
    "COM_B",
    "BCTTV1",
    "MOTIVATIONAL_INTERVIEWING",
    "SELF_DETERMINATION_THEORY",
    "SENSEI_RUNTIME_CONTROL",
]


class D2PolicySource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str = Field(min_length=1)
    framework: Framework
    title: str = Field(min_length=1)
    version: str = Field(min_length=1)
    publication_year: int = Field(ge=1900, le=2100)
    source_url: str = ""
    prompt_resource: str = Field(min_length=1)
    usage: str = Field(min_length=1)
    redistribution_note: str = Field(min_length=1)
    rule_origin: Literal["SOURCE_DERIVED", "SENSEI_OPERATIONAL"]

    @field_validator(
        "source_id",
        "title",
        "version",
        "prompt_resource",
        "usage",
        "redistribution_note",
    )
    @classmethod
    def require_visible_text(cls, value: str) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("Source registry text fields cannot be blank")
        return normalized


class D2SourceRegistry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    registry_version: str = Field(min_length=1)
    bundle_id: str = Field(min_length=1)
    sources: list[D2PolicySource] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_source_ids(self):
        source_ids = [source.source_id for source in self.sources]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("Duplicate D2 policy source IDs")
        return self

    @property
    def source_ids(self) -> list[str]:
        return [source.source_id for source in self.sources]

    @property
    def prompt_resources(self) -> set[str]:
        return {source.prompt_resource for source in self.sources}


def _validate_prompt_resources(
    registry: D2SourceRegistry,
    prompt_root: Path,
) -> None:
    root = prompt_root.resolve()
    for resource in registry.prompt_resources:
        relative = PurePosixPath(resource.replace("\\", "/"))
        if (
            relative.is_absolute()
            or not relative.parts
            or relative.parts[0] != "advisor_policy"
            or relative.suffix != ".txt"
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            raise ValueError(f"Invalid D2 policy prompt resource: {resource!r}")
        candidate = root.joinpath(*relative.parts).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise ValueError(
                f"D2 policy prompt escapes its resource root: {resource!r}"
            ) from exc
        if not candidate.is_file() or not candidate.read_text(encoding="utf-8").strip():
            raise ValueError(f"Missing or empty D2 policy prompt: {resource!r}")

    policy_directory = root / "advisor_policy"
    actual_resources = {
        path.relative_to(root).as_posix()
        for path in policy_directory.glob("*.txt")
        if path.is_file()
    }
    if registry.prompt_resources != actual_resources:
        missing_sources = sorted(actual_resources - registry.prompt_resources)
        missing_files = sorted(registry.prompt_resources - actual_resources)
        raise ValueError(
            "D2 source registry and policy fragments differ: "
            f"unregistered={missing_sources}, missing={missing_files}"
        )


def load_d2_source_registry(
    path: Path | None = None,
    *,
    prompt_root: Path | None = None,
) -> D2SourceRegistry:
    """Load and validate source metadata and its policy-resource coverage."""

    registry_path = (
        Path(path)
        if path is not None
        else Path(__file__).with_name("d2_source_registry.json")
    )
    if not registry_path.is_file():
        raise FileNotFoundError(f"D2 source registry not found: {registry_path}")
    registry = D2SourceRegistry.model_validate(
        json.loads(registry_path.read_text(encoding="utf-8"))
    )
    root = (
        Path(prompt_root)
        if prompt_root is not None
        else Path(__file__).resolve().parents[2] / "prompts" / "d2_coaching_strategy"
    )
    _validate_prompt_resources(registry, root)
    return registry


__all__ = [
    "D2PolicySource",
    "D2SourceRegistry",
    "Framework",
    "load_d2_source_registry",
]
