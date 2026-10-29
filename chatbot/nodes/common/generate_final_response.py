"""LangGraph node that produces the final user-facing response."""

import json
import time
from datetime import datetime
from typing import Any, Mapping, Optional

from langchain_core.messages import AIMessage
from langchain_core.output_parsers import StrOutputParser

from chatbot.nodes.operational.alert_config_responder import alert_config_responder
from chatbot.nodes.operational.capabilities_responder import capabilities_responder
from chatbot.nodes.operational.onboarding_responder import onboarding_responder
from chatbot.config import get_settings
from chatbot.graph.state import AgentState
from chatbot.i18n import message
from chatbot.services.llm import LLMPort, default_llm_service, generate_user_facing
from chatbot.policies.availability import uncollected_metrics
from chatbot.policies.result_processing import (
    _filter_meaningless_metrics,
    _has_records,
    normalize_result_values,
    _parse_query_result,
    trim_records_for_plan,
)
from chatbot.policies.metric_normalization import _normalize_metrics
from chatbot.policies.response_format import (
    append_non_redundant_followup as _append_non_redundant_followup,
    _build_analysis_footer,
    _build_source_aware_monitoring_answer,
    _polish_answer_text,
    _strip_llm_footer,
    _strip_trailing_offer_question,
)
from chatbot.prompts import load_chat_prompt
from chatbot.nodes.common.support import (
    legacy_debug as _debug,
    effective_history as _effective_history,
    elapsed_ms as _elapsed_ms,
    last_assistant_text as _last_assistant_text,
    latest_question as _latest_question,
    llm,
)
from chatbot.policies.time_ranges import (
    _local_today,
    active_evaluation_time,
    evaluation_time_family,
)


_default_response_port: LLMPort = default_llm_service()


def _answer_today(plan: Mapping[str, Any], state: Mapping[str, Any]) -> str:
    """Use the request-local evaluation clock when one is bound."""

    evaluation_time = active_evaluation_time()
    if evaluation_time is not None:
        return evaluation_time.bounds(evaluation_time_family(dict(plan)))[1].isoformat()
    if plan.get("reference_clock_local"):
        return datetime.fromisoformat(str(plan["reference_clock_local"])).date().isoformat()
    return _local_today(str(plan.get("timezone_iana") or state.get("timezone_iana") or "")).isoformat()


def call_user_facing_response(
    input_text: str,
    *,
    previous_response_id: Optional[str] = None,
    instructions: Optional[str] = None,
    store: bool = True,
    deployment: Optional[str] = None,
    insecure_ssl: Optional[bool] = None,
) -> Mapping[str, Any]:
    """Call the configured user-facing LLM port."""

    return _default_response_port.respond(
        input_text,
        previous_response_id=previous_response_id,
        instructions=instructions,
        store=store,
        deployment=deployment,
        insecure_ssl=insecure_ssl,
    )


def _user_facing_generate(state, prompt, variables):
    """Generate a user-facing answer through the injected service boundary."""

    return generate_user_facing(
        state=state or {},
        prompt=prompt,
        variables=variables,
        response_call=call_user_facing_response,
        fallback_model=llm,
        debug=_debug,
        settings=get_settings(),
    )


def _coaching_response(state, *, question, memory_context, answer_language, data_json="null"):
    """One final-response call using validated D2 strategy and optional D1 facts."""
    prompt = load_chat_prompt("common/generate_final_response/coaching.txt")
    text, response_id = _user_facing_generate(state, prompt, {
        "question": question,
        "history": _effective_history(state, max_turns=6),
        "memory_context": memory_context or "None",
        "answer_language": answer_language,
        "coaching_strategy": json.dumps(state["coaching_strategy"], ensure_ascii=False),
        "data_json": data_json,
        "coverage_context": json.dumps(state.get("coverage_context") or {}, ensure_ascii=False, default=str),
    })
    # A strategy-requested open question is part of coaching, not a routine offer.
    answer = _polish_answer_text(_strip_llm_footer(text.strip()))
    if data_json != "null":
        footer = _build_analysis_footer(state.get("plan") or {}, question=question)
        if footer:
            answer = answer.rstrip() + "\n\n" + footer
    result = {
        "messages": [AIMessage(content=answer)],
        "last_result_summary": json.dumps({
            "question": question, "intent": state.get("intent"), "answer": answer,
            "dialogue_objective": state["coaching_strategy"]["dialogue_objective"],
        }, ensure_ascii=False),
    }
    if response_id:
        result["response_id"] = response_id
    return result


