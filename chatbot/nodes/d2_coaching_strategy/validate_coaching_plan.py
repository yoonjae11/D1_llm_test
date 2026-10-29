"""Deterministic consistency checks, never an evaluator/model call."""

from chatbot.resources.coaching.bct_catalog import BCTCatalog
from chatbot.schemas.d2_coaching_strategy_plan import D2CoachingStrategyPlan, MIDelivery, SDTSupport


D2_VALIDATION_POLICY_IDS = tuple(f"D2-V{index:02d}" for index in range(1, 11))


def validate_coaching_strategy_plan(
    plan: D2CoachingStrategyPlan, catalogue: BCTCatalog, *, concrete_plan_requested: bool = False,
    allowed_evidence_ids: set[str] | None = None, d1_success: bool = False,
    d3_safety_level: str = "ROUTINE", d3_knowledge_status: str = "",
) -> tuple[D2CoachingStrategyPlan, list[str]]:
    original = plan.model_dump()
    plan = plan.model_copy(deep=True)
    if not plan.coaching_applicable:
        plan.interaction_depth = "INFORMATION_ONLY"
        plan.dialogue_objective = "NONE"
        plan.comb_status = "NOT_APPLICABLE"
        plan.comb_components = []
        plan.comb_evidence_summary = ""
        plan.comb_evidence_refs = []
        plan.barrier_relevant = plan.barrier_unresolved = False
        plan.planning_uncertainty_acknowledged = False
        plan.selected_bcts = []
        plan.bct_evidence_refs = {}
        plan.mi_delivery = MIDelivery(use_reflection=False, seek_collaboration=False)
    else:
        if plan.dialogue_objective == "NONE":
            plan.dialogue_objective = "EXPLORE_GOAL"
        if plan.interaction_depth == "INFORMATION_ONLY":
            plan.interaction_depth = "VERY_BRIEF"
        plan.comb_components = list(dict.fromkeys(plan.comb_components))
        # NOT_APPLICABLE means barrier formulation is unnecessary. UNKNOWN is
        # reserved for a relevant barrier whose COM-B component is unavailable.
        if not plan.barrier_relevant:
            plan.comb_status = "NOT_APPLICABLE"
            plan.comb_components = []
            plan.comb_evidence_summary = ""
            plan.comb_evidence_refs = []
            plan.barrier_unresolved = False
            plan.planning_uncertainty_acknowledged = False
            if plan.dialogue_objective == "EXPLORE_BARRIER":
                plan.dialogue_objective = "EXPLORE_GOAL"
        elif plan.comb_status in {"IDENTIFIED", "MULTIPLE"}:
            if (
                not plan.comb_components
                or not plan.comb_evidence_summary.strip()
                or not plan.comb_evidence_refs
            ):
                plan.comb_status = "UNKNOWN"
                plan.comb_components = []
                plan.comb_evidence_summary = ""
                plan.comb_evidence_refs = []
                plan.barrier_unresolved = True
            else:
                plan.comb_status = (
                    "MULTIPLE" if len(plan.comb_components) > 1 else "IDENTIFIED"
                )
        else:
            plan.comb_status = "UNKNOWN"
            plan.comb_components = []
            plan.comb_evidence_summary = ""
            plan.comb_evidence_refs = []
            plan.barrier_unresolved = True
        unresolved = plan.barrier_relevant and plan.barrier_unresolved
        tentative_requested = concrete_plan_requested and plan.planning_uncertainty_acknowledged
        if plan.dialogue_objective == "COLLABORATIVE_PLANNING" and unresolved and not tentative_requested:
            plan.dialogue_objective = "EXPLORE_BARRIER"
            plan.selected_bcts = []
        if plan.dialogue_objective in {"EXPLORE_GOAL", "EXPLORE_BARRIER", "RESOLVE_AMBIVALENCE"}:
            plan.mi_delivery.use_open_question = True
        if plan.dialogue_objective == "SUPPORT_CONFIDENCE":
            plan.mi_delivery.use_affirmation = True
        if plan.dialogue_objective == "PROVIDE_INFORMATION_WITH_PERMISSION":
            plan.mi_delivery.provide_advice_with_permission = True
        plan.mi_delivery.seek_collaboration = True

    plan.selected_bcts = list(dict.fromkeys(
        item for item in plan.selected_bcts
        if item in catalogue.supported_ids and plan.bct_rationale.get(item, "").strip()
    ))
    plan.bct_rationale = {item: plan.bct_rationale[item] for item in plan.selected_bcts}
    allowed = allowed_evidence_ids or {"USER_CURRENT_TURN"}
    def valid_ref(ref):
        return ref.evidence_id in allowed
    plan.comb_evidence_refs = [ref for ref in plan.comb_evidence_refs if valid_ref(ref)]
    plan.bct_evidence_refs = {
        bct_id: [ref for ref in plan.bct_evidence_refs.get(bct_id, []) if valid_ref(ref)]
        for bct_id in plan.selected_bcts
    }
    # Sensor-personalized techniques require successful declared D1 evidence.
    if not d1_success:
        for bct_id, refs in list(plan.bct_evidence_refs.items()):
            plan.bct_evidence_refs[bct_id] = [ref for ref in refs if ref.source_domain != "D1"]
    if (
        d3_safety_level in {"URGENT", "EMERGENCY"}
        or d3_knowledge_status == "INSUFFICIENT_EVIDENCE"
    ):
        plan.coaching_applicable = False
        plan.dialogue_objective = "NONE"
        plan.interaction_depth = "INFORMATION_ONLY"
        plan.comb_status = "NOT_APPLICABLE"
        plan.comb_components = []
        plan.comb_evidence_summary = ""
        plan.comb_evidence_refs = []
        plan.barrier_relevant = False
        plan.barrier_unresolved = False
        plan.planning_uncertainty_acknowledged = False
        plan.selected_bcts = []
        plan.bct_rationale = {}
        plan.bct_evidence_refs = {}
    plan.mi_delivery.emphasize_autonomy = True
    plan.mi_delivery.avoid_confrontation = True
    plan.mi_delivery.avoid_controlling_language = True
    plan.mi_delivery.avoid_premature_problem_solving = True
    plan.sdt_support = SDTSupport()

    # Model-written free text cannot be reliably contradiction-checked. Rebuild
    # executable constraints from typed fields, preserving the rationale separately.
    constraints = [
        "Answer the user's requested point first; do not invent sensor facts or barriers.",
        "Preserve choice; use respectful language without coercion or shame.",
        "Cite only allowed evidence IDs; never invent a source, turn, measurement, baseline, or target.",
    ]
    if not plan.coaching_applicable:
        constraints.append("Provide information only; do not add goals, BCTs, plans or unsolicited coaching questions.")
    else:
        constraints.extend([
            f"Follow dialogue objective {plan.dialogue_objective} and depth {plan.interaction_depth}.",
            "Support autonomy, competence and relatedness; make suggestions collaborative and achievable.",
        ])
        if plan.dialogue_objective in {"EXPLORE_GOAL", "EXPLORE_BARRIER", "RESOLVE_AMBIVALENCE"}:
            constraints.append("Reflect and ask at most one relevant open question; understand before proposing a plan.")
        if plan.dialogue_objective == "SUPPORT_CONFIDENCE":
            constraints.append("Acknowledge difficulty and the user's strengths; avoid shaming or rigid targets.")
        if plan.dialogue_objective == "COLLABORATIVE_PLANNING":
            constraints.append("Offer tentative achievable options and agree a next step with the user.")
            if plan.barrier_unresolved:
                constraints.append("Explicitly acknowledge the unresolved barrier; do not present tentative options as personalized certainty.")
        if plan.mi_delivery.provide_advice_with_permission:
            constraints.append("Seek permission before introducing unsolicited advice.")
    plan.response_constraints = constraints
    repairs = [key for key, value in plan.model_dump().items() if value != original[key]]
    return plan, repairs


def validate_coaching_plan(state: dict) -> dict:
    """LangGraph node adapter for the existing deterministic policy function."""
    plan, repairs = validate_coaching_strategy_plan(
        D2CoachingStrategyPlan.model_validate(state["coaching_plan"]),
        BCTCatalog.model_validate(state["bct_catalogue"]),
        concrete_plan_requested=state["coaching_context"]["concrete_plan_requested"],
        allowed_evidence_ids=set(state["coaching_context"].get("allowed_evidence_ids") or []),
        d1_success=(state["coaching_context"].get("dependency_results", {}).get("D1") or {}).get("status") == "success",
        d3_safety_level=((state["coaching_context"].get("health_safety") or {}).get("safety_level") or "ROUTINE"),
        d3_knowledge_status=((state["coaching_context"].get("health_safety") or {}).get("status") or ""),
    )
    return {"coaching_plan": plan.model_dump(), "validation_repairs": repairs}
