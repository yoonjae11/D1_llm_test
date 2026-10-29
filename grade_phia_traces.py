"""Grade local_llm_test.py trace JSON files against the PHIA objective workbook.

Scoring uses the number ADX computed from the model's KQL (trace ``query_result``),
not the wording of the natural-language answer.

Rules
- Gold = "수정된정답" if present, else "기존 PHIA 정답".
- Gold "NULL"  -> correct when the query ran and returned no value.
- Gold number  -> correct when |pred - gold| <= tolerance (default 0.01: gold values are
                  rounded to 2 decimals, so a last-digit difference still counts as correct).
- Gold label   -> (e.g. "Run") correct when a returned text value matches, case-insensitive.
- Speed        -> the model reports m/s, gold is m/min: predicted speed is multiplied by 60.
                  A value is treated as speed when its Unit is m/s (METER_PER_SECOND), or,
                  when no unit is returned, when the asked quantity (the part of the question
                  before "on days / when / that / where / during") mentions speed.
- The workbook has no user column: rows 2-1001 = 465, 1002-2001 = 171,
  2002-3001 = 333, 3002-4001 = 41.

Usage
    python grade_phia_traces.py <PHIA.xlsx> evaluation-output\\run1.json [more.json ...]
    python grade_phia_traces.py <PHIA.xlsx> evaluation-output\\*.json --output evaluation-output\\graded.csv
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import math
import re
from collections import defaultdict
from pathlib import Path

import openpyxl

QUESTION_COL, PHIA_COL, CORRECTED_COL, TYPE_COL = "Question", "기존 PHIA 정답", "수정된정답", "질문유형"
USER_IDS = ("465", "171", "333", "41")
ROWS_PER_USER = 1000
MPS_TO_MPM = 60.0
SPEED_UNITS_MPS = {"METER_PER_SECOND", "M/S", "MPS", "METERS_PER_SECOND"}
SPEED_UNITS_MPM = {"METER_PER_MINUTE", "M/MIN"}
_TARGET_SPLIT = re.compile(r"\b(on days|on the days|when|that|which were|where|during days|while)\b", re.I)
_NUM = (int, float)
LABEL_MARK = "제외"
STATUS_WORDS = {"NULL": "null", "ERROR": "error", "NO_QUERY": "no_query",
                "CLARIFICATION": "no_query", "UNPARSEABLE": "ambiguous", "NO_MODEL": "error"}


def split_labels(text) -> set:
    parts = re.split(r",|;|\n|\band\b|/", str(text or ""), flags=re.I)
    return {re.sub(r"[^a-z0-9 ]", "", p.lower()).strip() for p in parts} - {""}


def predict_label(record):
    """Label questions are graded on the short text answer (answer_mode=label)."""
    if record.get("execution_status") == "error":
        return "error", None, str(record.get("error") or "")[:200]
    answer = " ".join(str(record.get("answer") or "").split())
    status = STATUS_WORDS.get(answer.upper())
    if status:
        return status, None, f"answer {answer}"
    return "label_text", answer, ""


def norm_q(text) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().lower()


def load_gold(path: Path) -> dict:
    ws = openpyxl.load_workbook(path, read_only=True, data_only=True).worksheets[0]
    rows = ws.iter_rows(values_only=True)
    header = [str(h).strip() if h is not None else "" for h in next(rows)]
    qi, pi, ci = header.index(QUESTION_COL), header.index(PHIA_COL), header.index(CORRECTED_COL)
    ti = header.index(TYPE_COL) if TYPE_COL in header else None
    gold = {}
    for offset, row in enumerate(rows):
        excel_row = offset + 2
        block = offset // ROWS_PER_USER
        if block >= len(USER_IDS) or not row[qi]:
            continue
        corrected, phia = row[ci], row[pi]
        # "제외(정답이 수치가 아님)" marks a text-answer question: the gold is the PHIA label.
        is_label = isinstance(corrected, str) and corrected.strip().startswith(LABEL_MARK)
        value = phia if is_label else corrected if corrected not in (None, "") else phia
        key = (USER_IDS[block], norm_q(row[qi]))
        # Duplicate questions keep every Excel row: one answer is copied to each of them.
        gold.setdefault(key, []).append({"excel_row": excel_row, "gold": value, "label": is_label,
                                         "qtype": row[ti] if ti is not None else ""})
    return gold


def gold_kind(value):
    if value is None or (isinstance(value, str) and value.strip().upper() in ("", "NULL")):
        return "null", None
    if isinstance(value, _NUM) and not isinstance(value, bool):
        return "number", float(value)
    try:
        return "number", float(str(value).replace(",", ""))
    except ValueError:
        return "label", str(value).strip()


def parse_rows(query_result):
    if query_result is None:
        return None
    data = query_result
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            return None
    if isinstance(data, dict):
        data = data.get("rows", [data])
    return [r for r in data if isinstance(r, dict)] if isinstance(data, list) else None


def row_value(row):
    """Return (value, unit) for one result row."""
    unit = str(row.get("Unit") or "").strip().upper()
    if "Value" in row:
        return row["Value"], unit
    nums = [v for k, v in row.items() if isinstance(v, _NUM) and not isinstance(v, bool)
            and k not in ("EvidenceRows", "SourceCount")]
    return (nums[0] if len(nums) == 1 else None), unit


def asks_speed(question: str) -> bool:
    return "speed" in _TARGET_SPLIT.split(question, maxsplit=1)[0].lower()


def predict(record):
    """Return (kind, value, note). kind: number | null | label_values | error | no_query | ambiguous."""
    if record.get("execution_status") == "error":
        return "error", None, str(record.get("error") or "")[:200]
    rows = parse_rows(record.get("query_result"))
    if rows is None:
        return "no_query", None, "no query result (clarification or planning failure)"
    values = [(row_value(r), r) for r in rows]
    present = [(v, u, r) for (v, u), r in values if v is not None and not (isinstance(v, float) and math.isnan(v))]
    if not present:
        return "null", None, "query returned no value"
    texts = [str(v) for v, _, _ in present if isinstance(v, str)]
    texts += [str(x) for _, _, r in present for x in r.values() if isinstance(x, str)]
    nums = [(float(v), u) for v, u, _ in present if isinstance(v, _NUM) and not isinstance(v, bool)]
    if len(nums) > 1:
        return "ambiguous", None, f"{len(nums)} numeric rows returned"
    if not nums:
        return "label_values", texts, ""
    value, unit = nums[0]
    note = ""
    if unit in SPEED_UNITS_MPS or (not unit and asks_speed(record.get("question", ""))):
        value, note = value * MPS_TO_MPM, f"speed m/s x60 -> m/min (unit={unit or 'none'})"
    elif unit in SPEED_UNITS_MPM:
        note = "speed already m/min"
    return "number", value, note


def grade(gold_value, pred_kind, pred_value, tol):
    kind, gold = gold_kind(gold_value)
    if pred_kind in ("error", "no_query", "ambiguous"):
        return "X"
    if kind == "null":
        return "O" if pred_kind == "null" else "X"
    if kind == "number":
        return "O" if pred_kind == "number" and abs(pred_value - gold) <= tol + 1e-9 else "X"
    if pred_kind == "label_text":
        return "O" if split_labels(pred_value) == split_labels(gold) else "X"
    want = gold.lower()
    return "O" if pred_kind == "label_values" and any(t.strip().lower() == want for t in pred_value) else "X"


# CSV header -> key in the trace record's "timings" (milliseconds or counts).
# Per-stage times, input tokens, time to first output and retry details stay in
# the trace JSON ("timings"); summarize_phia_results.py builds the model table.
TIMING_COLUMNS = {
    "Total Time (s)": "total_ms",
    "LLM Inference Time (s)": "llm_inference_ms",    # server time minus the model's minimal-request server time
    "LLM Wait Time (s)": "llm_wait_ms",              # DNS/TCP/TLS setup + fixed server overhead + retry back-off
    "ADX Time (s)": "adx_ms",                        # all ADX queries incl. schema inventory
    "Output Tokens": "llm_output_tokens",
    "Reasoning Tokens": "llm_reasoning_tokens",
    "LLM Calls": "llm_calls",
}
_COUNT_KEYS = ("llm_calls", "llm_output_tokens", "llm_reasoning_tokens")

COLUMNS = ["Repeat", "User ID", "Question", "Question Type", "Gold Answer", "Retrieved Value",
           "Correct (o/x)", "LLM Model", "Generated KQL", "Failure Reason"] + list(TIMING_COLUMNS)


def repeat_of(path: Path) -> int:
    """Repeat number from the file name: ...-r3.json -> 3 (default 1)."""
    m = re.search(r"[-_]r(\d+)(?=\D*$)", path.stem, re.I)
    return int(m.group(1)) if m else 1


def failure_reason(gold_value, kind, value, note) -> str:
    gk, _ = gold_kind(gold_value)
    if kind == "error":
        return f"Execution error: {note}"
    if kind == "no_query":
        return "No query executed (clarification or planning failure)"
    if kind == "ambiguous":
        return f"Non-single-value result ({note})"
    if gk == "null":
        return "Returned a value but gold is NULL"
    if kind == "null":
        return "Empty result but gold has a value"
    if gk == "label":
        return "Label mismatch"
    if kind == "label_values":
        return "Non-numeric result"
    return "Value mismatch" + (f" ({note})" if note else "")


def timing_cells(record) -> list:
    """Seconds per stage from the trace's evaluation timings (blank if absent)."""
    timings = record.get("timings") or {}
    cells = []
    for key in TIMING_COLUMNS.values():
        value = timings.get(key)
        cells.append("" if value is None else value if key in _COUNT_KEYS
                     else round(value / 1000.0, 2))
    return cells


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("workbook", type=Path)
    ap.add_argument("traces", nargs="+", help="trace JSON files (wildcards allowed); name them ...-r1.json ... -r5.json")
    ap.add_argument("--tolerance", type=float, default=0.01)
    ap.add_argument("--output", type=Path, default=Path("evaluation-output/graded.csv"))
    args = ap.parse_args()

    gold = load_gold(args.workbook)
    paths = sorted({Path(p) for pattern in args.traces for p in (glob.glob(pattern, recursive=True) or [pattern])
                    if ".partial" not in Path(p).name})
    out, unmatched, skipped_mode = [], [], 0
    # Group runs per (repeat, model, user, question, mode) in run order, then pair the
    # k-th run with the k-th workbook row holding that question. If a question was run
    # fewer times than it appears (older de-duplicated runs), the last run is reused.
    groups: dict = defaultdict(list)
    for path in paths:
        rep = repeat_of(path)
        for rec in json.loads(path.read_text(encoding="utf-8")):
            key = (str(rec.get("user_id", "")).strip(), norm_q(rec.get("question")))
            entries = gold.get(key)
            if not entries:
                unmatched.append((path.name, rec.get("model"), key[0], rec.get("question")))
                continue
            label_record = rec.get("answer_mode") == "label"
            if entries[0]["label"] != label_record:
                skipped_mode += 1  # e.g. a text question asked in numeric mode: not graded
                continue
            groups[(rep, rec.get("model"), key)].append(rec)

    extra_runs = 0
    for (rep, model, key), recs in groups.items():
        entries = gold[key]
        extra_runs += max(0, len(recs) - len(entries))
        for i, g in enumerate(entries):
            rec = recs[min(i, len(recs) - 1)]
            kind, value, note = predict_label(rec) if g["label"] else predict(rec)
            shown = (round(value, 4) if kind == "number" else
                     "NULL" if kind == "null" else
                     value[:120] if kind == "label_text" else
                     "|".join(value)[:80] if kind == "label_values" else "")
            result = grade(g["gold"], kind, value, args.tolerance)
            out.append((g["excel_row"], dict(zip(COLUMNS, [
                rep, key[0], rec.get("question"), g["qtype"], g["gold"], shown, result.lower(),
                model, rec.get("kql") or "",
                "" if result == "O" else failure_reason(g["gold"], kind, value, note),
                *timing_cells(rec)]))))

    # Same order as the workbook, per repeat and model.
    out = [row for _, row in sorted(out, key=lambda t: (t[1]["Repeat"], t[1]["LLM Model"], t[0]))]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(out)

    # Console summary: accuracy per model, per repeat and overall.
    stats = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    for r in out:
        cell = stats[r["LLM Model"]][r["Repeat"]]
        cell[0] += r["Correct (o/x)"] == "o"
        cell[1] += 1
    reps = sorted({r["Repeat"] for r in out})
    print("\n" + f"{'model':<26}" + "".join(f"{'r'+str(k):>8}" for k in reps) + f"{'overall':>9}")
    for model, by_rep in sorted(stats.items()):
        total_o = sum(c[0] for c in by_rep.values())
        total_n = sum(c[1] for c in by_rep.values())
        cells = "".join(f"{(by_rep[k][0] / by_rep[k][1]) if by_rep[k][1] else 0:>8.1%}" for k in reps)
        print(f"{model:<26}{cells}{total_o / max(total_n, 1):>9.1%}")
    print(f"\nCSV: {args.output.resolve()}  ({len(out)} rows)")
    if extra_runs:
        print(f"Ignored {extra_runs} surplus run(s) (more runs than workbook rows for a question).")
    if skipped_mode:
        print(f"Skipped {skipped_mode} record(s) asked in the other answer mode (numeric vs text question).")
    if unmatched:
        print(f"\n{len(unmatched)} trace record(s) not found in the workbook (check user id / question text):")
        for u in unmatched[:10]:
            print("  ", u)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
