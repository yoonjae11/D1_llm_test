"""D2's single typed strategy model call; no graph wiring or result wrapping."""
from hashlib import sha256
import json

from chatbot.nodes.common import support
from chatbot.prompts import load_prompt
from chatbot.schemas.d2_coaching_strategy_plan import D2CoachingStrategyPlan

D2_ADVISOR_POLICY_RESOURCES = (
    "d2_coaching_strategy/plan_coaching_strategy.txt",
    "d2_coaching_strategy/advisor_policy/01_nhs_role_and_personalised_care.txt",
    "d2_coaching_strategy/advisor_policy/02_comb_framework.txt",
    "d2_coaching_strategy/advisor_policy/03_bct_framework.txt",
    "d2_coaching_strategy/advisor_policy/04_mi_framework.txt",
    "d2_coaching_strategy/advisor_policy/05_sdt_framework.txt",
    "d2_coaching_strategy/advisor_policy/06_interaction_depth_and_scope.txt",
)


def build_advisor_policy_bundle(loader=load_prompt) -> str:
    """Load the source-specific Advisor policy in one stable order."""

    return "\n\n".join(
        loader(resource).strip() for resource in D2_ADVISOR_POLICY_RESOURCES
    )


ADVISOR_COACHING_POLICY = build_advisor_policy_bundle()
D2_ADVISOR_POLICY_SHA256 = sha256(
    ADVISOR_COACHING_POLICY.encode("utf-8")
).hexdigest()
D2_PROMPT_HASHES = {
    resource: sha256(load_prompt(resource).encode("utf-8")).hexdigest()
    for resource in D2_ADVISOR_POLICY_RESOURCES
}


def coaching_strategy_messages(context: dict, catalogue: dict) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": ADVISOR_COACHING_POLICY + "\nOutput JSON schema:\n"
         + json.dumps(D2CoachingStrategyPlan.model_json_schema())},
        {"role": "user", "content": json.dumps(
            {"context": context, "bct_catalogue": catalogue}, ensure_ascii=False, default=str
        )},
    ]


def _fallback_strategy():
    return D2CoachingStrategyPlan(
        coaching_applicable=False,
        interaction_depth="INFORMATION_ONLY",
        dialogue_objective="NONE",
        comb_status="NOT_APPLICABLE",
        strategy_rationale="Strategy generation was unavailable.",
    )


def plan_coaching_strategy(state: dict) -> dict:
    if state["coaching_status"] == "error":
        return {"coaching_plan": _fallback_strategy().model_dump()}
    try:
        raw = support.call_chat_completion(
            coaching_strategy_messages(state["coaching_context"], state["bct_catalogue"]), temperature=0
        )
        plan = D2CoachingStrategyPlan.model_validate_json(support.clean_model_text(raw))
        return {"coaching_plan": plan.model_dump(), "llm_calls": 1}
    except Exception as exc:
        support.logger.warning("D2 strategy unavailable: %s", type(exc).__name__)
        return {
            "coaching_plan": _fallback_strategy().model_dump(), "llm_calls": 1,
            "coaching_status": "error", "failure_type": type(exc).__name__,
        }


__all__ = [
    "ADVISOR_COACHING_POLICY",
    "D2_ADVISOR_POLICY_RESOURCES",
    "D2_ADVISOR_POLICY_SHA256",
    "D2_PROMPT_HASHES",
    "build_advisor_policy_bundle",
    "coaching_strategy_messages",
    "plan_coaching_strategy",
]
