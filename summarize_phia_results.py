"""Build the per-model summary table from a graded PHIA CSV (and its traces).

One row per model: accuracy (overall and per question type), median and mean
times (total, LLM inference, LLM wait, ADX), mean output/reasoning tokens and
LLM calls, output tokens per inference second (sum of output tokens / sum of
inference time) and the model's median server baseline (minimal-request server
time). Every column name says whether it is a median, a mean or a sum ratio.

Usage (from this folder, venv active):
    python summarize_phia_results.py evaluation-output/phia_results.csv
    python summarize_phia_results.py evaluation-output/phia_results.csv \
        --traces "evaluation-output/phia/**/*-r*.json"
Writes <csv>_summary.csv and <csv>_summary.md next to the graded CSV.
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import statistics
from collections import defaultdict
from pathlib import Path

# Experiment design: operating baseline, base <-> lightweight pairs, references.
MODEL_GROUPS = {
    "gpt-5": ("운영 기준선", "Sensei 운영 모델"),
    "gpt-6-sol": ("GPT-6", "기본"), "gpt-6-luna": ("GPT-6", "경량"),
    "claude-opus-5-5": ("Claude 5.5", "기본"), "claude-haiku-5-5": ("Claude 5.5", "경량"),
    "deepseek-v4-pro": ("DeepSeek V4", "기본"), "deepseek-v4-flash": ("DeepSeek V4", "경량"),
    "grok-4-1-fast-reasoning": ("grok 4.1 fast", "추론 켬"),
    "grok-4-1-fast-non-reasoning": ("grok 4.1 fast", "추론 끔"),
    "kimi-k2-6": ("참고", "경량판 없음"), "llama-4-maverick": ("참고", "경량판 없음"),
}
GROUP_ORDER = ["운영 기준선", "GPT-6", "Claude 5.5", "DeepSeek V4", "grok 4.1 fast", "참고"]
QUESTION_TYPES = ["Summary-only", "Exercise-only", "Cross"]
TIME_COLUMNS = [("전체 시간", "Total Time (s)"), ("순수 추론 시간", "LLM Inference Time (s)"),
                ("LLM 대기 시간", "LLM Wait Time (s)"), ("ADX 시간", "ADX Time (s)")]


def number(value):
    try:
        return float(value) if value not in (None, "") else None
    except ValueError:
        return None


def median(values):
    values = [v for v in values if v is not None]
    return round(statistics.median(values), 2) if values else None


def mean(values, digits=1):
    values = [v for v in values if v is not None]
    return round(sum(values) / len(values), digits) if values else None


def trace_totals(pattern: str) -> dict[str, dict]:
    """Per-model server baselines, which live only in the trace JSON."""
    totals: dict[str, dict] = defaultdict(lambda: {"baseline": []})
    for path in glob.glob(pattern, recursive=True):
        if path.endswith(".partial.json"):
            continue
        try:
            records = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for record in records if isinstance(records, list) else []:
            timings = record.get("timings") or {}
            model = record.get("model")
            if not model:
                continue
            if timings.get("llm_baseline_ms") is not None:
                totals[model]["baseline"].append(timings["llm_baseline_ms"] / 1000.0)
    return totals


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv", type=Path, help="graded CSV from grade_phia_traces.py / run_phia_all.py")
    ap.add_argument("--traces", help="trace glob, e.g. \"evaluation-output/phia/**/*-r*.json\"")
    args = ap.parse_args()

    rows = list(csv.DictReader(args.csv.open(encoding="utf-8-sig")))
    by_model: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_model[row["LLM Model"]].append(row)
    traces = trace_totals(args.traces) if args.traces else {}

    table = []
    for model, items in by_model.items():
        group, role = MODEL_GROUPS.get(model, ("기타", ""))
        correct = [r["Correct (o/x)"] == "o" for r in items]
        gen = [number(r.get("LLM Inference Time (s)")) for r in items]
        out = [number(r.get("Output Tokens")) for r in items]
        paired = [(g, o) for g, o in zip(gen, out) if g and o]
        speed = round(sum(o for _, o in paired) / sum(g for g, _ in paired), 1) if paired else None
        line = {
            "그룹": group, "구분": role, "모델": model, "문항 수": len(items),
            "정답률(%)": round(100 * sum(correct) / len(items), 1),
        }
        for qtype in QUESTION_TYPES:
            subset = [r["Correct (o/x)"] == "o" for r in items if r.get("Question Type") == qtype]
            line[f"{qtype.split('-')[0]} 정답률(%)"] = round(100 * sum(subset) / len(subset), 1) if subset else None
        # Times: median (robust to rare network/retry spikes) and mean (includes them).
        for label, column in TIME_COLUMNS:
            values = [number(r.get(column)) for r in items]
            line[f"{label} 중앙값(s)"] = median(values)
            line[f"{label} 평균(s)"] = mean(values, 2)
        line.update({
            "출력 토큰 평균": mean(out),
            "추론 토큰 평균": mean(number(r.get("Reasoning Tokens")) for r in items),
            "LLM 호출 수 평균": mean(number(r.get("LLM Calls")) for r in items),
            "추론 처리 속도(출력 tokens/s, 전체 합 기준)": speed,
        })
        t = traces.get(model)
        line["서버 기준값 중앙값(s, 호출당)"] = median(t["baseline"]) if t else None
        table.append(line)

    table.sort(key=lambda r: (GROUP_ORDER.index(r["그룹"]) if r["그룹"] in GROUP_ORDER else 99, r["모델"]))
    if not table:
        print("No rows.")
        return 1
    headers = list(table[0])
    out_csv = args.csv.with_name(args.csv.stem + "_summary.csv")
    with out_csv.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        writer.writerows(table)
    show = lambda v: "-" if v is None else str(v)
    md = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    md += ["| " + " | ".join(show(r[h]) for h in headers) + " |" for r in table]
    out_md = args.csv.with_name(args.csv.stem + "_summary.md")
    out_md.write_text("\n".join(md) + "\n", encoding="utf-8")
    print("\n".join(md))
    print(f"\nSaved: {out_csv}\n       {out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
