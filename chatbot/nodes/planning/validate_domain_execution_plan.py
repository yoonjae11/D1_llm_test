"""Validate a bounded DAG before any runner is called. No model calls."""

from pydantic import ValidationError

from chatbot.schemas.domain_execution_plan import DomainExecutionPlan, DomainRequirement

MAX_EXECUTION_STAGES = 3


def validate_domain_execution_plan(candidate: dict | DomainExecutionPlan) -> DomainExecutionPlan:
    # Re-parse model instances too: callers may have mutated their fields.
    plan = DomainExecutionPlan.model_validate(
        candidate.model_dump() if isinstance(candidate, DomainExecutionPlan) else candidate
    )
    plan.selected_domains = list(dict.fromkeys(plan.selected_domains))
    selected = set(plan.selected_domains)
    if set(plan.dependencies) - selected or set(plan.domain_requirements) - selected:
        raise ValueError("Requirements/dependency owners must be selected domains")
    for domain in plan.selected_domains:
        dependencies = list(dict.fromkeys(plan.dependencies.get(domain, [])))
        if set(dependencies) - selected:
            raise ValueError("Dependency targets must be selected domains")
        if domain in dependencies:
            raise ValueError("Self-dependency is forbidden")
        plan.dependencies[domain] = dependencies
        requirement = plan.domain_requirements.setdefault(domain, DomainRequirement())
        requirement.optional_dependencies = list(dict.fromkeys(requirement.optional_dependencies))
        if set(requirement.optional_dependencies) - set(dependencies):
            raise ValueError("Optional dependencies must be declared scheduling dependencies")

    # Kahn traversal checks cycles independently of model-provided stage order.
    remaining = list(plan.selected_domains)
    completed = set()
    derived = []
    while remaining:
        ready = [d for d in remaining if set(plan.dependencies[d]) <= completed]
        if not ready:
            raise ValueError("Dependency cycle")
        derived.append(ready)
        completed.update(ready)
        remaining = [d for d in remaining if d not in completed]
    stages = plan.execution_stages or derived
    if len(stages) > MAX_EXECUTION_STAGES:
        raise ValueError("Execution exceeds the three-stage maximum")
    flattened = [domain for stage in stages for domain in stage]
    if any(not stage for stage in stages):
        raise ValueError("Empty execution stage")
    if len(flattened) != len(set(flattened)):
        raise ValueError("A domain cannot execute in multiple stage positions")
    if set(flattened) != selected:
        raise ValueError("Stages must contain exactly the selected domains")
    positions = {domain: i for i, stage in enumerate(stages) for domain in stage}
    for target, sources in plan.dependencies.items():
        if any(positions[source] >= positions[target] for source in sources):
            raise ValueError("Every dependency must complete in an earlier stage")
    if plan.intent in {"capabilities", "alert_config", "unsupported", "clarification"} and selected:
        raise ValueError("Operational/clarification routes cannot execute domain work")
    if "D1" in selected:
        plan.intent = "data"
    plan.execution_stages = stages
    return plan


def safe_domain_execution_plan(candidate: dict | DomainExecutionPlan) -> tuple[DomainExecutionPlan, list[str]]:
    try:
        return validate_domain_execution_plan(candidate), []
    except (ValidationError, ValueError, TypeError) as exc:
        return DomainExecutionPlan(
            intent="clarification", rationale="Invalid execution plan; clarify without running domains"
        ), [type(exc).__name__]
