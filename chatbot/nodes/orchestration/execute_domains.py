"""Registry-based, bounded DAG execution with generic dependency handling."""
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from copy import deepcopy
import time
from typing import Callable

from chatbot.nodes.common import support
from chatbot.nodes.d1_sensor_data.workflow import run_d1
from chatbot.nodes.d2_coaching_strategy.workflow import run_d2
from chatbot.nodes.d3_health_knowledge.workflow import run_d3
from chatbot.nodes.d4_multiturn_context.workflow import run_d4
from chatbot.nodes.planning.plan_domain_execution import _route_name
from chatbot.nodes.planning.validate_domain_execution_plan import safe_domain_execution_plan
from chatbot.schemas.domain_execution_plan import DOMAIN_NAMES, Domain
from chatbot.schemas.domain_execution_result import DomainExecutionResult, DomainRunOutput

DOMAIN_RUNNERS: dict[Domain, Callable[[dict], DomainRunOutput]] = {
    "D1": run_d1, "D2": run_d2, "D3": run_d3, "D4": run_d4,
}
_RESERVED_UPDATES = {
    "messages", "user_id", "thread_id", "intent", "route", "domain_execution_plan",
    "dependency_results", "domain_results", "orchestration_trace", "coaching_strategy",
    "health_knowledge_result", "resolved_context",
}


def execute_domains(state: dict) -> dict:
    started = time.perf_counter()
    plan, issues = safe_domain_execution_plan(state.get("domain_execution_plan"))
    trace = dict(state.get("orchestration_trace") or {})
    trace.update({"execution_stages": plan.execution_stages, "executed_stages": []})
    results = {
        domain: DomainExecutionResult(domain=domain, selected=domain in plan.selected_domains, status="skipped").model_dump()
        for domain in DOMAIN_NAMES
    }
    if issues:
        trace.update({"planner_status": "error", "validation_issues": issues})
        return {
            "intent": "clarification", "route": _route_name("clarification"),
            "domain_execution_plan": plan.model_dump(), "domain_results": results,
            "orchestration_trace": trace,
        }

    updates = {}
    def execute(domain: Domain, snapshot: dict) -> DomainRunOutput:
        dependencies = {source: deepcopy(results[source]) for source in plan.dependencies[domain]}
        statuses = {source: value["status"] for source, value in dependencies.items()}
        optional = set(plan.domain_requirements[domain].optional_dependencies)
        failed = [source for source, status in statuses.items() if status != "success" and source not in optional]
        if failed:
            return DomainRunOutput(result=DomainExecutionResult(
                domain=domain, status="blocked_dependency",
                summary="A required dependency did not provide usable information",
                missing_information=[f"{source}: {statuses[source]}" for source in failed],
                provenance={"dependency_statuses": statuses, "blocked_by": failed, "runner_called": False},
            ))
        snapshot["dependency_results"] = dependencies
        # Completed but undeclared domain outputs must not become hidden inputs.
        snapshot["domain_results"] = {}
        try:
            raw = DOMAIN_RUNNERS[domain](snapshot)
            output = DomainRunOutput.model_validate(raw)
            if output.result.domain != domain or not output.result.selected:
                raise ValueError("Runner returned an inconsistent domain wrapper")
            if _RESERVED_UPDATES.intersection(output.state_updates):
                raise ValueError("Runner attempted to overwrite shared orchestration state")
        except Exception as exc:
            support.logger.warning("%s execution failed: %s", domain, type(exc).__name__)
            output = DomainRunOutput(result=DomainExecutionResult(
                domain=domain, status="error", summary="Domain execution failed",
                provenance={"failure_type": type(exc).__name__},
            ))
        output.result.provenance.update({"dependency_statuses": statuses, "runner_called": True})
        return output

    for stage in plan.execution_stages:
        snapshot = {**state, **updates, "domain_execution_plan": plan.model_dump()}
        if len(stage) == 1:
            completed = [execute(stage[0], deepcopy(snapshot))]
        else:
            with ThreadPoolExecutor(max_workers=len(stage), thread_name_prefix="sensei-domain") as pool:
                futures = [pool.submit(copy_context().run, execute, domain, deepcopy(snapshot)) for domain in stage]
                completed = [future.result() for future in futures]
        # Concurrent outputs cannot race to replace the same shared field.
        owners: dict[str, list[int]] = {}
        for index, output in enumerate(completed):
            for key in output.state_updates:
                owners.setdefault(key, []).append(index)
        conflicting = {index for indices in owners.values() if len(indices) > 1 for index in indices}
        for index, (domain, output) in enumerate(zip(stage, completed)):
            if index in conflicting:
                output = DomainRunOutput(result=DomainExecutionResult(
                    domain=domain, status="error", summary="Concurrent domain state updates conflict",
                    provenance={**output.result.provenance, "failure_type": "ConflictingStateUpdates"},
                ))
            results[domain] = output.result.model_dump()
            updates.update(output.state_updates)
        trace["executed_stages"].append(stage)
        statuses = {results[domain]["status"] for domain in stage}
        stop = (
            "ON_REQUIRED_DEPENDENCY_FAILURE" in plan.early_exit_conditions and "blocked_dependency" in statuses
        ) or ("ON_DOMAIN_ERROR" in plan.early_exit_conditions and "error" in statuses)
        if stop:
            trace["early_exit"] = True
            for result in results.values():
                if result["selected"] and result["status"] == "skipped":
                    result["provenance"] = {"reason": "execution_plan_early_exit", "runner_called": False}
            break
    trace["execution_latency_ms"] = support.elapsed_ms(started)
    return {**updates, "domain_results": results, "orchestration_trace": trace}
