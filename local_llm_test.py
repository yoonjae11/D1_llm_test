"""Local chat with selectable deployments and reference-table evaluation clocks.

Uses the real shared LangGraph and ADX. No benchmark or source-specific branch.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
import uuid
from contextlib import nullcontext
from datetime import date
from pathlib import Path

from runtime_environment import load_local_environment

APP_ROOT = Path(__file__).resolve().parent


def evaluation_clock(as_of: str | None, data_start: str | None):
    """A caller-defined data date, identical for all measurement resources."""
    if as_of is None and data_start is None:
        return None
    if not as_of or not data_start:
        raise ValueError("--as-of and --data-start must be supplied together")
    reference, minimum = date.fromisoformat(as_of), date.fromisoformat(data_start)
    if minimum > reference:
        raise ValueError("--data-start cannot follow --as-of")
    from chatbot.policies.time_ranges import EvaluationTimeContext
    return EvaluationTimeContext(
        summary_reference_date=reference, summary_min_date=minimum,
        workout_reference_date=reference, workout_min_date=minimum,
    )


def isolated_memory(user_id: str, timezone: str = "Asia/Seoul"):
    """Run the shared graph with local profile/conversation/inventory storage."""
    from chatbot.services.memory import use_memory_ports
    from chatbot.services.memory.in_memory import InMemoryOperationalStorage
    repository = InMemoryOperationalStorage(user_id, timezone)
    return use_memory_ports(repository, repository)


def resolve_local_clock(user_id: str, mode: str = "referenced-data", *,
                        as_of: str | None = None, data_start: str | None = None,
                        timezone: str = "Asia/Seoul", port=None):
    """Explicit dates override discovery; current mode keeps the production clock."""
    explicit = evaluation_clock(as_of, data_start)
    if explicit is not None:
        return explicit, {"mode": "explicit", "basis": "caller_defined_dates"}
    if mode == "current":
        return None, {"mode": "current"}
    if mode not in {"latest-data", "referenced-data"}:
        raise ValueError(f"Unknown local clock mode: {mode}")
    from chatbot.services.local_data_clock import discover_latest_data_clock, discover_referenced_data_clock
    discover = discover_referenced_data_clock if mode == "referenced-data" else discover_latest_data_clock
    discovered = discover(user_id, timezone, port=port)
    return discovered.context, discovered.diagnostics()


def trace_record(answer, user_id: str, model: str, question: str, clock,
                 clock_details: dict | None = None) -> dict:
    """Only query diagnostics; no credentials or benchmark labels."""
    state = answer.payload.get("final_state") or {}
    d1 = state.get("d1_result") or {}
    provenance = d1.get("provenance") or {}
    from chatbot.policies.time_ranges import evaluation_time_family
    family = evaluation_time_family(state.get("plan"))
    return {
        "user_id": user_id, "model": model, "question": question,
        "clock": {
            "as_of": clock.bounds(family)[1].isoformat(),
            "data_start": clock.bounds(family)[0].isoformat(),
            "referenced_family": family,
            "table_clocks": {name: {"as_of": clock.bounds(name)[1].isoformat(),
                                    "data_start": clock.bounds(name)[0].isoformat()}
                             for name in ("summary", "workout")},
            **(clock_details or {}),
        } if clock is not None else {"mode": "current"},
        "plan": state.get("plan"),
        "raw_plan": state.get("raw_plan") if state.get("raw_plan") is not None else provenance.get("raw_plan"),
        "sanitized_plan": state.get("sanitized_plan") if state.get("sanitized_plan") is not None else provenance.get("sanitized_plan"),
        "generation_trace": state.get("kql_generation_trace"),
        "sensor_plan_trace": state.get("sensor_plan_trace"),
        "kql": state.get("kql_query"),
        "query_result": state.get("query_result"),
        "coverage": state.get("coverage_context"),
        "d1_result": state.get("d1_result"),
        "orchestration_trace": state.get("orchestration_trace"),
        "answer": answer.text,
        "execution_status": answer.execution_status,
        "error": state.get("error"),
    }


def _single_value(row: dict):
    if "Value" in row:
        return row.get("Value")
    nums = [v for k, v in row.items() if isinstance(v, (int, float)) and not isinstance(v, bool)
            and k not in ("EvidenceRows", "SourceCount")]
    return nums[0] if len(nums) == 1 else None


def numeric_answer(state) -> str:
    """Evaluation-only short answer: the queried value itself, without LLM prose."""
    if state.get("error"):
        return "ERROR"
    plan = state.get("plan") if isinstance(state.get("plan"), dict) else {}
    if plan.get("execution_ready") is False:
        return "CLARIFICATION"
    raw = state.get("query_result")
    if raw is None:
        return "NO_QUERY"
    try:
        rows = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return "UNPARSEABLE"
    if isinstance(rows, dict):
        rows = rows.get("rows", [rows])
    rows = [r for r in (rows if isinstance(rows, list) else []) if isinstance(r, dict)]
    values = [v for v in (_single_value(r) for r in rows) if v is not None]
    if not values:
        return "NULL"
    if len(values) > 1:
        return "MULTI: " + ", ".join(str(v) for v in values[:10])
    value = values[0]
    return str(round(value, 4)) if isinstance(value, float) else str(value)


LABEL_ANSWER_SYSTEM = (
    "You answer a health-data question using ONLY the query result rows provided. "
    "Reply with ONLY the answer value(s) - no sentence, no explanation, no units, no markdown. "
    "If several values answer the question, list each distinct value once, separated by ', '. "
    "If the rows contain no answer, reply NULL."
)


def label_answer(state, question: str) -> str:
    """Evaluation-only short text answer (e.g. 'Run') produced by the selected LLM."""
    status = numeric_answer(state)
    if status in ("ERROR", "CLARIFICATION", "NO_QUERY", "UNPARSEABLE"):
        return status
    if status == "NULL":  # numeric view found no Value; text-only rows still need the LLM
        raw = state.get("query_result")
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else raw
        except ValueError:
            parsed = None
        if not parsed:
            return "NULL"
    from chatbot.local_models import active_model, complete
    profile = active_model()
    if profile is None:
        return "NO_MODEL"
    rows = state.get("query_result")
    rows = rows if isinstance(rows, str) else json.dumps(rows, ensure_ascii=False, default=str)
    reply = complete(profile, [
        {"role": "system", "content": LABEL_ANSWER_SYSTEM},
        {"role": "user", "content": f"Question: {question}\nQuery result rows (JSON): {rows[:6000]}"},
    ])["text"]
    return " ".join(reply.strip().strip("`").split()) or "NULL"


_TIMED_D1_NODES = {
    "plan_sensor_query": "plan_ms",
    "generate_sensor_query": "query_generation_ms",
    "guard_sensor_query": "guard_ms",
    "repair_sensor_query": "repair_ms",
    "execute_sensor_query": "adx_execution_ms",
}
_LLM_STAGES = {"plan_sensor_query": "plan", "generate_sensor_query": "query_generation",
               "repair_sensor_query": "repair"}
_timings: dict | None = None
_llm_stage: str | None = None  # D1 node whose LLM calls are being measured
_llm_baseline_ms: float | None = None  # minimal-request time of the active model


def measure_llm_baseline(profile) -> None:
    """Measure the active model's network/queue baseline once per process."""
    global _llm_baseline_ms
    from chatbot.local_models import measure_baseline_ms
    try:
        _llm_baseline_ms = measure_baseline_ms(profile)
    except Exception as exc:  # timing support must never stop the evaluation
        print(f"Baseline measurement failed: {type(exc).__name__}")
        _llm_baseline_ms = None
    shown = "unavailable" if _llm_baseline_ms is None else f"{_llm_baseline_ms / 1000:.2f}s"
    print(f"LLM server baseline (minimal request server time, median of 3): {shown}")


