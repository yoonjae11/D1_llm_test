# -*- coding: utf-8 -*-
"""LangGraph node for threshold-alert and scheduled-summary configuration.

Lets users SET / CLEAR / LIST personal vital-sign alerts conversationally
("alert me if my heart rate goes above 120", "notify me if my heart rate is 40 or
lower", "what alerts do I have?") instead of the /set_threshold command — which
still works
(this is purely additive). Reuses the threshold parser/formatter
(chatbot/alert_thresholds.py) and storage (OperationalTableRepository), plus the
pending_clarification state for the multi-turn "for how many minutes?" follow-up.
"""

from __future__ import annotations

import json
import math
from typing import Any, Dict, Optional

from langchain_core.messages import AIMessage
from langchain_core.output_parsers import StrOutputParser

from chatbot.graph.state import AgentState
from chatbot.i18n import message
from chatbot.prompts import load_chat_prompt
from chatbot.services.memory import create_operational_storage
from chatbot.alert_thresholds import (
    format_threshold_line,
    metric_label,
    resolve_metric,
    supported_metrics_help,
)
from chatbot.nodes.common.support import (
    clean_model_text as _clean_model_text,
    legacy_debug as _debug,
    effective_history as _effective_history,
    latest_question as _latest_question,
    llm,
    safe_json_loads as _safe_json_loads,
)


_EXTRACT_PROMPT = load_chat_prompt(
    "operational/alert_config_responder.txt"
)


def _to_minutes(value: Any, unit: Any) -> Optional[int]:
    """Normalize a duration to whole minutes (floor 1). None if not a number."""
    try:
        v = float(value)
    except Exception:
        return None
    if v <= 0:
        return None
    u = str(unit or "minute").strip().lower()
    if u in {"s", "sec", "secs", "second", "seconds"}:
        minutes = v / 60.0
    elif u in {"h", "hr", "hrs", "hour", "hours"}:
        minutes = v * 60.0
    else:  # minutes (default)
        minutes = v
    return max(1, int(math.ceil(minutes)))