def _generate_standard_response(state: AgentState):
    """
    Generate final user response from intent + query result.
    """
    _debug("\n[Node] Response Generator")
    question = _latest_question(state)
    intent = str(state.get("intent", "chat")).lower()
    plan = state.get("plan") if isinstance(state.get("plan"), dict) else {}
    memory_context = str(state.get("memory_context") or "")
    if state.get("response_domain_context"):
        memory_context += (
            "\nDomain results: use completed retrieval records as evidence. A D1 missing "
            "metric does not erase a retrieved entity/date; retain known fields and null "
            "values. A placeholder or blocked dependency is not evidence. If D2 is "
            "blocked, provide available facts only, without inventing a coaching plan.\n"
            + json.dumps(state["response_domain_context"], ensure_ascii=False, default=str)
        )
    # Telegram reply-to: surface the quoted bubble so the answer addresses what the
    # user replied to (both the chat and data answer prompts include {memory_context}).
    _reply_to_text = str(state.get("reply_to_text") or "").strip()
    if _reply_to_text:
        _reply_role = str(state.get("reply_to_role") or "assistant").strip() or "assistant"
        memory_context = (
            f"The user is replying to this earlier {_reply_role} message: «{_reply_to_text[:600]}». "
            "Interpret and answer their message as a follow-up to it.\n" + memory_context
        )
    is_korean = False
    answer_language = "English"
    coaching = bool((state.get("coaching_strategy") or {}).get("coaching_applicable"))

    if intent == "chat" and coaching:
        return _coaching_response(
            state, question=question, memory_context=memory_context,
            answer_language=answer_language,
        )

    if intent == "clarification":
        answer = message("response.clarification", is_korean=is_korean)
        return {
            "messages": [AIMessage(content=answer)],
            "last_result_summary": json.dumps({"question": question, "intent": intent, "answer": answer}, ensure_ascii=False),
        }

    if intent == "chat":
        history = _effective_history(state, max_turns=6)
        prompt = load_chat_prompt("common/generate_final_response/chat.txt")
        llm_started = time.perf_counter()
        chat_text, chat_rid = _user_facing_generate(
            state,
            prompt,
            {
                "question": question,
                "history": history,
                "memory_context": memory_context or "None",
                "answer_language": answer_language,
            },
        )
        raw_chat_answer = chat_text.strip()
        concise_chat_answer = _strip_trailing_offer_question(raw_chat_answer)
        answer = _polish_answer_text(concise_chat_answer or raw_chat_answer)
        _debug(f"  -> Response LLM latency: {_elapsed_ms(llm_started)} ms")
        _debug(f"  -> Bot: {answer}")
        chat_result = {
            "messages": [AIMessage(content=answer)],
            "last_result_summary": json.dumps({"question": question, "intent": intent, "answer": answer}, ensure_ascii=False),
        }
        if chat_rid:
            chat_result["response_id"] = chat_rid
        return chat_result

    if intent == "unsupported":
        prompt = load_chat_prompt(
            "common/generate_final_response/unsupported_metric.txt"
        )
        chain = prompt | llm | StrOutputParser()
        llm_started = time.perf_counter()
        answer = _polish_answer_text(chain.invoke({"question": question, "answer_language": answer_language}).strip())
        _debug(f"  -> Response LLM latency: {_elapsed_ms(llm_started)} ms")
        _debug(f"  -> Bot: {answer}")
        return {
            "messages": [AIMessage(content=answer)],
            "last_result_summary": json.dumps({"question": question, "intent": intent, "answer": answer}, ensure_ascii=False),
        }

    if plan.get("execution_ready") is False:
        # Typed plans fail closed.  In particular, never run a broad fallback
        # query and present it as the answer to a rejected condition/selector.
        answer = str(plan.get("followup_question") or "").strip()
        if not answer:
            answer = "I could not safely interpret that calculation. Please restate the target metric and calculation."
        answer = _polish_answer_text(answer)
        return {
            "messages": [AIMessage(content=answer)],
            "execution_status": "needs_clarification",
            "last_result_summary": json.dumps(
                {
                    "question": question,
                    "intent": intent,
                    "plan_valid": False,
                    "answer": answer,
                },
                ensure_ascii=False,
            ),
        }

    parsed = _parse_query_result(state.get("query_result"))
    has_data = _has_records(parsed)
    error = state.get("error")

    assumptions = plan.get("assumptions", []) if isinstance(plan.get("assumptions"), list) else []
    followup_question = str(plan.get("followup_question", "")).strip()
    # Only carry a follow-up question when the planner explicitly flagged the
    # request as ambiguous (needs_followup). Otherwise we neither append a
    # generic offer nor keep a trailing "Want me to ...?" line on the answer.
    needs_followup = bool(plan.get("needs_followup", False))
    case_type = str(plan.get("case_type", "monitoring"))
    metrics = _normalize_metrics(plan.get("metrics", []) if isinstance(plan.get("metrics"), list) else [])
    time_range = plan.get("time_range", {})
    coverage_context = state.get("coverage_context") if isinstance(state.get("coverage_context"), dict) else {}
    previous_assistant = _last_assistant_text(state)

    if error:
        error_text = str(error or "")
        lowered_error = error_text.lower()
        if "certificate verify failed" in lowered_error or "tls certificate verification failed" in lowered_error:
            base = message("response.error.tls", is_korean=is_korean)
        else:
            base = message("response.error.query", is_korean=is_korean)
        if followup_question:
            base = _append_non_redundant_followup(base, followup_question, previous_assistant)
        base = _polish_answer_text(base)
        _debug(f"  -> Bot: {base}")
        return {
            "messages": [AIMessage(content=base)],
            "last_result_summary": json.dumps({"question": question, "intent": intent, "error": error_text, "answer": base}, ensure_ascii=False),
        }

    if not has_data:
        if coverage_context.get("recheck_status") not in {None, "completed", "no_evidence_after_recheck"}:
            base = message("response.no_data.unverified_daily", is_korean=is_korean)
            footer = _build_analysis_footer(plan, question=question)
            if footer:
                base = base.rstrip() + "\n\n" + footer
            base = _polish_answer_text(base)
            return {
                "messages": [AIMessage(content=base)],
                "last_result_summary": json.dumps({"question": question, "intent": intent, "row_count": 0, "answer": base}, ensure_ascii=False),
            }
        if state.get("execution_status") == "success":
            # Successful empty retrieval is absent evidence, unlike a failed
            # query or an unverified daily/flat gap. The user-facing null is a
            # presentation contract, not a query/answer template.
            return {
                "messages": [AIMessage(content="null")],
                "last_result_summary": json.dumps({"question": question, "intent": intent,
                                                   "row_count": 0, "answer": "null"},
                                                  ensure_ascii=False),
            }
        availability_ctx = state.get("availability_context") if isinstance(state.get("availability_context"), dict) else {}
        uncollected = uncollected_metrics(metrics, availability_ctx)
        if uncollected and len(uncollected) == len(metrics):
            # The user has NEVER had this metric in their synced data: say
            # "not collected" plainly instead of "no records for that period".
            from chatbot.policies.response_format import _human_metric_name

            names = ", ".join(_human_metric_name(m) for m in uncollected)
            base = message(
                "response.no_data.uncollected",
                is_korean=is_korean,
                names=names,
                names_capitalized=names.capitalize(),
            )
        else:
            base = message("response.no_data.period", is_korean=is_korean)
        if followup_question:
            base = _append_non_redundant_followup(base, followup_question, previous_assistant)
        footer = _build_analysis_footer(plan, question=question)
        if footer:
            base = base.rstrip() + "\n\n" + footer
        base = _polish_answer_text(base)
        _debug(f"  -> Bot: {base}")
        return {
            "messages": [AIMessage(content=base)],
            "last_result_summary": json.dumps({"question": question, "intent": intent, "row_count": 0, "answer": base}, ensure_ascii=False),
        }

    # Keep prompt payloads within the same configurable result bound used by the
    # executor. The default still allows a full month of daily rows.
    payload_rows = normalize_result_values(
        trim_records_for_plan(
            parsed,
            plan,
            max_rows=max(1, int(get_settings().max_result_rows)),
        )
    )
    # In the typed result contract, zero is meaningful (for example zero
    # matching sessions) and null is an explicit no-evidence signal. Preserve
    # both, together with zero-valued coverage counters, for grounded wording.
    if plan.get("schema_version") != 2:
        payload_rows = _filter_meaningless_metrics(payload_rows)
    payload = json.dumps(payload_rows, ensure_ascii=False, default=str)
    if coaching:
        return _coaching_response(
            state, question=question, memory_context=memory_context,
            answer_language=answer_language, data_json=payload,
        )
    assumptions_text = "None"
    if assumptions:
        assumptions_text = "; ".join(str(item) for item in assumptions)

    # Sleep answers are intentionally phrased by the LLM (not a fixed template) so
    # it adapts to the actual question — wake time / bedtime / duration / efficiency
    # / "how did I sleep" — handles typos and missing stages gracefully, and uses
    # the rich per-night row (asleep vs in-bed vs awake, efficiency, stages). The
    # guarded query supplies the values; wording is the model's.
    # So there is no sleep fast-path return here — sleep flows to the
    # LLM response below (source-aware also returns None for sleep metrics).

    # Typed computation rows contain aggregate and selection evidence that the
    # legacy single-metric formatter does not understand.  Let the response
    # prompt verbalize those rows without recomputing their values.
    source_aware = None
    if plan.get("schema_version") != 2:
        source_aware = _build_source_aware_monitoring_answer(
            plan, payload_rows, question=question
        )
    if source_aware:
        answer = source_aware
        suggestion = followup_question if needs_followup else ""
        answer = _append_non_redundant_followup(answer, suggestion, previous_assistant)
        footer = _build_analysis_footer(plan, question=question)
        if footer:
            answer = answer.rstrip() + "\n\n" + footer
        answer = _polish_answer_text(answer)
        _debug(f"  -> Bot: {answer}")
        return {
            "messages": [AIMessage(content=answer)],
            "last_result_summary": json.dumps(
                {"question": question, "intent": intent, "plan": plan, "row_count": len(payload_rows), "answer": answer},
                ensure_ascii=False,
                default=str,
            ),
        }

    prompt = load_chat_prompt("common/generate_final_response/sensor_data.txt")
    llm_started = time.perf_counter()
    data_text, data_rid = _user_facing_generate(
        state,
        prompt,
        {
            "question": question,
            "previous_assistant": previous_assistant[-800:] if previous_assistant else "",
            "memory_context": memory_context or "None",
            "case_type": case_type,
            "metrics": ", ".join(metrics) if metrics else "none",
            "computation": json.dumps(
                plan.get("computation")
                if isinstance(plan.get("computation"), dict)
                else {},
                ensure_ascii=False,
            ),
            "time_range": json.dumps({
                "primary": time_range,
                "comparison": plan.get("comparison_time_range"),
                "daily_clock_window": plan.get("time_of_day"),
            } if plan.get("comparison_time_range") or plan.get("time_of_day")
              else time_range, ensure_ascii=False),
            "coverage_context": json.dumps(coverage_context, ensure_ascii=False, default=str),
            "assumptions": assumptions_text,
            "data_json": payload,
            "selection_context": json.dumps({
                "requested_selector": (plan.get("computation") or {}).get("selector"),
                "actual_selected_rows": coverage_context.get("selected_rows"),
                "meaning": "Observed cardinality, not requested N; state actual selected count when supplied.",
            }, ensure_ascii=False),
            "today_date": _answer_today(plan, state),
            "answer_language": answer_language,
        },
    )
    # Remove any model-written footer lines first; the deterministic footer is
    # appended exactly once below (prevents the duplicated "Analysis scope" block).
    answer = _strip_llm_footer(data_text.strip())
    _debug(f"  -> Response LLM latency: {_elapsed_ms(llm_started)} ms")

    # Always drop model-invented trailing offers first. If the planner explicitly
    # requires clarification, append the canonical follow-up below.
    answer = _strip_trailing_offer_question(answer)
    if needs_followup:
        suggestion = followup_question
        answer = _append_non_redundant_followup(answer, suggestion, previous_assistant)

    footer = _build_analysis_footer(plan, question=question)
    if footer:
        answer = answer.rstrip() + "\n\n" + footer

    answer = _polish_answer_text(answer)

    _debug(f"  -> Bot: {answer}")
    data_result = {
        "messages": [AIMessage(content=answer)],
        "last_result_summary": json.dumps(
            {"question": question, "intent": intent, "plan": plan, "row_count": len(payload_rows), "answer": answer},
            ensure_ascii=False,
            default=str,
        ),
    }
    if data_rid:
        data_result["response_id"] = data_rid
    return data_result