def _add_timing(key: str, started: float, count_key: str | None = None) -> None:
    if _timings is not None:
        _timings[key] = _timings.get(key, 0.0) + (time.perf_counter() - started) * 1000.0
        if count_key:
            _timings[count_key] = _timings.get(count_key, 0) + 1


def _add_llm_metrics(result) -> None:
    """Add one model call's streamed timing and token usage to the question totals."""
    if _timings is None:
        return
    metrics = result.get("metrics") if isinstance(result, dict) else None
    if not isinstance(metrics, dict):
        _timings["llm_unmeasured_calls"] = _timings.get("llm_unmeasured_calls", 0) + 1
        return
    stage = _llm_stage or "other"
    totals = {"llm_request_ms": metrics.get("request_ms"), "llm_headers_ms": metrics.get("headers_ms"),
              "llm_first_output_ms": metrics.get("first_output_ms"),
              "llm_generation_ms": metrics.get("generation_ms"),
              "llm_retry_wait_ms": metrics.get("retry_wait_ms"),
              "llm_input_tokens": metrics.get("input_tokens"),
              "llm_output_tokens": metrics.get("output_tokens"),
              "llm_reasoning_tokens": metrics.get("reasoning_tokens"),
              f"{stage}_llm_generation_ms": metrics.get("generation_ms"),
              f"{stage}_llm_output_tokens": metrics.get("output_tokens")}
    attempts = int(metrics.get("attempts") or 0)
    server_ms = metrics.get("server_ms")
    totals["llm_network_ms"] = metrics.get("network_ms")
    totals["llm_server_ms"] = server_ms
    if _llm_baseline_ms is not None and isinstance(server_ms, (int, float)) and attempts:
        # Inference = server time (request sent -> response done) minus the
        # model's minimal-request server time per attempt. Wait = connection
        # setup (DNS/TCP/TLS/upload, measured per call) + that fixed server
        # overhead + retry back-off. Same rule for streaming and buffered models.
        overhead = min(server_ms, attempts * _llm_baseline_ms)
        totals["llm_inference_ms"] = server_ms - overhead
        totals[f"{stage}_llm_inference_ms"] = server_ms - overhead
        totals["llm_wait_ms"] = (float(metrics.get("network_ms") or 0.0) + overhead
                                 + float(metrics.get("retry_wait_ms") or 0.0))
    else:
        _timings["llm_unmeasured_calls"] = _timings.get("llm_unmeasured_calls", 0) + 1
    for key, value in totals.items():
        if isinstance(value, (int, float)):
            _timings[key] = _timings.get(key, 0) + value
    _timings["llm_attempts"] = _timings.get("llm_attempts", 0) + attempts


