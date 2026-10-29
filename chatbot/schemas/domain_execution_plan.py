"""Generic D1-D4 plan shape; scheduling policy lives in the plan validator."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Domain = Literal["D1", "D2", "D3", "D4"]
DomainName = Domain
DOMAIN_NAMES: tuple[Domain, ...] = ("D1", "D2", "D3", "D4")
Intent = Literal["data", "chat", "unsupported", "clarification", "capabilities", "alert_config"]


class DomainRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    need: str = ""
    # Dependencies are required unless explicitly listed as optional. Optional
    # failures are still delivered and traced, never presented as evidence.
    optional_dependencies: list[Domain] = Field(default_factory=list)


class DomainExecutionPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent: Intent
    selected_domains: list[Domain] = Field(default_factory=list)
    domain_requirements: dict[Domain, DomainRequirement] = Field(default_factory=dict)
    dependencies: dict[Domain, list[Domain]] = Field(default_factory=dict)
    execution_stages: list[list[Domain]] = Field(default_factory=list, max_length=3)
    early_exit_conditions: list[Literal["ON_REQUIRED_DEPENDENCY_FAILURE", "ON_DOMAIN_ERROR"]] = Field(default_factory=list)
    concrete_plan_requested: bool = False
    rationale: str = ""
