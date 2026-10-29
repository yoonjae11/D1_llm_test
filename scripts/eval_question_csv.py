"""Grade the D1 sensor-data path against a question/answer CSV.

Each row's question runs through the production D1 steps (planner LLM, plan
sanitization, deterministic KQL, safety guard, ADX). The result is reduced to
one number and compared with the gold answer.

It is READ-ONLY against Azure: it only queries ADX and writes a local results
CSV. Every question costs one planner LLM call.

Input columns: ``question`` and ``answer`` (required); ``user_id`` and
``reference_date`` (optional per-row overrides of the flags).

Usage::

    python scripts/eval_question_csv.py questions.csv --user-id <app-user-id>
    python scripts/eval_question_csv.py questions.csv --user-id <id> --reference-date 2026-09-22

``--reference-date`` is the "today" that relative phrases such as "yesterday"
resolve against. Pin it to the date the gold answers were computed for;
otherwise it defaults to the current local date and the answers drift daily.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import math
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from langchain_core.messages import HumanMessage

from chatbot.config import get_settings
from chatbot.nodes.d1_sensor_data.execute_sensor_query import execute_sensor_query
from chatbot.nodes.d1_sensor_data.generate_sensor_query import generate_sensor_query
from chatbot.nodes.d1_sensor_data.guard_sensor_query import guard_sensor_query
from chatbot.nodes.d1_sensor_data.plan_sensor_query import plan_sensor_query
from chatbot.policies.domain_catalog import METRIC_DEFINITIONS
from chatbot.policies.kql_generation import metric_result_alias
from chatbot.policies.result_processing import _parse_query_result
from chatbot.policies.time_ranges import (
    _V2_MAX_ABSOLUTE_DAYS,
    EvaluationTimeContext,
    resolve_evaluation_plan_time_range,
    use_evaluation_time,
)


RESULT_COLUMNS = [
    "question", "gold", "predicted", "unit", "result", "status", "detail",
    "user_id", "reference_date", "kql",
]
_NUMBER_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?")


def _number(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def extract_scalar(plan: Dict[str, Any], rows: Any) -> Tuple[Optional[float], str, str]:
    """Reduce D1 result rows to ``(value, unit, reason_when_no_value)``."""

    if not isinstance(rows, list) or not rows:
        return None, "", "no_data"
    if len(rows) != 1 or not isinstance(rows[0], dict):
        return None, "", f"not_scalar: {len(rows)} result rows"
    row = rows[0]
    unit = str(row.get("Unit") or "")

    # Typed (schema v2) results always carry the compiled answer in ``Value``.
    if plan.get("schema_version") == 2:
        if row.get("ResultStatus") == "no_metric_evidence":
            return None, unit, "no_data"
        value = _number(row.get("Value"))
        return value, unit, "" if value is not None else "not_scalar: Value is not numeric"

    # Schema v1 result shapes differ per builder: a per-metric alias column, or
    # the generic value columns of the activity and sampled-aggregate queries.
    metrics = plan.get("metrics") if isinstance(plan.get("metrics"), list) else []
    if len(metrics) != 1:
        return None, unit, f"not_scalar: {len(metrics)} planned metrics"
    metric = str(metrics[0])
    alias = metric_result_alias(METRIC_DEFINITIONS.get(metric, {}).get("summary", ""))
    if metric.endswith("_max"):
        fallbacks = ["MaxValue"]
    elif metric.endswith("_min"):
        fallbacks = ["MinValue"]
    else:
        fallbacks = ["ValueExact", "Value"]
    for column in [alias, *fallbacks]:
        value = _number(row.get(column)) if column else None
        if value is not None:
            return value, unit, ""
    return None, unit, f"not_scalar: no numeric column for {metric}"


def is_correct(predicted: float, gold_text: str, tolerance: Optional[float]) -> Optional[bool]:
    """Compare with the gold answer; ``None`` when the gold holds no number.

    Without an explicit tolerance the prediction must round to the gold at the
    precision the gold was written with ("9" accepts 8.5-9.5, "9.0" 8.95-9.05).
    """

    match = _NUMBER_RE.search(str(gold_text or ""))
    if not match:
        return None
    gold_number = match.group().replace(",", "")
    if tolerance is None:
        tolerance = 0.5 * 10 ** -len(gold_number.partition(".")[2])
    return abs(predicted - float(gold_number)) <= tolerance + 1e-9


def answer_question(question: str, user_id: str, timezone: str) -> Dict[str, Any]:
    """Run one question through D1. Call inside ``use_evaluation_time``."""

    state: Dict[str, Any] = {
        "messages": [HumanMessage(content=question)],
        "user_id": user_id,
        "timezone_iana": timezone,
        "repair_attempts": 0,
    }
    planned = plan_sensor_query(state)
    state.update(planned)
    plan = state["plan"]
    # The planner node swallows model failures and returns a default plan;
    # the evaluation-only raw plan is how that case stays visible here.
    if not planned.get("raw_plan"):
        return {"status": "planner_failed", "detail": "planner returned no usable JSON"}
    if plan.get("execution_ready") is False:
        return {"status": "needs_clarification", "detail": str(plan.get("followup_question") or "")}

    # KQL builders resolve relative windows against ADX now(); freeze them to
    # absolute dates so the run is reproducible for the reference date.
    plan["time_range"] = resolve_evaluation_plan_time_range(plan, timezone)

    state.update(generate_sensor_query(state))
    kql = str(state.get("kql_query") or "")
    if not kql:
        return {"status": "no_query", "detail": str(state.get("error") or state.get("kql_generation_status") or "")}
    state.update(guard_sensor_query(state))
    if state.get("guard_status") != "execute":
        return {"status": "blocked", "detail": str(state.get("error") or ""), "kql": kql}
    state.update(execute_sensor_query(state))
    kql = str(state.get("kql_query") or kql)
    if state.get("error"):
        return {"status": "query_error", "detail": str(state["error"])[:300], "kql": kql}

    value, unit, reason = extract_scalar(state["plan"], _parse_query_result(state.get("query_result")))
    if value is None:
        status, _, detail = reason.partition(": ")
        return {"status": status, "detail": detail, "unit": unit, "kql": kql}
    return {"status": "answered", "predicted": value, "unit": unit, "kql": kql}


def evaluate_rows(
    rows: List[Dict[str, str]],
    *,
    question_column: str,
    answer_column: str,
    default_user_id: str,
    default_reference_date: dt.date,
    timezone: str,
    tolerance: Optional[float],
) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    for index, row in enumerate(rows, start=1):
        question = str(row.get(question_column) or "").strip()
        gold = str(row.get(answer_column) or "").strip()
        user_id = str(row.get("user_id") or "").strip() or default_user_id
        result: Dict[str, Any] = {"question": question, "gold": gold, "user_id": user_id}
        try:
            row_date = str(row.get("reference_date") or "").strip()
            reference = dt.date.fromisoformat(row_date) if row_date else default_reference_date
            result["reference_date"] = reference.isoformat()
            if not question or not user_id:
                raise ValueError("question and user_id are required")
            # Same horizon production uses for an "all available" request.
            clock = EvaluationTimeContext(
                summary_reference_date=reference,
                summary_min_date=reference - dt.timedelta(days=_V2_MAX_ABSOLUTE_DAYS - 1),
            )
            with use_evaluation_time(clock):
                result.update(answer_question(question, user_id, timezone))
        except Exception as exc:
            result.update({"status": "error", "detail": f"{type(exc).__name__}: {exc}"[:300]})

        # A question the pipeline could not answer with a number is wrong; only
        # a row whose gold answer has no number is left out of the score.
        graded = is_correct(result.get("predicted", math.nan), gold, tolerance)
        result["result"] = "-" if graded is None else "O" if graded else "X"
        if graded is None:
            result["status"] = "bad_gold"
        results.append(result)
        # Values stay in the results file; the console shows the verdict only.
        suffix = "" if result["status"] == "answered" else f" ({result['status']})"
        print(f"[{index}/{len(rows)}] {result['result']}{suffix}")
    return results


def _preflight() -> Optional[str]:
    """Fail before the first row when the LLM or ADX client cannot start."""

    try:
        from chatbot.llm_client import get_llm_client

        get_llm_client()
    except Exception as exc:
        return f"LLM is not configured: {exc}"
    try:
        from chatbot.adx_client import adx_manager
    except ImportError as exc:
        return f"ADX client is unavailable ({exc}); run: pip install -r requirements.txt"
    if adx_manager.client is None:
        return "ADX is not configured: set ADX_CLUSTER_URI (or ADX_QUERY_URI) and ADX_DATABASE"
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("csv_path", type=Path)
    parser.add_argument("--user-id", default="", help="App user id for rows without a user_id column")
    parser.add_argument("--reference-date", type=dt.date.fromisoformat, help="'Today' for the questions (YYYY-MM-DD)")
    parser.add_argument("--timezone", default=get_settings().default_timezone)
    parser.add_argument("--tolerance", type=float, help="Absolute tolerance; default rounds to the gold's precision")
    parser.add_argument("--question-column", default="question")
    parser.add_argument("--answer-column", default="answer")
    parser.add_argument("--output", type=Path, help="Results CSV (default: <input>_results.csv)")
    args = parser.parse_args()

    # utf-8-sig reads and writes the BOM Excel uses for UTF-8 CSV files.
    with args.csv_path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
        columns = reader.fieldnames or []
    missing = [name for name in (args.question_column, args.answer_column) if name not in columns]
    if missing:
        print(f"ERROR: CSV is missing column(s): {', '.join(missing)}. Found: {', '.join(columns)}")
        return 1
    if not args.user_id and "user_id" not in columns:
        print("ERROR: pass --user-id or add a user_id column.")
        return 1
    problem = _preflight()
    if problem:
        print(f"ERROR: {problem}")
        return 1

    reference = args.reference_date or dt.datetime.now(ZoneInfo(args.timezone)).date()
    print(f"Reference date: {reference.isoformat()} ({args.timezone})")
    results = evaluate_rows(
        rows,
        question_column=args.question_column,
        answer_column=args.answer_column,
        default_user_id=args.user_id,
        default_reference_date=reference,
        timezone=args.timezone,
        tolerance=args.tolerance,
    )

    output = args.output or args.csv_path.with_name(args.csv_path.stem + "_results.csv")
    with output.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=RESULT_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(results)

    correct = sum(1 for item in results if item["result"] == "O")
    graded = sum(1 for item in results if item["result"] in {"O", "X"})
    accuracy = f"{correct / graded:.1%}" if graded else "n/a"
    print(f"\nAccuracy: {correct}/{graded} = {accuracy}")
    failures: Dict[str, int] = {}
    for item in results:
        if item["status"] != "answered":
            failures[item["status"]] = failures.get(item["status"], 0) + 1
    if failures:
        print("Not answered: " + ", ".join(f"{name}={count}" for name, count in sorted(failures.items())))
    print(f"Results: {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