def install_stage_timing() -> None:
    """Evaluation-only wall-clock timing per D1 stage, LLM call and ADX query.

    Wraps the D1 graph's node functions and the LLM/ADX clients in this
    process only; graph structure and node code are unchanged.
    """
    import chatbot.llm_client as llm_client
    import chatbot.nodes.d1_sensor_data.workflow as d1_workflow
    import chatbot.services.sensor_inventory as sensor_inventory
    from chatbot.adx_client import adx_manager

    def timed(function, key, count_key=None, stage=None):
        def wrapper(*args, **kwargs):
            global _llm_stage
            started = time.perf_counter()
            previous = _llm_stage
            if stage:
                _llm_stage = stage
            try:
                return function(*args, **kwargs)
            finally:
                _llm_stage = previous
                _add_timing(key, started, count_key)
        return wrapper

    def timed_llm(function):
        def wrapper(*args, **kwargs):
            started = time.perf_counter()
            result = None
            try:
                result = function(*args, **kwargs)
                return result
            finally:
                _add_timing("llm_ms", started, "llm_calls")
                _add_llm_metrics(result)
        return wrapper

    for name, key in _TIMED_D1_NODES.items():
        setattr(d1_workflow, name, timed(getattr(d1_workflow, name), key, stage=_LLM_STAGES.get(name)))
    llm_client.complete_local_model = timed_llm(llm_client.complete_local_model)
    adx_manager.execute_query = timed(adx_manager.execute_query, "adx_ms", "adx_queries")
    sensor_inventory.observe_sensor_schema = timed(sensor_inventory.observe_sensor_schema,
                                                   "schema_inventory_ms")


def start_question_timing() -> None:
    global _timings
    _timings = {"started": time.perf_counter()}


def finish_question_timing() -> dict:
    global _timings
    timings, _timings = _timings or {}, None
    started = timings.pop("started", time.perf_counter())
    result = {"total_ms": (time.perf_counter() - started) * 1000.0}
    result.update(timings)
    if _llm_baseline_ms is not None:
        result["llm_baseline_ms"] = _llm_baseline_ms
    if result.get("llm_unmeasured_calls"):
        # A call without timing makes the question totals incomplete.
        result.pop("llm_inference_ms", None)
        result.pop("llm_wait_ms", None)
    return {key: round(value, 1) if isinstance(value, float) else value for key, value in result.items()}


def install_answer_only(label_mode: bool = False) -> None:
    """Replace only the final prose step, in this process only (chatbot/ is not edited)."""
    from langchain_core.messages import AIMessage
    import chatbot.nodes.common.generate_final_response as final_response

    def short_response(state):
        if label_mode:
            question = final_response._latest_question(state)
            question = str(question).split("\n\nEvaluation measurement metadata:")[0]
            answer = label_answer(state, question)
        else:
            answer = numeric_answer(state)
        return {"messages": [AIMessage(content=answer)],
                "last_result_summary": json.dumps({"answer": answer}, ensure_ascii=False)}

    final_response._generate_standard_response = short_response


