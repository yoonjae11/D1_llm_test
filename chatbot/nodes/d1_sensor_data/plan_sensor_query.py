"""LangGraph node that builds a validated personal-data query plan."""

import datetime
import json
import os
import re
import time

from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate

from chatbot.graph.state import AgentState
from chatbot.prompts import load_role_prompt
from chatbot.nodes.common.support import (
    clean_model_text as _clean_model_text,
    legacy_debug as _debug,
    effective_history as _effective_history,
    elapsed_ms as _elapsed_ms,
    last_assistant_text as _last_assistant_text,
    latest_question as _latest_question,
    llm,
    logger,
    safe_json_loads as _safe_json_loads,
)
from chatbot.policies.plan_sanitization import sanitize_plan
from chatbot.policies.reactive_query_scope import reference_clock_local
from chatbot.policies.availability import metric_availability_keys
from chatbot.policies.domain_catalog import (
    METRIC_DEFINITIONS,
    PHONE_TO_WITHINGS_METRIC_PRIORITY,
)
from chatbot.policies.question_analysis import (
    _is_contextual_followup,
    _is_time_only_followup,
)
from chatbot.policies.time_ranges import (
    _parse_time_range_from_question,
    active_evaluation_time,
    resolve_all_available_time_range,
    resolve_evaluation_plan_time_range,
)


def _enrich_plan_from_state(plan: dict, state: AgentState) -> dict:
    timezone = str(state.get("timezone_iana") or "").strip()
    locale = str(state.get("locale") or "").strip()
    if timezone:
        plan["timezone_iana"] = timezone
    if locale:
        plan["locale"] = locale
    if state.get("request_clock_local"):
        # Runtime-owned; never accept a clock invented by the plan model.
        plan["reference_clock_local"] = state["request_clock_local"]
    return plan


def _parse_plan_object(raw: object) -> dict:
    """Accept a JSON plan surrounded by prose without inventing plan fields."""
    text = _clean_model_text(raw)
    parsed = _safe_json_loads(text, default=None)
    if isinstance(parsed, dict):
        return parsed
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", text):
        try:
            value, _ = decoder.raw_decode(text, match.start())
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and any(
            key in value for key in ("schema_version", "metrics", "computation", "time_range")
        ):
            return value
    return {}


def _format_user_available_metrics(availability: object) -> str:
    """Describe observed data to the semantic planner with canonical names."""

    if not isinstance(availability, dict):
        return "Availability is not yet known; use the full metric catalog."
    current_keys = {
        str(metric_type or "").upper()
        for metric_type, item in availability.items()
        if (
            item.get("is_available")
            if isinstance(item, dict)
            else getattr(item, "is_available", False)
        )
    }
    historical_keys = {
        str(metric_type or "").upper()
        for metric_type, item in availability.items()
        if any(
            item.get(field) if isinstance(item, dict) else getattr(item, field, None)
            for field in ("earliest_date_from_local", "latest_date_to_local")
        )
    }
    observed_keys = current_keys | historical_keys
    if not observed_keys:
        return "No cached current or historical metric evidence; use bounded schema discovery."

    canonical_metrics = []
    for metric in METRIC_DEFINITIONS:
        # Do not advertise both a source-neutral metric and its historical
        # phone_* compatibility alias to the model.
        if metric in PHONE_TO_WITHINGS_METRIC_PRIORITY:
            continue
        if metric_availability_keys(metric) & observed_keys:
            canonical_metrics.append(metric)

    canonical_text = ", ".join(canonical_metrics) or "none mapped"
    observed_text = ", ".join(sorted(observed_keys))
    return (
        f"Candidate canonical metrics from cached observed types: {canonical_text}. "
        f"Observed availability keys: {observed_text}. "
        f"Currently available keys: {', '.join(sorted(current_keys)) or 'none'}. "
        "Historical date evidence is not proof of current measurements. Cached metadata "
        "is a discovery hint, not complete coverage or proof that each optional workout "
        "attribute exists; the bounded query inventory establishes that separately."
    )