def generate_final_response(state):
    """Keep operational responders and the existing error/missing-data behavior."""
    intent = state.get("intent", "chat")
    if intent == "data" and state.get("onboarding_required"):
        return onboarding_responder(state)
    if intent == "capabilities":
        return capabilities_responder(state)
    if intent == "alert_config":
        return alert_config_responder(state)
    domains = state.get("domain_results") or {}
    d3 = domains.get("D3") or {}
    d3_result = d3.get("structured_result") or {}
    # Deterministic precedence: a D3 safety escalation can never be softened,
    # delayed, or replaced by coaching or model-generated prose.
    if d3.get("status") == "success" and d3_result.get("status") == "SAFETY_ESCALATION":
        answer = str(d3_result.get("safety_message") or "Seek urgent medical help now.")
        return {
            "messages": [AIMessage(content=answer)], "execution_status": "safety_escalation",
            "last_result_summary": json.dumps({"question": _latest_question(state), "answer": answer, "domain": "D3"}, ensure_ascii=False),
        }
    d4 = domains.get("D4") or {}
    if d4.get("selected") and d4.get("status") == "insufficient_information" and not any(
        value.get("status") == "success" for key, value in domains.items() if key != "D4"
    ):
        answer = str((d4.get("structured_result") or {}).get("clarification_question") or "Which earlier message do you mean?")
        return {
            "messages": [AIMessage(content=answer)], "execution_status": "needs_clarification",
            "last_result_summary": json.dumps({"question": _latest_question(state), "answer": answer, "domain": "D4"}, ensure_ascii=False),
        }
    if d3.get("selected") and d3.get("status") == "insufficient_information" and not any(
        value.get("status") == "success" for key, value in domains.items() if key != "D3"
    ):
        answer = "I could not find approved health evidence for that request, so I will not answer it from model memory."
        return {
            "messages": [AIMessage(content=answer)], "execution_status": "insufficient_information",
            "last_result_summary": json.dumps({"question": _latest_question(state), "answer": answer, "domain": "D3"}, ensure_ascii=False),
        }
    if d3.get("status") == "success" and d3_result.get("status") == "SUCCESS" and not (
        domains.get("D1", {}).get("status") == "success" or domains.get("D2", {}).get("status") == "success"
    ):
        evidence = d3_result.get("evidence") or []
        answer = str(d3_result.get("grounded_summary") or "")
        if evidence:
            sources = ", ".join(f"{item['title']} ({item['source_url']})" for item in evidence)
            answer += "\n\nSources: " + sources
        return {
            "messages": [AIMessage(content=answer)], "execution_status": "ok",
            "last_result_summary": json.dumps({"question": _latest_question(state), "answer": answer, "domain": "D3"}, ensure_ascii=False),
        }
    unavailable = [domain for domain, result in domains.items()
                   if result.get("selected") and result.get("status") == "not_implemented"]
    blocked = [domain for domain, result in domains.items()
               if result.get("selected") and result.get("status") == "blocked_dependency"]
    skipped = [domain for domain, result in domains.items()
               if result.get("selected") and result.get("status") == "skipped"]
    notice = (
        "The additional knowledge or conversation-context lookup needed for this request is not available yet, so I could not complete the part that depends on it."
        if unavailable else
        "I could not obtain the required information to complete the dependent part of your request."
    )
    # A downstream dependency failure must not replace the established sensor
    # error/no-data explanation when D1 actually ran and returned that outcome.
    sensor_failure = domains.get("D1", {}).get("status") in {"error", "insufficient_information"}
    if blocked and not sensor_failure and (
        "D1" in blocked or not any(r.get("status") == "success" for r in domains.values())
    ):
        return {
            "messages": [AIMessage(content=notice)], "execution_status": "insufficient_information",
            "last_result_summary": json.dumps({"question": _latest_question(state), "answer": notice}, ensure_ascii=False),
        }
    result = _generate_standard_response(state)
    if unavailable or blocked or skipped:
        answer = str(result["messages"][-1].content).rstrip() + "\n\n" + notice
        result["messages"] = [AIMessage(content=answer)]
        summary = json.loads(result.get("last_result_summary") or "{}")
        summary["answer"] = answer
        result["last_result_summary"] = json.dumps(summary, ensure_ascii=False)
    return result