def _num(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except Exception:
        return None


def _msg(answer: str, pending: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    out: Dict[str, Any] = {"messages": [AIMessage(content=answer)]}
    # Always include pending_clarification so persistence stores (or clears) it.
    out["pending_clarification"] = pending if isinstance(pending, dict) else {}
    out["last_result_summary"] = json.dumps(
        {"intent": "alert_config", "answer": answer}, ensure_ascii=False
    )
    return out


def alert_config_responder(state: AgentState):
    """Set / clear / list a vital-sign alert from natural language, asking a
    follow-up question (via pending_clarification) when required info is missing."""
    _debug("\n[Node] Alert Config Responder")
    question = _latest_question(state)
    is_korean = False
    app_user_id = str(state.get("user_id") or "").strip()

    pending = state.get("pending_clarification") if isinstance(state.get("pending_clarification"), dict) else {}
    prior_spec = pending.get("spec") if isinstance(pending.get("spec"), dict) else {}

    # 1) Extract / merge the spec with the LLM.
    spec: Dict[str, Any] = {}
    try:
        chain = _EXTRACT_PROMPT | llm | StrOutputParser()
        raw = chain.invoke({
            "pending_spec": json.dumps(prior_spec, ensure_ascii=False) if prior_spec else "(none)",
            "history": _effective_history(state, max_turns=4),
            "question": question,
        })
        parsed = _safe_json_loads(_clean_model_text(raw), default={})
        if isinstance(parsed, dict):
            spec = parsed
    except Exception as exc:
        _debug(f"  -> alert_config extraction failed: {exc}")

    # Merge LLM output over the prior partial spec (LLM was told to merge, but be safe).
    merged = dict(prior_spec)
    for key in ("target", "action", "metric", "lower", "upper", "duration_value", "duration_unit", "time", "unsupported"):
        val = spec.get(key)
        if val not in (None, "", []):
            merged[key] = val

    action = str(merged.get("action") or "set").strip().lower()
    if action not in {"set", "clear", "list"}:
        action = "set"

    repo = None
    try:
        repo = create_operational_storage(fail_soft=True)
    except Exception as exc:
        _debug(f"  -> alert_config storage unavailable: {exc}")

    # Daily-summary schedule (set / clear / show) — distinct from vital thresholds.
    if str(merged.get("target") or "threshold").strip().lower() == "summary":
        return _handle_summary(repo, app_user_id, action, merged, is_korean, question=question)

    # 2) LIST
    if action == "list":
        return _msg(_list_alerts(repo, app_user_id, is_korean), pending={})

    # 3) CLEAR
    if action == "clear":
        return _msg(_clear_alert(repo, app_user_id, merged, is_korean), pending={})

    # 4) SET
    unsupported = str(merged.get("unsupported") or "").strip()
    metric_token = str(merged.get("metric") or "").strip()
    metric = resolve_metric(metric_token) if metric_token else None

    if unsupported and not metric:
        names = supported_metrics_help(is_korean)
        answer = message(
            "alert.unsupported_metric",
            is_korean=is_korean,
            unsupported=unsupported,
            names=names,
        )
        return _msg(answer, pending={})

    lower = _num(merged.get("lower"))
    upper = _num(merged.get("upper"))
    sustain = _to_minutes(merged.get("duration_value"), merged.get("duration_unit"))

    # Persist the running spec so a follow-up answer merges with it.
    running_spec = {
        "action": "set", "metric": metric or metric_token,
        "lower": lower, "upper": upper,
        "duration_value": merged.get("duration_value"), "duration_unit": merged.get("duration_unit"),
    }

    def _ask(qtext: str) -> Dict[str, Any]:
        return _msg(qtext, pending={"type": "alert_config", "spec": running_spec, "question": qtext})

    # Missing metric -> ask which one.
    if not metric:
        names = supported_metrics_help(is_korean)
        return _ask(message("alert.ask.metric", is_korean=is_korean, names=names))

    label = metric_label(metric, is_korean)

    # Missing both bounds -> ask the condition.
    if lower is None and upper is None:
        return _ask(message("alert.ask.bound", is_korean=is_korean, label=label))

    if lower is not None and upper is not None and lower >= upper:
        return _ask(message("alert.ask.invalid_bounds", is_korean=is_korean, lower=lower, upper=upper))

    # Missing duration -> ask how long.
    if sustain is None:
        return _ask(message("alert.ask.duration", is_korean=is_korean, label=label))

    # Complete -> save.
    answer = _save_alert(repo, app_user_id, metric, lower, upper, sustain, merged, is_korean)
    return _msg(answer, pending={})


def _save_alert(repo, app_user_id, metric, lower, upper, sustain, merged, is_korean) -> str:
    line = format_threshold_line(metric, lower, upper, sustain, is_korean)
    if repo is None or not app_user_id:
        return message("alert.storage.unavailable", is_korean=is_korean)
    try:
        from chatbot.operational_storage import MetricAlertThreshold
        ok = repo.save_alert_threshold(MetricAlertThreshold(
            app_user_id=app_user_id, metric_type=metric,
            lower=lower, upper=upper, sustain_minutes=sustain, enabled=True,
        ))
    except Exception as exc:
        _debug(f"  -> alert_config save failed: {exc}")
        ok = False
    if not ok:
        return message("alert.save.failed", is_korean=is_korean)

    # Note when a sub-minute duration was rounded up, and the sync-cadence caveat.
    raw_unit = str(merged.get("duration_unit") or "").lower()
    note = ""
    if raw_unit in {"s", "sec", "secs", "second", "seconds"}:
        note = message("alert.duration.rounding_note", is_korean=is_korean)
    header = message("alert.save.header", is_korean=is_korean)
    return header + line + note


def _fmt_summary_times(times, is_korean: bool) -> str:
    if not times:
        return message("alert.none", is_korean=is_korean)
    return ", ".join(f"{int(h):02d}:{int(m):02d}" for (h, m) in times)


def _handle_summary(
    repo, app_user_id: str, action: str, merged: Dict[str, Any], is_korean: bool, question: str = ""
) -> Dict[str, Any]:
    """Set / clear / show the scheduled daily-summary digest via natural language.
    Supports multiple times: set = ADD, clear = remove one / all, list = show all."""
    if repo is None or not app_user_id:
        return _msg(message("alert.storage.unavailable", is_korean=is_korean), pending={})

    try:
        from chatbot.alert_thresholds import parse_clock_time, round_to_5
        time_parsed = parse_clock_time(str(merged.get("time") or ""))
    except Exception:
        time_parsed = None
        round_to_5 = lambda h, m: (h, m)  # noqa: E731 (defensive fallback)
    wants_all = any(tok in str(question or "").lower()
                    for tok in ("all", "every", "everything"))

    # ---- CLEAR: remove one / all ----
    if action == "clear":
        current = repo.get_user_profile(app_user_id).summary_report_times
        if not current:
            return _msg(message("alert.summary.none", is_korean=is_korean), pending={})
        if wants_all or (time_parsed is None and len(current) <= 1):
            repo.clear_summary_times(app_user_id)
            return _msg(message("alert.summary.cleared_all", is_korean=is_korean), pending={})
        if time_parsed is None:
            joined = _fmt_summary_times(current, is_korean)
            q = message("alert.summary.clear.ask", is_korean=is_korean, joined=joined)
            return _msg(q, pending={"type": "alert_config", "spec": {"target": "summary", "action": "clear"}, "question": q})
        h, m = round_to_5(*time_parsed)
        res = repo.remove_summary_time(app_user_id, h, m)
        left = _fmt_summary_times(res.get("times") or [], is_korean)
        if res.get("removed"):
            return _msg(message("alert.summary.removed", is_korean=is_korean, hour=h, minute=m, left=left), pending={})
        return _msg(message("alert.summary.not_found", is_korean=is_korean, hour=h, minute=m, left=left), pending={})

    # ---- LIST ----
    if action == "list":
        current = repo.get_user_profile(app_user_id).summary_report_times
        if not current:
            return _msg(message("alert.summary.none", is_korean=is_korean), pending={})
        joined = _fmt_summary_times(current, is_korean)
        return _msg(message("alert.summary.list", is_korean=is_korean, joined=joined), pending={})

    # ---- SET = ADD (needs a time) ----
    if time_parsed is None:
        q = message("alert.summary.ask_time", is_korean=is_korean)
        return _msg(q, pending={"type": "alert_config", "spec": {"target": "summary", "action": "set"}, "question": q})
    ohour, ominute = time_parsed
    hour, minute = round_to_5(ohour, ominute)
    rnote = (
        message("alert.summary.rounding_note", is_korean=is_korean, hour=hour, minute=minute)
        if (hour, minute) != (ohour, ominute)
        else ""
    )
    try:
        res = repo.add_summary_time(app_user_id, hour, minute)
    except Exception as exc:
        _debug(f"  -> summary add failed: {exc}")
        res = {"ok": False, "reason": "error", "times": []}
    times_txt = _fmt_summary_times(res.get("times") or [], is_korean)
    reason = res.get("reason")
    if res.get("ok"):
        return _msg(message(
            "alert.summary.added", is_korean=is_korean, hour=hour, minute=minute,
            rounding_note=rnote, times=times_txt,
        ), pending={})
    if reason == "duplicate":
        return _msg(message(
            "alert.summary.duplicate", is_korean=is_korean, hour=hour, minute=minute, times=times_txt,
        ), pending={})
    if reason == "limit":
        return _msg(message(
            "alert.summary.limit", is_korean=is_korean,
            maximum=getattr(repo, "SUMMARY_TIMES_MAX", 5), times=times_txt,
        ), pending={})
    return _msg(message("alert.summary.save_failed", is_korean=is_korean), pending={})


def _clear_alert(repo, app_user_id, merged, is_korean) -> str:
    if repo is None or not app_user_id:
        return message("alert.storage.unavailable", is_korean=is_korean)
    token = str(merged.get("metric") or "").strip().lower()
    if token in {"all", "everything"}:
        try:
            rows = [t for t in repo.list_alert_thresholds(app_user_id) if t.enabled]
            for t in rows:
                repo.delete_alert_threshold(app_user_id, t.metric_type)
        except Exception as exc:
            _debug(f"  -> alert_config clear-all failed: {exc}")
            rows = []
        return message("alert.clear.all", is_korean=is_korean)
    metric = resolve_metric(token)
    if not metric:
        names = supported_metrics_help(is_korean)
        return message("alert.clear.unknown", is_korean=is_korean, names=names)
    try:
        repo.delete_alert_threshold(app_user_id, metric)
    except Exception as exc:
        _debug(f"  -> alert_config delete failed: {exc}")
    label = metric_label(metric, is_korean)
    return message("alert.clear.metric", is_korean=is_korean, label=label)


def _list_alerts(repo, app_user_id, is_korean) -> str:
    if repo is None or not app_user_id:
        return message("alert.storage.unavailable", is_korean=is_korean)
    # Threshold alerts.
    try:
        rows = [t for t in repo.list_alert_thresholds(app_user_id) if t.enabled]
    except Exception as exc:
        _debug(f"  -> alert_config list failed: {exc}")
        rows = []
    # Scheduled daily summaries (all per-user times), shown alongside thresholds so
    # "what alerts do I have?" matches the /alerts command output.
    try:
        times = repo.get_user_profile(app_user_id).summary_report_times
    except Exception:
        times = []
    if times:
        joined = _fmt_summary_times(times, is_korean)
        summary_line = message("alert.list.summary_set", is_korean=is_korean, joined=joined)
    else:
        summary_line = message("alert.list.summary_none", is_korean=is_korean)

    if rows:
        header = message("alert.list.header", is_korean=is_korean)
        lines = [format_threshold_line(t.metric_type, t.lower, t.upper, t.sustain_minutes, is_korean) for t in rows]
        threshold_block = header + "\n" + "\n".join(lines)
    else:
        threshold_block = message("alert.list.threshold_none", is_korean=is_korean)
    return threshold_block + "\n\n" + summary_line