def _resolve_all_available_for_state(plan: dict, state: AgentState) -> dict:
    """Resolve a production all-history request from hydrated availability."""

    time_range = plan.get("time_range")
    if not isinstance(time_range, dict):
        return plan
    candidate = dict(plan)
    candidate["timezone_iana"] = str(state.get("timezone_iana") or "")
    availability = state.get("availability_context")
    if isinstance(availability, dict):
        # Private transient input: resolve_all_available_time_range consumes it,
        # and it is never retained in the sanitized plan or sent to a prompt.
        candidate["_availability_context"] = availability
    candidate["time_range"] = resolve_all_available_time_range(time_range, candidate)
    if isinstance(candidate.get("comparison_time_range"), dict):
        candidate["comparison_time_range"] = resolve_all_available_time_range(
            candidate["comparison_time_range"], candidate,
        )
    candidate.pop("_availability_context", None)
    return candidate


def _review_observed_measurement_scope(plan: dict, candidate: dict, state: dict,
                                       chain, prompt_values: dict, question: str,
                                       last_plan: dict, requested_time_range: dict | None):
    """One LLM review of cross-stream workout conditions using observed schema.

    Schema metadata informs interpretation; missing data never rewrites a metric
    in Python, selects a benchmark-specific path, or supplies an answer label.
    """
    computation = plan.get("computation") or {}
    resources = {METRIC_DEFINITIONS.get(metric, {}).get("resource")
                 for metric in plan.get("metrics", [])}
    trace = {"attempted": False}
    # Cached context must establish that schema discovery is relevant. Unknown
    # availability remains unknown; no extra external probe on an unhydrated turn.
    if (plan.get("schema_version") != 2 or plan.get("execution_ready") is False
            or not computation.get("conditions") or "workout.sessions" not in resources
            or len(resources - {None}) < 2 or not state.get("availability_context")):
        return plan, candidate, trace
    try:
        from chatbot.adx_client import adx_manager
        from chatbot.policies.reactive_query_scope import reactive_query_bounds
        from chatbot.services.sensor_inventory import observe_sensor_schema
        start, end = reactive_query_bounds(plan)
        observed = observe_sensor_schema(str(state.get("app_user_id") or state.get("user_id") or ""),
                                         start, end, adx_manager.execute_query,
                                         include_workout_payload=True)
        if observed.get("status") not in {"observed", "partial"}:
            return plan, candidate, {**trace, "reason": "schema_discovery_unavailable"}
        trace = {"attempted": True, "accepted": False, "initial_plan": plan,
                 "schema_observations": observed}
        # Scope depends on observation definitions, not observed answer values,
        # coverage counts or incidental first/last timestamps. Keep the detailed
        # discovery in the audit; give this semantic review a compact vocabulary.
        scope_schema = {
            "sample_streams": [{"Type": row.get("Type"), "Unit": row.get("Unit")}
                               for row in observed.get("observations", [])
                               if row.get("Type")],
            "resource_fields": [{"resource": row["resource"],
                                 "fields": [key[:-4] for key in row
                                            if key.endswith("Rows")]}
                                for row in observed.get("observations", [])
                                if row.get("resource") and not row.get("Type")],
            "workout_fields": [{key: field[key] for key in
                                ("path", "types", "observed_labels") if key in field}
                               for field in (observed.get("workout_payload") or {}).get("fields", [])],
        }
        reviewed_text = chain.invoke({**prompt_values, "validation_feedback": json.dumps({
            "review_kind": "observed_measurement_scope",
            "observed_schema": scope_schema,
            # The review receives a semantic patch interface, not the initial
            # candidate. Give it the declared date convention explicitly so it
            # cannot silently choose a different cross-stream matching key.
            "semantic_contract": {"sleep_date_basis": plan.get("sleep_date_basis")},
            "request": "Independently identify the ORIGINAL question's subject population, "
                       "output observation, condition observations, eligibility keys and reduction. "
                       "Rebuild the plan from the question without inheriting a prior interpretation. "
                       "Use observed field definitions to "
                       "check compatible measurements, not to choose whichever values are available. "
                       "Calendar grouping alone does not introduce a different measurement population. "
                       "Unqualified predicates on attributes of the request's session subject inherit "
                       "that session population; an explicit independent daily/resting/sample measurement "
                       "retains that stream even if unavailable. Make metric identifiers and stated "
                       "assumptions describe the SAME scope. Disclose a useful contextual interpretation "
                       "and clarify only essential ambiguity; do not invent finer observations. "
                       "Return the complete computation and scope assumptions as a semantic patch, "
                       "preserving requested thresholds, activities, selection, units and calculation. "
                       "Do not regenerate runtime-owned period or metric lists. Missing evidence is not zero. "
                       "The metadata supplies no answer values.",
        }, ensure_ascii=False, default=str)})
        reviewed = _safe_json_loads(_clean_model_text(reviewed_text), default={})
        if not isinstance(reviewed, dict) or not reviewed:
            return plan, candidate, trace
        if "schema_version" not in reviewed:
            # The scope model need not regenerate runtime-owned dates/catalog
            # lists. Merge only its semantic patch, then use the same validator.
            if not isinstance(reviewed.get("computation"), dict):
                trace["review_errors"] = [{"code": "missing_review_computation"}]
                return plan, candidate, trace
            patch_fields = ("computation", "assumptions", "needs_followup", "followup_question")
            reviewed = {**candidate, **{key: reviewed[key] for key in patch_fields if key in reviewed}}
            for key in ("metrics", "resources_priority", "tables_priority", "granularity"):
                reviewed.pop(key, None)
            trace["review_output_kind"] = "scope_patch"
        # A revised dependency graph can change the applicable local table clock.
        # Keep the original semantic period, not an old resolved absolute window.
        if isinstance(requested_time_range, dict) and requested_time_range.get("mode") in {"relative", "all_available"}:
            reviewed["time_range"] = dict(requested_time_range)
        reviewed = _resolve_all_available_for_state(reviewed, state)
        checked = _enrich_plan_from_state(sanitize_plan(reviewed, question, last_plan), state)
        if checked.get("validation_errors") or checked.get("unsupported_metrics"):
            trace["review_errors"] = checked.get("validation_errors") or [{
                "code": "unsupported_review_metrics",
                "metrics": checked["unsupported_metrics"],
            }]
            return plan, candidate, trace
        trace["accepted"] = True
        return checked, reviewed, trace
    except Exception as exc:
        logger.warning("Sensor measurement-scope review unavailable: %s", type(exc).__name__)
        return plan, candidate, {**trace, "reason": "review_unavailable", "error_type": type(exc).__name__}


