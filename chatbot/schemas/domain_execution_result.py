"""Serializable domain wrapper shared by evidence and Advisor strategy."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .domain_execution_plan import Domain


class DomainExecutionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    domain: Domain
    selected: bool = True
    status: Literal["success", "skipped", "not_implemented", "insufficient_information", "blocked_dependency", "error"]
    summary: str = ""
    structured_result: dict[str, Any] = Field(default_factory=dict)
    missing_information: list[str] = Field(default_factory=list)
    confidence: float | None = Field(default=None, ge=0, le=1)
    provenance: dict[str, Any] = Field(default_factory=dict)


class DomainRunOutput(BaseModel):
    """Uniform runner interface: domain wrapper plus explicitly owned updates.

    DomainRunOutput does not serialize a complete child graph snapshot, which
    would duplicate message history and leak other domains' state.
    """

    model_config = ConfigDict(extra="forbid")
    result: DomainExecutionResult
    state_updates: dict[str, Any] = Field(default_factory=dict)