def main() -> int:
    parser = argparse.ArgumentParser(description="Chat with the shared Sensei LangGraph.")
    model_options = parser.add_mutually_exclusive_group()
    model_options.add_argument("--model", help="One deployment (default: gpt-5).")
    model_options.add_argument("--models", nargs="+", help="Run the same --question(s) once per selected deployment, with isolated memory.")
    model_options.add_argument("--all-models", action="store_true", help="Compare all configured deployments; requires --question. Calls are billed by Azure.")
    parser.add_argument("--user-id", help="AppUserId; prompts if omitted (default: kist008).")
    parser.add_argument("--list-models", action="store_true")
    parser.add_argument("--clock", choices=("referenced-data", "latest-data", "current"), default="referenced-data",
                        help="Referenced table's latest date (default); latest-data shares one global date; current uses real time.")
    parser.add_argument("--as-of", help="Evaluation today, YYYY-MM-DD; normally the dataset's last logical day.")
    parser.add_argument("--data-start", help="First logical data day, YYYY-MM-DD; required with --as-of.")
    parser.add_argument("--question", action="append", help="Run a question noninteractively; repeat for multiple turns.")
    parser.add_argument("--fresh-thread", action="store_true", help="Isolate each question instead of retaining chat context.")
    parser.add_argument("--trace-output", type=Path, help="Write question/plan/KQL/rows/answer diagnostics as JSON.")
    parser.add_argument("--isolated", action="store_true", help="Use in-memory profiles/conversations/inventory; still call the real LLM and ADX.")
    parser.add_argument("--timezone", default="Asia/Seoul", help="IANA timezone for clock lookup and --isolated profile (default: Asia/Seoul).")
    parser.add_argument("--stored-speed-unit", choices=("m/s", "m/min", "km/h"),
                        help="Explicit local producer contract for recorded Value.speed; never rescales stored values.")
    parser.add_argument("--question-speed-unit", choices=("m/s", "m/min", "km/h"),
                        help="Local evaluation convention for speed thresholds lacking a unit; requires --stored-speed-unit.")
    parser.add_argument("--label-answer", action="store_true",
                        help="Evaluation: like --answer-only, but the LLM replies with a short text value (e.g. Run).")
    parser.add_argument("--answer-only", action="store_true",
                        help="Evaluation: reply with the queried value only (number/NULL/ERROR); skips the final-response LLM call.")
    args = parser.parse_args()

    load_local_environment(APP_ROOT)
    try:
        from local_evaluation_units import LocalEvaluationUnits, SPEED_UNITS
        evaluation_units = LocalEvaluationUnits(args.stored_speed_unit, args.question_speed_unit)
        clock = evaluation_clock(args.as_of, args.data_start)
    except ValueError as exc:
        parser.error(str(exc))
    from chatbot.local_models import load_models, require_key, use_model
    from chatbot.policies.time_ranges import use_evaluation_time
    from chatbot.services.measurement_contract import use_workout_speed_unit

    profiles = load_models(APP_ROOT / "local_models.json")
    if args.list_models:
        print("\n".join(profiles))
        return 0
    comparison = bool(args.models or args.all_models)
    names = list(profiles) if args.all_models else list(dict.fromkeys(args.models or [args.model or "gpt-5"]))
    for name in names:
        if name not in profiles:
            parser.error(f"Unknown model {name!r}. Choose from: {', '.join(profiles)}")
    if comparison and not args.question:
        parser.error("--models/--all-models requires at least one --question")
    if comparison:
        # A comparison must not inherit another model's conversation/inventory,
        # or mutate the user's stored conversations/profile.
        args.isolated = args.fresh_thread = True
    if args.user_id is None:
        try:
            user_id = input("AppUserId [kist008]: ").strip() or "kist008"
        except EOFError:
            user_id = "kist008"
    else:
        user_id = args.user_id.strip()
    if not user_id:
        parser.error("--user-id cannot be empty")
    try:
        # Validate timezone before making any external request. Construct a new
        # repository per model below, not a shared cross-model cache.
        if args.isolated:
            isolated_memory(user_id, args.timezone)
    except ValueError as exc:
        parser.error(str(exc))
    except KeyError as exc:
        parser.error(f"Unknown IANA timezone: {args.timezone}")

    for name in names:
        require_key(profiles[name])
    try:
        clock, clock_details = resolve_local_clock(
            user_id, args.clock, as_of=args.as_of, data_start=args.data_start,
            timezone=args.timezone,
        )
    except (ValueError, KeyError, RuntimeError) as exc:
        parser.error(str(exc))
    os.environ["KQL_GENERATE_METHOD"] = "llm"
    from chatbot.entrypoint import handle_user_message
    answer_mode = "label" if args.label_answer else "numeric" if args.answer_only else "prose"
    if args.label_answer:
        install_answer_only(label_mode=True)
        print("Answer mode: short text value from the LLM (e.g. Run)")
    elif args.answer_only:
        install_answer_only()
        print("Answer mode: queried value only (no final-response LLM call)")

    install_stage_timing()
    print(f"User: {user_id} | Models: {', '.join(names)} | KQL: LLM")
    if clock is not None:
        for family in ("summary", "workout"):
            minimum, reference = clock.bounds(family)
            print(f"Evaluation {family}: today={reference} | Data starts: {minimum}")
        print(f"Clock: {clock_details['mode']} | Basis: {clock_details['basis']}")
        if clock_details.get("limitation"):
            print(f"Clock scope: {clock_details['limitation']}")
    else:
        print("Clock: current local date")
    if args.isolated:
        print(f"Memory: isolated in-process | Timezone: {args.timezone} | ADX/LLM: real configured services")
    else:
        print("Uses configured ADX and conversation storage.")
    print("Type /exit to finish.")

    traces, failures = [], 0
    for name in names:
        profile = profiles[name]
        thread_id = f"local-llm:{profile.name}:{uuid.uuid4().hex}"
        pending = iter(args.question) if args.question else None
        memory_context = isolated_memory(user_id, args.timezone) if args.isolated else nullcontext()
        print(f"\nModel: {profile.name}")
        measure_llm_baseline(profile)
        with use_model(profile), use_evaluation_time(clock) if clock is not None else nullcontext(), memory_context, use_workout_speed_unit(SPEED_UNITS.get(args.stored_speed_unit)):
            while True:
                try:
                    message = next(pending) if pending is not None else input("You> ").strip()
                except (StopIteration, EOFError, KeyboardInterrupt):
                    break
                if message.lower() in {"/exit", "/quit"}:
                    break
                if not message:
                    continue
                if args.fresh_thread:
                    thread_id = f"local-llm:{profile.name}:{uuid.uuid4().hex}"
                if pending is not None:
                    print(f"You> {message}")
                runtime_message = evaluation_units.prepare_question(message)
                start_question_timing()
                try:
                    answer = asyncio.run(handle_user_message(
                        app_user_id=user_id, message_text=runtime_message, thread_id=thread_id,
                    ))
                except Exception as exc:
                    failures += 1
                    print(f"Sensei error: {exc}")
                    traces.append({"user_id": user_id, "model": profile.name, "answer_mode": answer_mode,
                                   "question": message, "execution_status": "error", "error": str(exc),
                                   "timings": finish_question_timing()})
                    continue
                timings = finish_question_timing()
                print(f"Sensei> {answer.text}")
                print(f"Timing (s): total {timings['total_ms'] / 1000:.1f} | plan "
                      f"{timings.get('plan_ms', 0) / 1000:.1f} | query generation "
                      f"{timings.get('query_generation_ms', 0) / 1000:.1f} | ADX execution "
                      f"{timings.get('adx_execution_ms', 0) / 1000:.1f} | LLM inference "
                      f"{timings.get('llm_inference_ms', 0) / 1000:.1f} | LLM wait "
                      f"{timings.get('llm_wait_ms', 0) / 1000:.1f} | output tokens "
                      f"{timings.get('llm_output_tokens', 0)}")
                traces.append(trace_record(answer, user_id, profile.name, message, clock, clock_details))
                traces[-1].update(runtime_question=runtime_message, answer_mode=answer_mode,
                                  evaluation_units=evaluation_units.diagnostics(), timings=timings)
                if clock is not None:
                    print(f"Question clock: {traces[-1]['clock']['as_of']} ({traces[-1]['clock']['referenced_family']})")
                if answer.execution_status == "error":
                    failures += 1
                    print(f"Execution error: {answer.payload.get('error')}")
    if args.trace_output:
        args.trace_output.parent.mkdir(parents=True, exist_ok=True)
        args.trace_output.write_text(
            json.dumps(traces, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        print(f"Trace: {args.trace_output.resolve()}")
    return 1 if args.question and failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
