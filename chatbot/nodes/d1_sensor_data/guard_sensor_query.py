"""LangGraph node that authorizes KQL before ADX execution.

Question intent, measurement selection and empty rollups are not security
decisions and must not override an approved authorization result here.

The policy implementation lives in :mod:`chatbot.policies.kql_safety`.  The
imports below deliberately re-export the former module-level constants and
private helpers so existing imports keep working.
"""

from __future__ import annotations

import re
import time

from chatbot.graph.state import AgentState
from chatbot.config import get_settings, normalize_kql_generate_method
from chatbot.policies.kql_safety import inspect_query_safety, repair_query_deterministically
from chatbot.policies.semantic_resources import WITHINGS_RESOURCE_IDS
from chatbot.nodes.common.support import debug, elapsed_ms, format_kql_for_log


def bind_authenticated_user(kql: str, user_id: str) -> tuple[str, list[str]]:
    """Bind user references to the authenticated account; never widens scope.

    Two common model slips are repaired without an LLM call: an unquoted
    numeric account literal as a Sensor function's first argument, and a
    TargetUserId reference without its let binding. Both bind to user_id only,
    so the authorization check below still decides execution.
    """
    from chatbot.policies.kql_safety import (
        _active_contract_functions, _let_expressions, _mask_kql_literals_and_comments,
    )
    repairs = []
    if not kql or not user_id:
        return kql, repairs
    if re.fullmatch(r"[1-9][0-9]*", user_id):
        names = "|".join(re.escape(name) for name in _active_contract_functions())
        pattern = re.compile(rf"\b({names})\s*\(\s*{re.escape(user_id)}\s*,")
        quoted, count = pattern.subn(lambda m: f"{m.group(1)}('{user_id}',", kql)
        if count:
            kql = quoted
            repairs.append("quoted the numeric authenticated user id in Sensor calls")
    code = _mask_kql_literals_and_comments(kql)
    if re.search(r"\bTargetUserId\b", code) and not _let_expressions(kql, "TargetUserId"):
        literal = "'" + user_id.replace("'", "''") + "'"
        kql = f"let TargetUserId = {literal};\n{kql}"
        repairs.append("declared TargetUserId as the authenticated user")
    return kql, repairs


def guard_sensor_query(state: AgentState) -> dict:
    debug("\n[Node] guard_sensor_query")
    started = time.perf_counter()
    original_kql = str(state.get("kql_query") or "")
    kql, deterministic_repairs = repair_query_deterministically(original_kql)
    user_id = str(state.get("user_id") or "").strip()
    kql, binding_repairs = bind_authenticated_user(kql, user_id)
    deterministic_repairs = [*deterministic_repairs, *binding_repairs]
    attempts = int(state.get("repair_attempts") or 0)
    settings = get_settings()
    configured_limit = max(0, int(settings.max_kql_repair_attempts))
    state_limit = state.get("max_repair_attempts")
    max_attempts = configured_limit if state_limit is None else max(0, int(state_limit))

    for repair in deterministic_repairs:
        debug(f"  [guard_sensor_query] deterministic_repair: {repair}")

    raw_trace = state.get("kql_generation_trace")
    trace = dict(raw_trace) if isinstance(raw_trace, dict) else {}
    traced_method = trace.get("method")
    method = normalize_kql_generate_method(
        settings.kql_generate_method if traced_method is None else traced_method
    )
    plan = state.get("plan") if isinstance(state.get("plan"), dict) else {}
    resources = plan.get("resources_priority")
    explicit_vendor_request = bool(
        isinstance(resources, list)
        and any(str(item) in WITHINGS_RESOURCE_IDS for item in resources)
    )
    allow_legacy_sources = bool(
        method == "deterministic"
        and str(state.get("kql_source") or "").startswith("deterministic")
        and explicit_vendor_request
    )
    decision = inspect_query_safety(
        kql,
        user_id,
        allow_legacy_sources=allow_legacy_sources,
    )
    if decision["is_safe"]:
        status = "execute"
        error = None
        error_type = None
    elif decision["should_repair"] and attempts < max_attempts:
        status = "repair"
        error = str(decision["reason"])
        error_type = "kql_guard"
    else:
        status = "error"
        error = str(decision["reason"])
        error_type = "kql_guard"

    debug(f"  guard_status={status}")
    debug(f"  guard_decision={decision}")
    debug("  guarded_kql=")
    debug(format_kql_for_log(kql))
    debug(f"  latency_ms={elapsed_ms(started)}")

    trace["method"] = method
    trace.setdefault("pre_guard_kql", original_kql or None)
    trace["guard_pass"] = status == "execute"
    trace["repair_attempted"] = bool(trace.get("repair_attempted")) or bool(
        deterministic_repairs
    )
    trace["final_kql"] = kql if status == "execute" else None

    return {
        "kql_query": kql,
        "kql_guard": decision,
        "guard_status": status,
        "execution_status": None,
        "error": error,
        "adx_error": None,
        "error_type": error_type,
        "kql_issues": decision.get("issues", []),
        "failed_kql": kql if status != "execute" else None,
        "kql_generation_trace": trace,
    }

__all__ = ["guard_sensor_query"]
