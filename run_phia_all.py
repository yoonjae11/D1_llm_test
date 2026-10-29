"""Run every PHIA question through local_llm_test.py for several models x N repeats, then grade.

- Reads questions from the PHIA workbook (rows 2-1001 = user 465, 1002-2001 = 171,
  2002-3001 = 333, 3002-4001 = 41). Every row is asked once per repeat (4,000 runs),
  duplicates included. Text-answer rows ("제외(정답이 수치가 아님)") run in label mode.
- Splits each user's questions into chunks; one local_llm_test.py process per
  (repeat, user, model, chunk) so a crash or Wi-Fi drop only loses one chunk.
- Resumable: a chunk whose trace file already exists is skipped. Just run the
  same command again after an interruption.
- --redo-network-errors re-runs chunks whose trace contains network/timeout errors.
- Models run in parallel (different deployments), default one worker per model.
- When everything is done, grade_phia_traces.py writes one CSV.
- A chunk whose log shows no new output for --stall-minutes (default 20) is
  stopped and counted as failed; it runs again on the next invocation.

PHIA D1 evaluation rules live in this file only (install_phia_d1_rules); the
chatbot code and prompts carry none of them. Each chunk process installs them
and then runs local_llm_test.py unchanged:
- --routing d1 (default) sends every question down the D1 data path without the
  domain-routing LLM call; --routing auto keeps the chatbot's own routing
  (D1-D4, chat/data/unsupported).
- The reference date of the evaluation clock is a completed historical day; the
  D1 planner receives that fact as evaluation context.
- A benchmark question cannot answer a clarification, so the D1 plan proceeds
  with its stated interpretation instead of asking.
- Two KQL repair attempts per question (the chatbot allows one).

Usage (from the azure_function_main_app folder, .venv active):
    python run_phia_all.py "C:\\path\\PHIA_v3.xlsx"
    python run_phia_all.py "C:\\path\\PHIA_v3.xlsx" --repeats 5 --chunk-size 50
    python run_phia_all.py "C:\\path\\PHIA_v3.xlsx" --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

import openpyxl

APP_ROOT = Path(__file__).resolve().parent
# Operating baseline (the chatbot's model), base <-> lightweight pairs, references.
DEFAULT_MODELS = ["gpt-5",
                  "gpt-6-sol", "gpt-6-luna",
                  "claude-opus-5-5", "claude-haiku-5-5",
                  "deepseek-v4-pro", "deepseek-v4-flash",
                  "grok-4-1-fast-reasoning", "grok-4-1-fast-non-reasoning",
                  "kimi-k2-6", "llama-4-maverick"]
USER_IDS = ("465", "171", "333", "41")
ROWS_PER_USER = 1000
INTERRUPT_CODES = {3221225786, -2, 130, -9}  # Ctrl+C / killed (Windows, POSIX)
MAX_FAIL_STREAK = 10


LABEL_MARK = "제외(정답이 수치가 아님)"


def load_questions(workbook: Path) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Return (numeric questions, text-answer questions) per user: every workbook row, in order."""
    ws = openpyxl.load_workbook(workbook, read_only=True, data_only=True).worksheets[0]
    rows = ws.iter_rows(values_only=True)
    header = [str(h).strip() if h is not None else "" for h in next(rows)]
    qi = header.index("Question")
    sub = header.index("수정 카테고리(소분류)") if "수정 카테고리(소분류)" in header else None
    per_user: dict[str, list[str]] = {u: [] for u in USER_IDS}
    labels: dict[str, list[str]] = {u: [] for u in USER_IDS}
    for offset, row in enumerate(rows):
        block = offset // ROWS_PER_USER
        if block >= len(USER_IDS) or not row[qi]:
            continue
        q = " ".join(str(row[qi]).split())
        user = USER_IDS[block]
        # Duplicate questions are asked again (one run per workbook row).
        if sub is not None and LABEL_MARK in str(row[sub] or ""):
            labels[user].append(q)
        else:
            per_user[user].append(q)
    return per_user, labels


def has_network_errors(path: Path) -> bool:
    try:
        records = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True
    return any("network/timeout" in str(r.get("error", "")) for r in records
               if r.get("execution_status") == "error")


RETRYABLE_ERROR_MARKERS = ("network/timeout", "RuntimeError", "ConnectError", "ConnectTimeout",
                           "ReadTimeout", "getaddrinfo", "Transient")


