"""One model decision for routing and domain dependencies."""

import json
import re
import time

from chatbot.nodes.common import support
from chatbot.nodes.common.support import conversation_context
from chatbot.nodes.planning.validate_domain_execution_plan import safe_domain_execution_plan
from chatbot.prompts import load_prompt
from chatbot.schemas.domain_execution_plan import DomainExecutionPlan

DOMAIN_EXECUTION_POLICY = load_prompt("planning/plan_domain_execution.txt")
GREETING_TOKENS = {"hi", "hello", "hey", "good morning", "good afternoon", "good evening"}
THANKS_TOKENS = {"thanks", "thank you", "thx"}


def _fast_intent(question: str) -> str | None:
    """Return only deterministic exact greeting/thanks decisions."""
    normalized = re.sub(r"\s+", " ", str(question or "").strip().lower())
    if not normalized or normalized in GREETING_TOKENS or normalized in THANKS_TOKENS:
        return "chat"
    return None


def _route_name(intent: str) -> str:
    return {
        "data": "personal_data_supported",
        "unsupported": "personal_data_unavailable",
        "clarification": "clarification_required",
        "capabilities": "general_chat",
        "chat": "general_chat",
        "alert_config": "alert_config",
    }.get(intent, "general_chat")


def plan_domain_execution(state: dict) -> dict:
    started = time.perf_counter()
    context = conversation_context(state)
    fast = _fast_intent(context["question"])
    status = "success"
    issues = []
    llm_calls = 0
    review_attempted = False
    if fast:
        plan = DomainExecutionPlan(intent=fast, rationale="Exact greeting/thanks or empty input")
    else:
        last_plan = state.get("last_plan") or {}
        context["last_data_plan"] = {
            key: last_plan[key] for key in ("metrics", "time_range", "case_type", "query_family")
            if key in last_plan
        }
        # Operational pending state supplies meaning to short configuration replies.
        pending = state.get("pending_clarification") or {}
        context["pending_clarification"] = {
            key: pending[key] for key in ("kind", "stage", "question", "metric") if key in pending
        }
        try:
            messages = [
                {"role": "system", "content": DOMAIN_EXECUTION_POLICY + "\nOutput JSON schema:\n"
                 + json.dumps(DomainExecutionPlan.model_json_schema())},
                {"role": "user", "content": json.dumps(context, ensure_ascii=False, default=str)},
            ]
            llm_calls += 1
            raw = support.call_chat_completion(messages, temperature=0)
            plan, issues = safe_domain_execution_plan(json.loads(support.clean_model_text(raw)))
            if issues or plan.intent == "clarification" or (plan.intent == "data" and not plan.selected_domains):
                review_attempted = True
                # The model, not a metric-keyword router, decides whether the
                # question is actually ambiguous. A valid clarification stays valid.
                llm_calls += 1
                reviewed = support.call_chat_completion([
                    *messages,
                    {"role": "assistant", "content": support.clean_model_text(raw)},
                    {"role": "user", "content": (
                        "Review this proposed routing against the original request. "
                        "If its measurement and operation are identifiable, route to the "
                        "needed domain even if wording is awkward or availability unknown. "
                        "Keep clarification when essential meaning really is missing. "
                        "Return the complete schema-valid JSON plan. Validation issues: "
                        + json.dumps(issues)
                    )},
                ], temperature=0)
                plan, issues = safe_domain_execution_plan(json.loads(support.clean_model_text(reviewed)))
            if issues:
                status = "error"
        except Exception as exc:
            support.logger.warning("domain planning failed: %s", type(exc).__name__)
            status = "error"
            issues = [type(exc).__name__]
            plan = DomainExecutionPlan(intent="clarification", rationale="Planning unavailable; clarify safely")
    return {
        "intent": plan.intent,
        "route": _route_name(plan.intent),
        "domain_execution_plan": plan.model_dump(),
        "orchestration_trace": {
            "planner_status": status,
            "planner_llm_calls": llm_calls,
            "review_attempted": review_attempted,
            "planner_latency_ms": support.elapsed_ms(started),
            "execution_stages": plan.execution_stages,
            "validation_issues": issues,
        },
    }
