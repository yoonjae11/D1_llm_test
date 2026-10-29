"""LangGraph node for one bounded, re-guarded KQL repair attempt.

Unsafe queries are never executed.  When the deterministic guard identifies a
repairable candidate, this node performs one configured LLM repair attempt and
returns the result to the guard for a fresh safety decision.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Mapping

from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate

from chatbot.config import get_settings, normalize_kql_generate_method
from chatbot.graph.state import AgentState
from chatbot.nodes.common.support import clean_model_text, debug, latest_question, llm
from chatbot.policies.semantic_resources import prompt_resource_values as _resource_prompt_values
from chatbot.prompts import load_role_prompt, load_prompt


ADX_SCHEMA_CONTEXT = load_prompt("d1_sensor_data/adx_schema_context.txt")
_OUTPUT_SCHEMA_CACHE: dict[tuple[str, str, str], list[dict]] = {}


def _repair_schema_observations(user_id: str, kql: str, plan: dict | None) -> dict:
    """Read only column/type metadata for the approved functions in the query."""
    from chatbot.policies.kql_safety import _active_contract_functions
    from chatbot.policies.reactive_query_scope import reactive_query_bounds
    from chatbot.services.adx import default_adx_service

    try:
        start, end = reactive_query_bounds(plan or {})
    except Exception:
        return {}
    columns = {}
    for name in sorted(_active_contract_functions()):
        if not re.search(rf"\b{re.escape(name)}\s*\(", kql, re.I):
            continue
        cache_key = (os.environ.get("ADX_QUERY_URI") or os.environ.get("ADX_CLUSTER_URI", ""),
                     os.environ.get("ADX_DATABASE", ""), name)
        rows = _OUTPUT_SCHEMA_CACHE.get(cache_key)
        if rows is None:
            account = user_id.replace("'", "''")
            query = f"{name}('{account}', datetime({start}), datetime({end})) | getschema"
            try:
                raw = default_adx_service().execute_query(query, suppress_error_log=True)
                raw = json.loads(raw) if isinstance(raw, str) else raw
                rows = [{"column": str(row["ColumnName"]), "type": str(row.get("ColumnType", ""))}
                        for row in raw if isinstance(row, dict) and row.get("ColumnName")]
            except (TypeError, ValueError, KeyError):
                continue
            if rows:
                _OUTPUT_SCHEMA_CACHE[cache_key] = rows
        if rows:
            columns[name] = rows
    return {"function_output_columns": columns} if columns else {}


def _aggregate_scope_hints(kql: str) -> list[str]:
    """Diagnose aggregate calls in scalar operators; never rewrite the query."""
    from chatbot.policies.kql_safety import _split_top_level, _mask_kql_literals_and_comments

    hints = []
    for segment in _split_top_level(kql, "|"):
        code = _mask_kql_literals_and_comments(segment)
        # A scalar wrapper alone is not an aggregation context. Previously
        # any toscalar in an operator suppressed all aggregate diagnostics,
        # including the reproduced invalid toscalar(count()) candidate.
        wrapped = sorted(set(re.findall(
            r"\b(?:toscalar|totable)\s*\(\s*"
            r"(count|countif|dcount|sum|sumif|avg|min|max|stdev|stdevp|percentile)\s*\(",
            code, re.I,
        )))
        if wrapped:
            hints.append("scalar wrapper directly contains aggregate calls without a tabular "
                         f"aggregation context: {', '.join(wrapped)}")
        operator = re.match(r"(?i)^\s*(project|extend)\s+", code)
        if not operator or re.search(r"\b(?:toscalar|totable)\s*\(", code, re.I):
            continue
        calls = sorted(set(re.findall(
            r"\b(count|countif|dcount|sum|sumif|avg|min|max|stdev|stdevp|percentile)\s*\(",
            code, re.I,
        )))
        if calls:
            hints.append(f"{operator.group(1)} contains aggregate calls: {', '.join(calls)}")
    return hints


def _expression_hints(kql: str) -> list[str]:
    """Additional standard KQL diagnostics; never authorize or rewrite a query."""
    from chatbot.policies.kql_safety import (
        _all_tabular_function_arguments, _mask_kql_literals_and_comments, _split_top_level,
    )

    hints = []
    for name, expected in (("iff", 3), ("iif", 3), ("count", 0), ("countif", 1)):
        for args in _all_tabular_function_arguments(kql, name):
            if len(args) != expected:
                description = ("condition, true value, false value" if expected == 3
                               else "no arguments" if expected == 0 else "one predicate")
                hints.append(f"{name} has {len(args)} arguments; standard KQL expects "
                             f"{expected}: {description}. Preserve the requested "
                             "measurement and availability semantics.")
    code = _mask_kql_literals_and_comments(kql)
    if re.search(r"\brow_count\s*\(", code, re.I):
        hints.append("row_count is not a standard KQL scalar function. Compute the "
                     "intended relation's count in its summarize and retain that "
                     "alias, rather than replacing it with a bare count() in project.")
    for segment in _split_top_level(kql, "|"):
        masked = _mask_kql_literals_and_comments(segment)
        if not re.match(r"\s*join\b", masked, re.I):
            continue
        on = list(re.finditer(r"\bon\s+", masked, re.I))
        if not on:
            continue
        keys = masked[on[-1].end():].split(";", 1)[0].strip()
        if re.search(r"\b[A-Za-z_]\w*\s*\(", keys):
            hints.append("join on contains a computed expression. Derive the intended "
                         "key as a column on each input before joining, then reference "
                         "only those retained key columns in on.")
        elif re.fullmatch(r"[A-Za-z_]\w*\s*==\s*[A-Za-z_]\w*", keys):
            hints.append("join equality uses unqualified columns. Different input "
                         "key names require on $left.LeftKey == $right.RightKey; "
                         "identically named keys use on Key.")
    return hints


def repair_attempt_limit(state: Mapping[str, Any]) -> int:
    """Share a bounded repair budget and preserve the ADX diagnostic on each attempt."""
    configured = max(0, int(get_settings().max_kql_repair_attempts))
    state_limit = state.get("max_repair_attempts")
    requested = configured if state_limit is None else max(0, int(state_limit))
    return min(1, configured, requested)


def repair_query_with_llm(
    user_id: str,
    question: str,
    kql: str,
    error_hint: str,
    plan: dict | None = None,
    schema_observations: dict | None = None,
) -> str:
    """Return a repair candidate; the graph always sends it back to guard_sensor_query."""

    # A compiler reports only its first error. Supply other visible aggregate
    # scope errors too, so a repair can address the whole broken pipeline.
    aggregate_hints = [*_aggregate_scope_hints(kql), *_expression_hints(kql)]
    if aggregate_hints:
        error_hint += ("\nAdditional compiler-scope diagnostics: " + "; ".join(aggregate_hints)
                       + ". Aggregate calls belong in summarize; downstream project/extend "
                         "must refer to the retained aggregate aliases.")
    observations = dict(schema_observations or {})
    try:
        observations.update(_repair_schema_observations(user_id, kql, plan))
    except Exception as exc:
        debug(f"  -> Repair schema metadata unavailable: {type(exc).__name__}")

    if "resolve" in error_hint.lower():
        from chatbot.policies.kql_errors import extend_dependency_hints
        dependencies = extend_dependency_hints(kql)
        if dependencies:
            error_hint += ("\nPossible sibling-alias dependencies throughout the broken query: "
                + "; ".join(dependencies)
                + ". Inspect ALL these extend/project operators, not only the first reported name. "
                  "A newly introduced alias must be defined in an earlier pipeline operator "
                  "before a dependent expression. Preserve any genuinely existing input-column "
                  "reference. This diagnostic does not authorize changing measurements, "
                  "selection, thresholds, units, or bounds.")

    resources = _resource_prompt_values()
    from chatbot.policies.reactive_query_scope import query_time_scope
    model_plan = {key: value for key, value in (plan or {}).items()
                  if key not in {"tables_priority", "resources_priority"}}
    if model_plan.get("time_range"):
        model_plan["query_time_scope"] = query_time_scope(plan)
    system, user = load_role_prompt("d1_sensor_data/repair_sensor_query.txt")
    prompt = ChatPromptTemplate.from_messages([("system", system), ("human", user)])
    chain = prompt | llm | StrOutputParser()
    raw = chain.invoke(
        {
            "schema": ADX_SCHEMA_CONTEXT.format(user_id=user_id),
            "user_id": user_id,
            "question": question,
            "plan_json": json.dumps(model_plan, ensure_ascii=False),
            "kql": kql,
            "error_hint": error_hint,
            "schema_observations": json.dumps(observations or {"status": "unavailable"},
                                              ensure_ascii=False, default=str),
            **resources,
        }
    )
    # Do not string-replace legacy table names with function names: doing so
    # creates an invalid bare function reference and can silently preserve the
    # wrong schema. The repaired candidate must independently use complete
    # canonical calls and pass the shared guard.
    return clean_model_text(raw)


def repair_sensor_query(state: AgentState) -> dict:
    settings = get_settings()
    raw_trace = state.get("kql_generation_trace")
    trace = dict(raw_trace) if isinstance(raw_trace, dict) else {}
    traced_method = trace.get("method")
    method = normalize_kql_generate_method(
        settings.kql_generate_method if traced_method is None else traced_method
    )
    trace["method"] = method
    trace.setdefault("pre_guard_kql", clean_model_text(state.get("kql_query") or "") or None)
    trace["guard_pass"] = False
    trace.setdefault("repair_attempted", False)
    trace["final_kql"] = None

    if method != "llm":
        # Deterministic generation is a model-free contract. A guard rejection
        # must not silently turn into an LLM repair call.
        original = clean_model_text(state.get("kql_query") or "")
        return {
            "guard_status": "error",
            "execution_status": "blocked",
            "error": "Deterministic KQL cannot be repaired by a language model.",
            "error_type": "kql_guard",
            "failed_kql": original,
            "kql_generation_trace": trace,
        }

    attempts = int(state.get("repair_attempts") or 0)
    max_attempts = repair_attempt_limit(state)
    original = clean_model_text(state.get("kql_query") or "")

    if attempts >= max_attempts:
        return {
            "guard_status": "error",
            "execution_status": "blocked",
            "error": str(state.get("error") or "KQL repair limit reached."),
            "error_type": "kql_guard",
            "failed_kql": original,
            "kql_generation_trace": trace,
        }

    next_attempt = attempts + 1
    trace["repair_attempted"] = True
    error_hint = str(state.get("error") or "The query did not pass the read-only guard.")
    repair_history = list(trace.get("repair_history") or [])
    if (next_attempt > 1 and state.get("guard_status") == "error" and repair_history
            and repair_history[-1].get("origin") == "adx_validation"):
        # The previous query passed authorization but failed ADX compilation.
        # Repair that known query, not a malformed/unauthorized LLM rewrite.
        previous = repair_history[-1]
        original = str(previous.get("failed_kql") or original)
        error_hint = (str(previous.get("error") or "")
                      + "\nThe previous repair was rejected by the guard: " + error_hint)
    if repair_history:
        # Supply compiler lineage without duplicating entire earlier queries.
        # A later compile error can hide a still-unfixed cause from the first
        # attempt. These are diagnostics for the LLM, not rewrite templates.
        previous_diagnostics = []
        for entry in repair_history[-2:]:
            issue = str(entry.get("error") or "")
            if issue and issue not in previous_diagnostics:
                previous_diagnostics.append(issue)
        if previous_diagnostics:
            error_hint += "\nEarlier failed diagnostics: " + json.dumps(previous_diagnostics, ensure_ascii=False)
        if any(clean_model_text(entry.get("failed_kql") or "") == original for entry in repair_history):
            error_hint += ("\nThis exact candidate already failed. Address the diagnosed operator/column "
                           "scope rather than resubmitting it unchanged; preserve the original request.")
    repair_history.append({
        "attempt": next_attempt,
        "origin": "adx_validation" if state.get("execution_status") == "error" else "guard",
        "failed_kql": original,
        "error": error_hint,
    })
    trace["repair_history"] = repair_history
    if "datatable(" in original.lower() and "unsupported" in error_hint.lower():
        error_hint += " Remove the literal datatable source; use an aggregate over an approved Sensor function for the empty-result status."
    repair_error = None
    try:
        repaired = clean_model_text(
            repair_query_with_llm(
                str(state.get("user_id") or ""),
                latest_question(state),
                original,
                error_hint,
                state.get("plan") if isinstance(state.get("plan"), dict) else {},
                schema_observations=(trace.get("source_review") or {}).get("schema_observations"),
            )
        )
    except Exception as exc:
        debug(f"  -> KQL repair attempt {next_attempt} failed: {exc}")
        repair_error = f"KQL repair call failed ({type(exc).__name__}): {exc}"
        repaired = ""

    if not repaired:
        repair_error = repair_error or "KQL repair returned no query."
        repair_history[-1]["repair_error"] = repair_error

    return {
        # An empty repair must not re-execute the known-failing original query.
        # The graph reports this failure directly; all nonempty candidates are
        # still re-authorized by the mandatory guard.
        "kql_query": repaired,
        "repair_attempts": next_attempt,
        "max_repair_attempts": max_attempts,
        "guard_status": None if repaired else "error",
        "execution_status": None if repaired else "error",
        "error": None if repaired else f"{repair_error} Original validation error: {error_hint}",
        "error_type": None if repaired else "kql_repair",
        "adx_error": None,
        "query_result": "[]",
        "coverage_context": {},
        "failed_kql": original,
        "kql_generation_trace": trace,
    }

__all__ = ["repair_sensor_query", "repair_attempt_limit"]