def is_retryable(record: dict) -> bool:
    """Infrastructure-looking failures only (LLM call/network), never wrong KQL or clarifications."""
    orchestration = record.get("orchestration_trace") or {}
    if isinstance(orchestration, dict) and orchestration.get("planner_status") == "error":
        return True  # domain planning LLM call failed (e.g. network drop)
    if record.get("execution_status") == "error":
        error = str(record.get("error") or "")
        return any(marker in error for marker in RETRYABLE_ERROR_MARKERS)
    return False


def retry_jobs(jobs):
    """(rep, user, model, questions, trace, label_mode, indices) for failed records of finished chunks."""
    out = []
    for rep, user, model, _qs, trace, label_mode, _ in jobs:
        if not trace.exists():
            continue
        try:
            records = json.loads(trace.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        indices = [i for i, r in enumerate(records) if is_retryable(r)]
        if indices:
            out.append((rep, user, model, [records[i]["question"] for i in indices], trace, label_mode, indices))
    return out


def wait_or_stall(proc: subprocess.Popen, log_path: Path, stall_seconds: float) -> tuple[int, bool]:
    """Wait for a chunk process; stop it when its log has not grown for stall_seconds."""
    last_size, last_change = -1, time.time()
    while True:
        try:
            return proc.wait(timeout=15), False
        except subprocess.TimeoutExpired:
            pass
        try:
            size = log_path.stat().st_size
        except OSError:
            size = last_size
        now = time.time()
        if size != last_size:
            last_size, last_change = size, now
        elif now - last_change > stall_seconds:
            proc.kill()
            return proc.wait(), True


# --- PHIA D1 evaluation rules (installed only inside a chunk process) --------

CHILD_FLAG = "--phia-d1-child"
EVALUATION_REPAIR_ATTEMPTS = 2
EVALUATION_CLOCK_CONTEXT = (
    "Evaluation context: the reference date is a completed day of a historical dataset. "
    "Every record dated on it, including a sleep that begins that evening, is already available."
)
_TYPED_PLAN_FIELDS = ("schema_version", "computation", "entity", "outputs", "filters",
                      "conditions", "selector", "group_by")


def forced_d1_plan(state: dict) -> dict:
    """Domain-routing result that sends the question to D1 without an LLM call."""
    from chatbot.nodes.common import support
    from chatbot.nodes.planning.plan_domain_execution import _route_name
    from chatbot.schemas.domain_execution_plan import DomainExecutionPlan

    started = time.perf_counter()
    plan = DomainExecutionPlan(
        intent="data", selected_domains=["D1"],
        domain_requirements={"D1": {"need": "Answer the supplied sensor-data question"}},
        dependencies={"D1": []}, execution_stages=[["D1"]],
        rationale="PHIA D1 evaluation runner; domain-routing LLM skipped",
    )
    return {
        "intent": plan.intent, "route": _route_name(plan.intent),
        "domain_execution_plan": plan.model_dump(),
        "orchestration_trace": {
            "planner_status": "success", "planner_llm_calls": 0,
            "routing_mode": "phia_d1_runner", "review_attempted": False,
            "planner_latency_ms": support.elapsed_ms(started),
            "execution_stages": plan.execution_stages, "validation_issues": [],
        },
    }


def with_evaluation_clock_context(plan_node):
    """Give the D1 planner the evaluation-clock fact (not part of the question text)."""
    def node(state):
        memory = str(state.get("memory_context") or "").strip()
        return plan_node({**state, "memory_context": f"{memory}\n{EVALUATION_CLOCK_CONTEXT}".strip()})
    return node


def proceed_without_clarification(plan_node):
    """A benchmark question cannot answer a clarification.

    A valid plan that only asked to confirm an interpretation is kept, with that
    interpretation recorded as an assumption. If the typed computation stays
    invalid, the plan keeps its metrics and period (the query model reads the
    original question directly). Otherwise the record says why no query ran.
    """
    import chatbot.nodes.d1_sensor_data.plan_sensor_query as planner
    from chatbot.policies.plan_sanitization import sanitize_plan
    from chatbot.policies.reactive_query_scope import reference_clock_local

    def node(state):
        result = plan_node(state)
        plan = result.get("plan") if isinstance(result.get("plan"), dict) else {}
        if plan.get("execution_ready") is not False:
            return result
        state = {**state, "request_clock_local": reference_clock_local(
            {"timezone_iana": state.get("timezone_iana")})}
        last_plan = result.get("prev_plan") if isinstance(result.get("prev_plan"), dict) else {}
        raw = result.get("raw_plan") if isinstance(result.get("raw_plan"), dict) else {}
        errors = [item for item in plan.get("validation_errors") or [] if isinstance(item, dict)]
        fallback = {"attempted": True, "accepted": False,
                    "initial_errors": [str(item.get("code")) for item in errors]}
        kept = None
        if not errors and plan.get("metrics"):
            asked = str(plan.get("followup_question") or "").strip()
            kept = {**plan, "needs_followup": False, "followup_question": "", "execution_ready": True,
                    "assumptions": [*(plan.get("assumptions") or []),
                                    *([f"Unasked clarification: {asked}"] if asked else [])]}
            fallback["mode"] = "clarification_skipped"
        elif raw:
            reduced = {key: value for key, value in raw.items() if key not in _TYPED_PLAN_FIELDS}
            reduced.update(schema_version=1, needs_followup=False, followup_question="")
            try:
                reduced = planner._resolve_all_available_for_state(reduced, state)
                checked = planner._enrich_plan_from_state(
                    sanitize_plan(reduced, planner._latest_question(state), last_plan), state)
            except Exception as exc:  # keep the unavailable plan
                fallback["error_type"] = type(exc).__name__
                checked = None
            if (checked and checked.get("execution_ready") is not False
                    and not checked.get("validation_errors") and checked.get("metrics")):
                checked["assumptions"] = [*(checked.get("assumptions") or []),
                                          "Typed computation was invalid; the query follows the question text."]
                kept = checked
                fallback["mode"] = "computation_dropped"
        plan_trace = dict(result.get("sensor_plan_trace") or {})
        if kept is not None:
            fallback["accepted"] = True
            out = planner._plan_node_result(kept, last_plan, raw)
            out["sensor_plan_trace"] = {**plan_trace, "evaluation_fallback": fallback}
            return out
        # The exception type (e.g. RuntimeError) lets --retry-failed recognise a
        # failed model call rather than a bad plan.
        codes = [str(item.get("code") or "invalid_plan")
                 + (f" {item['exception']}" if item.get("exception") else "") for item in errors]
        return {**result, "error": "D1 query plan unavailable (" + ", ".join(codes or ["needs_followup"]) + ")",
                "error_type": "sensor_plan_validation",
                "sensor_plan_trace": {**plan_trace, "evaluation_fallback": fallback}}

    return node


def evaluation_repair_attempt_limit(state) -> int:
    from chatbot.config import get_settings
    configured = max(0, int(get_settings().max_kql_repair_attempts))
    requested = state.get("max_repair_attempts")
    requested = configured if requested is None else max(0, int(requested))
    return min(EVALUATION_REPAIR_ATTEMPTS, configured, requested)


def retry_invalid_rewrite(guard_decision, can_repair):
    """A rewrite of an ADX-rejected query may fail the guard; allow the second repair."""
    def decide(state):
        decision = guard_decision(state)
        if decision != "produce_sensor_result" or state.get("guard_status") != "error":
            return decision
        trace = state.get("kql_generation_trace")
        history = trace.get("repair_history") if isinstance(trace, dict) else None
        rejected_by_adx = isinstance(history, list) and any(
            isinstance(item, dict) and item.get("origin") == "adx_validation" for item in history)
        if int(state.get("repair_attempts") or 0) == 1 and rejected_by_adx and can_repair(state):
            return "repair_sensor_query"
        return decision
    return decide


def install_phia_d1_rules(routing: str) -> None:
    """Install the PHIA D1 evaluation rules into this process."""
    import chatbot.graph.workflow as graph_workflow
    import chatbot.nodes.d1_sensor_data.repair_sensor_query as repair_node
    import chatbot.nodes.d1_sensor_data.workflow as d1_workflow

    if routing == "d1":
        graph_workflow.plan_domain_execution = forced_d1_plan
    d1_workflow.plan_sensor_query = proceed_without_clarification(
        with_evaluation_clock_context(d1_workflow.plan_sensor_query))
    repair_node.repair_attempt_limit = evaluation_repair_attempt_limit
    d1_workflow.repair_attempt_limit = evaluation_repair_attempt_limit
    d1_workflow.guard_decision = retry_invalid_rewrite(d1_workflow.guard_decision,
                                                       d1_workflow._can_repair_with_llm)


def child_main(routing: str, argv: list[str]) -> int:
    """Chunk process: install the PHIA D1 rules, then run local_llm_test.py."""
    sys.path.insert(0, str(APP_ROOT))
    from runtime_environment import load_local_environment
    load_local_environment(APP_ROOT)
    install_phia_d1_rules(routing)
    print("Domain routing: D1 data path (PHIA runner, no domain-routing LLM call)" if routing == "d1"
          else "Domain routing: chatbot's own domain planner")
    import local_llm_test
    sys.argv = [str(APP_ROOT / "local_llm_test.py"), *argv]
    return local_llm_test.main()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("workbook", type=Path)
    ap.add_argument("--models", nargs="+", default=DEFAULT_MODELS)
    ap.add_argument("--repeats", type=int, default=1,
                    help="how many times the whole question set is asked (default 1); "
                         "repeat N writes r1..rN folders and a Repeat column in the CSV")
    ap.add_argument("--users", nargs="+", default=list(USER_IDS))
    ap.add_argument("--chunk-size", type=int, default=50)
    ap.add_argument("--workers", type=int, help="parallel processes (default: number of models)")
    ap.add_argument("--out-dir", type=Path, default=Path("evaluation-output/phia"))
    ap.add_argument("--csv", type=Path, default=Path("evaluation-output/phia_results.csv"))
    ap.add_argument("--redo-network-errors", action="store_true")
    ap.add_argument("--retry-failed", action="store_true",
                    help="re-ask only questions whose LLM call/network failed in finished chunks, "
                         "and replace just those records")
    ap.add_argument("--routing", choices=("d1", "auto"), default="d1",
                    help="d1 (default): every question takes the D1 data path without the "
                         "domain-routing LLM call; auto: the chatbot's own domain planner decides")
    ap.add_argument("--stall-minutes", type=float, default=20,
                    help="stop a chunk whose log shows no new output for this long (default 20); "
                         "it counts as failed and runs again next time")
    ap.add_argument("--dry-run", action="store_true", help="only print the plan")
    args = ap.parse_args()

    questions, label_questions = load_questions(args.workbook)
    jobs = []
    for rep in range(1, args.repeats + 1):
        for user in args.users:
            qs = questions[user]
            for ci in range(0, len(qs), args.chunk_size):
                for model in args.models:
                    trace = args.out_dir / f"r{rep}" / f"u{user}" / model / f"chunk{ci // args.chunk_size:03d}-r{rep}.json"
                    jobs.append((rep, user, model, qs[ci:ci + args.chunk_size], trace, False, None))
            # Text-answer questions ("제외(정답이 수치가 아님)") run in label mode.
            lqs = label_questions[user]
            for ci in range(0, len(lqs), args.chunk_size):
                for model in args.models:
                    trace = args.out_dir / f"r{rep}" / f"u{user}" / model / f"label{ci // args.chunk_size:03d}-r{rep}.json"
                    jobs.append((rep, user, model, lqs[ci:ci + args.chunk_size], trace, True, None))

    print(f"Questions per user: { {u: len(questions[u]) for u in args.users} } "
          f"(text-answer: { {u: len(label_questions[u]) for u in args.users} })")
    print(f"Models: {', '.join(args.models)} | repeats: {args.repeats}")
    print("Domain routing: D1 data path" if args.routing == "d1" else "Domain routing: chatbot's own")
    if args.retry_failed:
        todo = retry_jobs(jobs)
        n_q = sum(len(j[3]) for j in todo)
        print(f"Retry: {n_q} failed question(s) in {len(todo)} finished chunk(s) "
              f"({sum(1 for j in jobs if j[4].exists())} of {len(jobs)} chunks finished)")
    else:
        todo = [j for j in jobs if not j[4].exists() or (args.redo_network_errors and has_network_errors(j[4]))]
        n_q = sum(len(j[3]) for j in todo)
        print(f"Chunks: {len(jobs)} total, {len(jobs) - len(todo)} already done, {len(todo)} to run "
              f"({n_q} question-runs)")
    if args.dry_run or not todo:
        if not todo and not args.dry_run:
            print("Nothing left to run.")
        else:
            return 0

    env = dict(os.environ)
    env.setdefault("OPENAI_TIMEOUT_SECONDS", "180")
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"  # chunk logs show progress live, so a stall is visible
    lock, done, started = threading.Lock(), [0], time.time()
    stop = threading.Event()
    live: set[subprocess.Popen] = set()
    streak = [0]  # consecutive real failures

    def run(job):
        rep, user, model, qs, trace, label_mode, indices = job
        if stop.is_set():
            return "skipped"
        trace.parent.mkdir(parents=True, exist_ok=True)
        tmp = trace.with_suffix(".retry.partial.json" if indices else ".partial.json")
        tmp.unlink(missing_ok=True)
        cmd = [sys.executable, str(APP_ROOT / "run_phia_all.py"), CHILD_FLAG, args.routing,
               "--user-id", user,
               "--models", model, "--clock", "referenced-data",
               "--stored-speed-unit", "m/s", "--question-speed-unit", "m/min",
               "--label-answer" if label_mode else "--answer-only", "--trace-output", str(tmp)]
        for q in qs:
            cmd += ["--question", q]
        log_path = trace.with_suffix(".retry.log" if indices else ".log")
        with log_path.open("w", encoding="utf-8") as log:
            proc = subprocess.Popen(cmd, cwd=APP_ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL)
            with lock:
                live.add(proc)
            code, stalled = wait_or_stall(proc, log_path, args.stall_minutes * 60)
            with lock:
                live.discard(proc)
        if stalled and not stop.is_set():
            # No output for --stall-minutes: a hung call, not a slow answer. The
            # chunk counts as failed and runs again on the next invocation.
            tmp.unlink(missing_ok=True)
            with lock:
                streak[0] += 1
                print(f"r{rep} u{user} {model} {trace.stem}: STALLED (no output for {args.stall_minutes} min, "
                      f"stopped; see {log_path})", flush=True)
            return "failed"
        if stop.is_set() or code in INTERRUPT_CODES:
            stop.set()
            tmp.unlink(missing_ok=True)
            return "interrupted"
        ok = tmp.exists()
        if ok and indices:
            # Put the re-asked answers back in the same positions; everything else is untouched.
            new = json.loads(tmp.read_text(encoding="utf-8"))
            records = json.loads(trace.read_text(encoding="utf-8"))
            ok = len(new) == len(indices)
            if ok:
                for i, rec in zip(indices, new):
                    records[i] = rec
                merged = trace.with_suffix(".merge.partial.json")
                merged.write_text(json.dumps(records, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
                merged.replace(trace)
            tmp.unlink(missing_ok=True)
        elif ok:
            tmp.replace(trace)  # only a complete trace counts as done
        with lock:
            streak[0] = 0 if ok else streak[0] + 1
            if ok:
                done[0] += len(qs)
            elapsed = time.time() - started
            eta = (elapsed / done[0] * (n_q - done[0]) / 3600) if done[0] else float("nan")
            log_name = trace.with_suffix(".retry.log" if indices else ".log")
            status = (f"ok (retried {len(qs)})" if indices else "ok") if ok else f"FAILED (exit {code}, see {log_name})"
            print(f"[{done[0]}/{n_q} | ETA {eta:.1f} h] r{rep} u{user} {model} {trace.stem}: {status}", flush=True)
            if streak[0] >= MAX_FAIL_STREAK and not stop.is_set():
                print(f"\n{MAX_FAIL_STREAK} chunks failed in a row; stopping. Check the latest .log file.", flush=True)
                stop.set()
        return "ok" if ok else "failed"

    def kill_all():
        stop.set()
        with lock:
            procs = list(live)
        for proc in procs:
            try:
                proc.kill()
            except OSError:
                pass

    failed = 0
    if todo and not args.dry_run:
        pool = ThreadPoolExecutor(max_workers=args.workers or len(args.models))
        futures = {pool.submit(run, j) for j in todo}
        try:
            while futures:
                finished, futures = wait(futures, timeout=1, return_when=FIRST_COMPLETED)
                failed += sum(f.result() == "failed" for f in finished)
                if stop.is_set():
                    kill_all()
        except KeyboardInterrupt:
            kill_all()
        finally:
            pool.shutdown(wait=True, cancel_futures=True)
        if stop.is_set():
            print("\nStopped. Finished chunks are kept; run the same command again to resume.", flush=True)
            return 1

    remaining = [j for j in jobs if not j[4].exists()]
    if remaining:
        print(f"\n{len(remaining)} chunk(s) have no trace yet ({failed} failed this run). "
              "Run the same command again to finish them; grading uses what exists now.")
    print("\nGrading...")
    subprocess.run([sys.executable, str(APP_ROOT / "grade_phia_traces.py"), str(args.workbook),
                    str(args.out_dir / "**" / "*-r*.json"), "--output", str(args.csv)], cwd=APP_ROOT)
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == CHILD_FLAG:
        raise SystemExit(child_main(sys.argv[2], sys.argv[3:]))
    raise SystemExit(main())