def _plan_node_result(plan: dict, last_plan: dict, raw_plan: dict) -> dict:
    """Return production state plus evaluation-only planner audit fields."""

    if active_evaluation_time() is not None and isinstance(plan.get("time_range"), dict):
        # The full graph must resolve the same caller-defined clock as the
        # standalone evaluator. Otherwise legacy v1 query bounds use now()
        # even though the planner and answer see a historical reference date.
        plan = dict(plan)
        plan["time_range"] = resolve_evaluation_plan_time_range(
            plan, str(plan.get("timezone_iana") or ""),
        )
        plan["reference_clock_local"] = reference_clock_local({
            key: value for key, value in plan.items() if key != "reference_clock_local"
        })
    if plan.get("needs_followup"):
        plan = {**plan, "execution_ready": False}
    result = {"plan": plan, "prev_plan": last_plan, "last_plan": plan,
              "sensor_plan_trace": {"attempted": False}}
    # Unsanitized model output is useful for offline evaluation diagnostics but
    # must not expand the production final-state/payload surface. Evaluation is
    # identified solely by the allowed time context, never by dataset or DB.
    if active_evaluation_time() is not None:
        result["raw_plan"] = raw_plan
        result["sanitized_plan"] = plan
    return result


def plan_sensor_query(state: AgentState):
    """
    Build a structured plan for data queries.
    """
    _debug("\n[Node] Plan Builder")
    state = {**state, "request_clock_local": reference_clock_local({
        "timezone_iana": state.get("timezone_iana"),
    })}
    question = _latest_question(state)
    history = _effective_history(state, max_turns=6)
    memory_context = str(state.get("memory_context") or "")
    d4 = (state.get("dependency_results") or {}).get("D4")
    if d4 and d4.get("status") == "success":
        resolved = (d4.get("structured_result") or {}).get("resolved_items") or []
        memory_context += "\nDeclared D4 resolved context (use only its sourced values):\n" + json.dumps(
            resolved, ensure_ascii=False, default=str
        )
    # Telegram reply-to: if the user replied to a specific bubble, treat that
    # quoted text as the "last assistant message" for follow-up resolution, so the
    # follow-up is interpreted against the replied-to message, not the most recent.
    reply_to_text = str(state.get("reply_to_text") or "").strip()
    last_assistant_message = reply_to_text or _last_assistant_text(state)
    last_plan = state.get("last_plan")
    if not isinstance(last_plan, dict):
        last_plan = {}

    started = time.perf_counter()

    # The original implementation carried a prior plan forward for turns whose
    # meaning is entirely supplied by that plan. This is both more reliable and
    # cheaper than asking the planner to reconstruct "yes" or "yesterday".
    if last_plan and (
        _is_contextual_followup(question) or _is_time_only_followup(question)
    ):
        plan = _enrich_plan_from_state(
            sanitize_plan({}, question, last_plan),
            state,
        )
        _debug("  -> Plan source: follow-up carry-forward")
        _debug(f"  -> Plan generation latency: {_elapsed_ms(started)} ms")
        _debug("  -> Plan:")
        _debug(json.dumps(plan, ensure_ascii=False, indent=2))
        return _plan_node_result(plan, last_plan, {})

    system, user = load_role_prompt("d1_sensor_data/plan_sensor_query.txt")
    prompt = ChatPromptTemplate.from_messages([("system", system), ("human", user)])
    chain = prompt | llm | StrOutputParser()

    today = datetime.datetime.fromisoformat(state["request_clock_local"]).date()
    available_metrics_str = ", ".join(sorted(METRIC_DEFINITIONS.keys()))

    # Give the LLM both canonical concepts and the observed backend keys. This
    # lets it choose relevant evidence for broad questions without hardcoding a
    # fixed summary bundle in Python.
    user_available_metrics_str = _format_user_available_metrics(
        state.get("availability_context")
    )

    prompt_values = {
        "history": history,
        "memory_context": memory_context or "None",
        "last_assistant_message": last_assistant_message,
        "last_plan": json.dumps(last_plan, ensure_ascii=False),
        "question": question,
        "today_date": today.isoformat(),
        "today_weekday": today.strftime("%A"),
        "current_local_time": state["request_clock_local"],
        "available_metrics": available_metrics_str,
        "user_available_metrics": user_available_metrics_str,
        "validation_feedback": "None",
    }
    candidate = {}
    failure_code = "planner_invalid_output"
    failure_exception = None
    repair_trace = {"attempted": False}
    try:
        llm_started = time.perf_counter()
        raw = chain.invoke(prompt_values)
        _debug(f"  -> Plan LLM latency: {_elapsed_ms(llm_started)} ms")
        parsed = _parse_plan_object(raw)
        if isinstance(parsed, dict):
            candidate = parsed
    except Exception as exc:
        logger.warning("plan_sensor_query LLM failed: %s", exc)
        failure_code = "planner_call_failed"
        failure_exception = type(exc).__name__

    if not candidate and failure_code == "planner_invalid_output":
        # A malformed/empty JSON response is a model representation error,
        # not ambiguity in the user's question. Share the same one-repair
        # budget with schema validation below; never substitute a default plan.
        repair_trace = {"attempted": True, "succeeded": False,
                        "initial_errors": [{"code": failure_code, "path": "plan"}]}
        try:
            repaired_text = chain.invoke({
                **prompt_values,
                "validation_feedback": json.dumps({"errors": [{
                    "code": failure_code,
                    "message": "Return one non-empty valid JSON object matching the plan interface. "
                               "The original question and context are unchanged.",
                }]}),
            })
            parsed = _parse_plan_object(repaired_text)
            if isinstance(parsed, dict) and parsed:
                candidate = parsed
        except Exception as exc:
            repair_trace["error_type"] = type(exc).__name__
            logger.warning("Sensor plan JSON repair failed: %s", type(exc).__name__)

    if not candidate:
        # No semantic plan is not a request for a default metric bundle. Fail
        # closed so malformed/empty model output cannot trigger an unrelated
        # health-data query or make the answer claim invented metric intent.
        plan = _enrich_plan_from_state(
            {
                "metrics": [],
                "comparison_requested": False,
                "execution_ready": False,
                "needs_followup": True,
                "followup_question": (
                    "I could not interpret that request reliably. Please restate "
                    "the measurement and calculation you would like to know."
                ),
                "validation_errors": [
                    {
                        "code": failure_code,
                        "path": "plan",
                        "message": "The planner did not produce a non-empty JSON plan.",
                        **({"exception": failure_exception} if failure_exception else {}),
                    }
                ],
            },
            state,
        )
        _debug(f"  -> Plan unavailable: {failure_code}")
        return {**_plan_node_result(plan, last_plan, candidate), "sensor_plan_trace": repair_trace}

    requested_time_range = candidate.get("time_range")
    if isinstance(requested_time_range, dict):
        requested_time_range = dict(requested_time_range)
    if isinstance(candidate.get("time_range"), dict):
        candidate = _resolve_all_available_for_state(candidate, state)
    parsed_time_range, _, _ = _parse_time_range_from_question(question)
    if isinstance(parsed_time_range, dict) and str(
        parsed_time_range.get("mode") or ""
    ).lower() in {"all_available", "absolute"} and re.search(
        r"\b(?:all available|entire history|full history)\b", question.lower()
    ):
        candidate = dict(candidate)
        candidate["time_range"] = parsed_time_range
        candidate = _resolve_all_available_for_state(candidate, state)
        if active_evaluation_time() is None:
            # Explicit all-history questions use the typed execution-readiness
            # contract after their production availability bounds are frozen.
            candidate["schema_version"] = 2
    plan = _enrich_plan_from_state(
        sanitize_plan(candidate, question, last_plan),
        state,
    )
    if repair_trace["attempted"]:
        repair_trace["succeeded"] = plan.get("execution_ready") is not False and not plan.get("validation_errors")
    if (not repair_trace["attempted"] and plan.get("execution_ready") is False
            and plan.get("validation_errors")):
        # Repair the model's representation once; never silently discard a
        # condition/selector or substitute a Python-authored query plan.
        repair_trace = {"attempted": True, "succeeded": False,
                        "initial_errors": plan["validation_errors"]}
        try:
            repaired_text = chain.invoke({
                **prompt_values,
                "validation_feedback": json.dumps({
                    "previous_plan": candidate,
                    "errors": plan["validation_errors"],
                }, ensure_ascii=False, default=str),
            })
            repaired = _parse_plan_object(repaired_text)
            if isinstance(repaired, dict) and repaired:
                repaired = _resolve_all_available_for_state(repaired, state)
                checked = _enrich_plan_from_state(sanitize_plan(repaired, question, last_plan), state)
                if checked.get("execution_ready") is not False and not checked.get("validation_errors"):
                    candidate, plan = repaired, checked
                    repair_trace["succeeded"] = True
        except Exception as exc:
            repair_trace["error_type"] = type(exc).__name__
            logger.warning("Sensor plan repair failed: %s", type(exc).__name__)
    # The optional second-opinion scope review is an extra LLM call that rebuilds
    # the plan. The planner prompt's worked examples cover cross-stream scope, so
    # it is opt-in (SENSEI_D1_SCOPE_REVIEW=1) rather than part of every request.
    scope_review = {"attempted": False, "reason": "disabled"}
    if os.environ.get("SENSEI_D1_SCOPE_REVIEW") == "1":
        scope_system, scope_user = load_role_prompt("d1_sensor_data/review_measurement_scope.txt")
        scope_chain = ChatPromptTemplate.from_messages([
            ("system", scope_system), ("human", scope_user),
        ]) | llm | StrOutputParser()
        plan, candidate, scope_review = _review_observed_measurement_scope(
            plan, candidate, state, scope_chain, prompt_values, question, last_plan, requested_time_range,
        )
    repair_trace["measurement_scope_review"] = scope_review
    _debug(f"  -> Plan generation latency: {_elapsed_ms(started)} ms")
    _debug("  -> Plan:")
    _debug(json.dumps(plan, ensure_ascii=False, indent=2))
    return {**_plan_node_result(plan, last_plan, candidate), "sensor_plan_trace": repair_trace}
