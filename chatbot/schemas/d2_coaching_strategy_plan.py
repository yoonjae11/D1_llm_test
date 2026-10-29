"""Advisor instructions only: no evaluator scores or reference labels."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrategyModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MIDelivery(StrategyModel):
    use_open_question: bool = False
    use_reflection: bool = True
    use_affirmation: bool = False
    seek_collaboration: bool = True
    emphasize_autonomy: bool = True
    provide_advice_with_permission: bool = False
    avoid_confrontation: bool = True
    avoid_controlling_language: bool = True
    avoid_premature_problem_solving: bool = True


class SDTSupport(StrategyModel):
    autonomy: bool = True
    competence: bool = True
    relatedness: bool = True


class EvidenceRef(StrategyModel):
    """Reference to an allowlisted current-turn or declared-dependency fact."""

    source_domain: Literal["USER", "D1", "D3", "D4"]
    evidence_id: str = Field(min_length=1)
    source_turn_ids: list[str] = Field(default_factory=list)
    summary: str = ""


COMBComponent = Literal[
    "PHYSICAL_CAPABILITY", "PSYCHOLOGICAL_CAPABILITY", "PHYSICAL_OPPORTUNITY",
    "SOCIAL_OPPORTUNITY", "REFLECTIVE_MOTIVATION", "AUTOMATIC_MOTIVATION",
]


class D2CoachingStrategyPlan(StrategyModel):
    coaching_applicable: bool
    interaction_depth: Literal["INFORMATION_ONLY", "VERY_BRIEF", "BRIEF_COACHING", "COACHING_CONVERSATION"]
    dialogue_objective: Literal[
        "NONE", "EXPLORE_GOAL", "EXPLORE_BARRIER", "RESOLVE_AMBIVALENCE",
        "SUPPORT_CONFIDENCE", "COLLABORATIVE_PLANNING", "REVIEW_PROGRESS",
        "PROVIDE_INFORMATION_WITH_PERMISSION",
    ]
    comb_status: Literal["NOT_APPLICABLE", "IDENTIFIED", "MULTIPLE", "UNKNOWN"]
    comb_components: list[COMBComponent] = Field(default_factory=list)
    comb_evidence_summary: str = ""
    comb_evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    barrier_relevant: bool = False
    barrier_unresolved: bool = False
    planning_uncertainty_acknowledged: bool = False
    selected_bcts: list[str] = Field(default_factory=list)
    bct_rationale: dict[str, str] = Field(default_factory=dict)
    bct_evidence_refs: dict[str, list[EvidenceRef]] = Field(default_factory=dict)
    mi_delivery: MIDelivery = Field(default_factory=MIDelivery)
    sdt_support: SDTSupport = Field(default_factory=SDTSupport)
    strategy_rationale: str
    response_constraints: list[str] = Field(default_factory=list)
