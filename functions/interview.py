# -*- coding: utf-8 -*-
"""
Interview / onboarding flow.

Collects the user profile (name, age, gender, region/timezone) AND actually sets
the user up: optional vital-sign alert thresholds and an optional daily summary.
Each call receives the current step + user answer and returns the next message to
send plus an is_complete flag. Side effects (saving thresholds, the summary time)
happen immediately, so onboarding genuinely configures the system.

Conversation style (per product feedback):
  - questions are kept short and ONE thing is asked at a time;
  - each saved profile field is echoed back as a confirmation ("Name set to ...",
    "Age set to ...", "Gender set to ...", "Timezone set to ...");
  - choices are offered as numbered options answered with a number or a short word
    (yes/no, auto/manual) — interpreted leniently via chatbot.nlu (rules -> LLM);
  - an age the LLM had to INFER from typo'd/free text is confirmed with a yes/no
    (age_confirm) before it is stored; exact digits/number-words store immediately;
  - alerts are configured ONE value at a time (data type -> lower -> upper ->
    duration) instead of three numbers on one line, and each metric can be set
    AUTOmatically (cited medical reference; age/sex-aware where guidelines support
    it — e.g. the heart-rate ceiling) or MANUALly.

Step order:
  name -> age -> [age_confirm] -> gender -> timezone -> [timezone_confirm]
       -> alert_optin -> [alert_select -> per metric: alert_mode
                          -> (manual) alert_lower -> alert_upper -> alert_duration
                          -> [alert_bound_confirm for an inferred bound/duration]]
       -> summary_optin -> [summary_time]
       -> done (show /help guidance).

Robust answers: numbered choices accept "1"/"one"/"first"/"yes" and typos; bounds and
durations accept spelled-out numbers ("one hundred" -> 100). Any answer that is NOT the
plainly-designated form — a non-bare age ("thirty one"), a typo'd/free-text region
("koera"), a spelled-out bound — is echoed back for a yes/no confirmation before it is
stored, so a misread never saves silently. Bare/canonical inputs (a plain age number, a
menu number, a known city) skip the confirmation.

The name step is the only generic field step. Age and gender use dedicated
parsers and confirmation handlers, while the remaining steps own their dynamic
content and side effects explicitly in process_interview_answer().
"""

import json
import logging
from typing import Any, Dict, List, Optional, Tuple

from functions.shared import (
    get_registered_user,
    update_user_profile_fields,
    complete_interview,
    _utc_now_iso,
)

# ==========================================================================
# Step definitions
# ==========================================================================
INTERVIEW_STEPS = [
    "name", "age", "age_confirm", "gender", "gender_confirm", "timezone", "timezone_confirm",
    "alert_optin", "alert_select", "alert_mode", "alert_lower", "alert_upper", "alert_duration",
    "alert_bound_confirm",
    "summary_optin", "summary_time",
]

# Sentinel inserted between onboarding bubbles. The Telegram layer splits outgoing
# interview messages on it and sends each segment as its own message. It is always
# split out before sending, so it never reaches the user (non-printing fallback).
MESSAGE_BREAK = "\x1e"


def split_message_bubbles(text: str) -> "list[str]":
    """Split an interview message into separate Telegram bubbles on MESSAGE_BREAK.
    Returns the non-empty, stripped segments (or [""] so callers always send one)."""
    parts = [seg.strip() for seg in str(text or "").split(MESSAGE_BREAK)]
    parts = [seg for seg in parts if seg]
    return parts or [""]


# Static profile questions. Age and gender reuse this text when their dedicated
# handlers enter or resume those steps; only name uses the generic field path.
STEP_QUESTIONS: Dict[str, str] = {
    "name": "What's your name? (used for personalised replies)\n[e.g. Alex]",
    "age": "How old are you?\n[e.g. 30]",
    "gender": "Your gender? (male / female / other / prefer not to say)\n[e.g. male]",
}

STEP_FIELD_MAP: Dict[str, str] = {
    "name": "Name",
}

# "pass" to skip a step (case-insensitive); a couple of friendly synonyms too.
_PASS_TOKENS = {"pass", "skip"}


def _completion_message() -> str:
    """Short getting-started guide shown once onboarding is fully done."""
    return (
        "You're all set! 🎉\n"
        "Ask me anything about your health data — e.g. \"How many steps today?\" or "
        "\"How did I sleep last night?\".\n\n"
        "Manage alerts anytime with /alerts, /set_threshold, /set_scheduled_alert — "
        "or just tell me in chat. See /help."
    )


# ==========================================================================
# Question rendering
# ==========================================================================
def _question_for_step(step: str) -> str:
    """Render a static profile question or the dynamic timezone menu."""
    if step == "timezone":
        try:
            from chatbot.timezone_onboarding import build_menu_message
            return build_menu_message(False, intro=True)
        except Exception:
            return "Which timezone are you in? (e.g. Asia/Seoul)"
    return STEP_QUESTIONS.get(step, STEP_QUESTIONS["name"])


def _with_prefix(prefix: str, body: str) -> str:
    """Join an onboarding confirmation/notice (prefix) and the next content (body)
    as SEPARATE Telegram bubbles via MESSAGE_BREAK. No break when prefix is empty."""
    p = (prefix or "").strip()
    return (p + MESSAGE_BREAK + body) if p else body


def _alert_optin_question() -> str:
    return (
        "Set up health alerts? I'll message you when a vital goes out of range.\n"
        "1) Yes   2) No"
    )


def _alert_select_question(vitals: List[str]) -> str:
    listing = "\n".join(f"{i}) {v}" for i, v in enumerate(vitals, 1))
    return (
        "Which vitals should I watch?\n"
        f"{listing}\n\n"
        "Reply with numbers (e.g. 1 or 1,3), names, or 'all'."
    )


def _alert_mode_question(metric: str) -> str:
    label = _metric_label(metric)
    return (
        f"{label} — how should I set the alert?\n"
        "1) Auto (medical reference for your age/sex)\n"
        "2) Manual (you enter the numbers)"
    )


def _bound_hint(metric: str, app_user_id: str, which: str) -> str:
    """A standalone reference line (with explanation) for a manual bound question,
    or "" when the metric has no medical reference."""
    auto = _auto_for(metric, app_user_id)
    if not auto:
        return ""
    val = auto.get(which)
    if not isinstance(val, (int, float)):
        return ""
    unit = _metric_unit(metric)
    u = f" {unit}" if unit else ""
    return (
        f"Reference: {val:g}{u} — the medical-guideline value Auto mode would "
        "use for your age/sex.\n\n"
    )


def _alert_lower_question(metric: str, app_user_id: str = "") -> str:
    label = _metric_label(metric)
    u = _unit_suffix(metric)
    return (
        f"{label} — lower bound{u}.\n"
        "I'll alert you when it stays BELOW this.\n\n"
        f"{_bound_hint(metric, app_user_id, 'lower')}"
        "Enter a number, or '-' for no lower bound."
    )


def _alert_upper_question(metric: str, app_user_id: str = "") -> str:
    label = _metric_label(metric)
    u = _unit_suffix(metric)
    return (
        f"{label} — upper bound{u}.\n"
        "I'll alert you when it stays ABOVE this.\n\n"
        f"{_bound_hint(metric, app_user_id, 'upper')}"
        "Enter a number, or '-' for no upper bound."
    )


def _alert_duration_question(metric: str) -> str:
    label = _metric_label(metric)
    return (
        f"How long must {label} stay out of range before I alert you?\n"
        "Minutes, e.g. 10."
    )


def _summary_optin_question() -> str:
    return (
        "Want a daily summary of your collected data?\n"
        "1) Yes   2) No"
    )


def _summary_time_question() -> str:
    return (
        "What time should I send it? (your local time)\n"
        "[e.g. 09:00, 9pm] — set in 5-minute steps."
    )


def _no_vitals_guidance() -> str:
    return (
        "No alert-eligible vitals (e.g. heart rate) are syncing yet, so we'll skip "
        "alerts for now. You can add one later with /set_threshold."
    )


def _no_alert_guidance() -> str:
    return "No alerts set — you can add one anytime with /set_threshold or just ask me in chat."


# ---- profile confirmation (name / age / gender) ----
def _gender_label(value) -> str:
    return str(value or "").replace("_", " ").strip()


def _field_confirmation(step: str, value) -> str:
    """Short 'saved it' notice shown as its own bubble before the next question,
    mirroring the timezone step's 'Timezone set to ...' confirmation."""
    if step == "name":
        return f"Name set to {value}."
    if step == "age":
        return f"Age set to {value}."
    if step == "gender":
        return f"Gender set to {_gender_label(value)}."
    return ""


def _age_confirm_question(age: int) -> str:
    return (
        f"Just to confirm — did you mean age {age}?\n"
        "1) Yes   2) No"
    )


def _gender_confirm_question(gender: str) -> str:
    return f"Just to confirm — did you mean {_gender_label(gender)}?\n1) Yes   2) No"


def _bound_confirm_question(field: str, value) -> str:
    """Yes/no confirmation for a bound/duration value the parser had to INFER
    (e.g. the user typed "one hundred")."""
    try:
        shown = f"{float(value):g}"
    except Exception:
        shown = str(value)
    if field == "duration":
        return f"Just to confirm — did you mean {shown} minutes?\n1) Yes   2) No"
    label = "lower bound" if field == "lower" else "upper bound"
    return f"Just to confirm — did you mean {label} {shown}?\n1) Yes   2) No"


# ==========================================================================
# Helpers (lazy imports keep module load light + avoid circular imports)
# ==========================================================================
def _metric_label(metric: str) -> str:
    try:
        from chatbot.alert_thresholds import metric_label
        return metric_label(metric, is_korean=False)
    except Exception:
        return str(metric or "")


def _unit_suffix(metric: str) -> str:
    unit = _metric_unit(metric)
    return f" ({unit})" if unit else ""


def _metric_unit(metric: str) -> str:
    try:
        from chatbot.alert_thresholds import metric_unit
        return metric_unit(metric)
    except Exception:
        return ""


def _auto_for(metric: str, app_user_id: str) -> Optional[Dict[str, Any]]:
    """Cited auto-threshold dict for a metric using the user's age/sex, or None."""
    try:
        from chatbot.alert_thresholds import auto_threshold
        user = get_registered_user(app_user_id) or {}
        return auto_threshold(metric, user.get("Age"), user.get("Gender"))
    except Exception:
        return None


def _has_auto(metric: str) -> bool:
    try:
        from chatbot.alert_thresholds import has_auto_threshold
        return has_auto_threshold(metric)
    except Exception:
        return False


def _collected_vitals(app_user_id: str) -> List[str]:
    """ADX Type tokens the user is collecting AND that support alerts (sorted).

    Refreshes availability from ADX so the onboarding list is current; falls back
    to the cached availability map if the refresh fails.
    """
    avail: Dict[str, Any] = {}
    try:
        from chatbot.availability_refresh import refresh_and_context
        avail = refresh_and_context(app_user_id) or {}
    except Exception as exc:
        logging.warning("onboarding availability refresh failed for %s: %s", app_user_id, exc)
        try:
            from chatbot.operational_storage import OperationalTableRepository
            cached = OperationalTableRepository(fail_soft=True).get_metric_availability_map(app_user_id)
            avail = {k: {"is_available": getattr(v, "is_available", False)} for k, v in cached.items()}
        except Exception as exc2:
            logging.warning("onboarding cached availability failed for %s: %s", app_user_id, exc2)
            avail = {}
    try:
        from chatbot.alert_thresholds import eligible_alert_metrics
        eligible = set(eligible_alert_metrics())
    except Exception:
        eligible = set()
    out: List[str] = []
    for metric_type, item in avail.items():
        is_av = item.get("is_available") if isinstance(item, dict) else getattr(item, "is_available", False)
        token = str(metric_type).upper()
        if is_av and token in eligible and token not in out:
            out.append(token)
    return sorted(out)


# ---- onboarding scratch (queue of metrics + partial bounds) ----
def _set_alert_queue(app_user_id: str, metrics: List[str]) -> None:
    update_user_profile_fields(app_user_id, {"OnboardingAlertQueue": json.dumps(list(metrics))})


def _get_alert_queue(app_user_id: str) -> List[str]:
    user = get_registered_user(app_user_id) or {}
    try:
        queue = json.loads(user.get("OnboardingAlertQueue") or "[]")
        return [str(x).upper() for x in queue] if isinstance(queue, list) else []
    except Exception:
        return []


def _stored_vitals(app_user_id: str) -> List[str]:
    user = get_registered_user(app_user_id) or {}
    try:
        vitals = json.loads(user.get("OnboardingVitals") or "[]")
        return [str(x).upper() for x in vitals] if isinstance(vitals, list) else []
    except Exception:
        return []


def _set_threshold_scratch(app_user_id: str, lower, upper) -> None:
    update_user_profile_fields(app_user_id, {
        "OnboardingThresholdScratch": json.dumps({"lower": lower, "upper": upper})
    })


def _get_threshold_scratch(app_user_id: str) -> Dict[str, Any]:
    user = get_registered_user(app_user_id) or {}
    try:
        d = json.loads(user.get("OnboardingThresholdScratch") or "{}")
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _clear_threshold_scratch(app_user_id: str) -> None:
    update_user_profile_fields(app_user_id, {"OnboardingThresholdScratch": ""})


# ---- onboarding scratch (pending age awaiting yes/no confirmation) ----
def _set_pending_age(app_user_id: str, age: int) -> None:
    update_user_profile_fields(app_user_id, {"OnboardingPendingAge": str(int(age))})


def _get_pending_age(app_user_id: str) -> Optional[int]:
    user = get_registered_user(app_user_id) or {}
    raw = str(user.get("OnboardingPendingAge") or "").strip()
    try:
        return int(raw) if raw else None
    except Exception:
        return None


def _clear_pending_age(app_user_id: str) -> None:
    update_user_profile_fields(app_user_id, {"OnboardingPendingAge": ""})


# ---- onboarding scratch (an inferred gender awaiting yes/no confirmation) ----
def _set_pending_gender(app_user_id: str, gender: str) -> None:
    update_user_profile_fields(app_user_id, {"OnboardingPendingGender": str(gender)})


def _get_pending_gender(app_user_id: str) -> str:
    user = get_registered_user(app_user_id) or {}
    return str(user.get("OnboardingPendingGender") or "").strip()


def _clear_pending_gender(app_user_id: str) -> None:
    update_user_profile_fields(app_user_id, {"OnboardingPendingGender": ""})


# ---- onboarding scratch (an inferred bound/duration awaiting yes/no confirmation) ----
def _set_bound_pending(app_user_id: str, field: str, value) -> None:
    update_user_profile_fields(app_user_id, {
        "OnboardingBoundPending": json.dumps({"field": field, "value": value})
    })


def _get_bound_pending(app_user_id: str) -> Dict[str, Any]:
    user = get_registered_user(app_user_id) or {}
    try:
        d = json.loads(user.get("OnboardingBoundPending") or "{}")
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _clear_bound_pending(app_user_id: str) -> None:
    update_user_profile_fields(app_user_id, {"OnboardingBoundPending": ""})


# ---- onboarding scratch (an inferred timezone awaiting yes/no confirmation) ----
def _set_pending_tz(app_user_id: str, iana: str, site_code: str) -> None:
    update_user_profile_fields(app_user_id, {
        "OnboardingPendingTz": json.dumps({"iana": iana, "site": site_code})
    })


def _get_pending_tz(app_user_id: str) -> Dict[str, Any]:
    user = get_registered_user(app_user_id) or {}
    try:
        d = json.loads(user.get("OnboardingPendingTz") or "{}")
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_threshold(app_user_id: str, metric_type: str, lower, upper, sustain: int) -> bool:
    try:
        from chatbot.operational_storage import OperationalTableRepository, MetricAlertThreshold
        repo = OperationalTableRepository(fail_soft=True)
        return bool(repo.save_alert_threshold(MetricAlertThreshold(
            app_user_id=app_user_id,
            metric_type=metric_type,
            lower=lower,
            upper=upper,
            sustain_minutes=sustain,
            enabled=True,
        )))
    except Exception as exc:
        logging.warning("onboarding threshold save failed for %s: %s", app_user_id, exc)
        return False


def _save_summary_hour(app_user_id: str, hour: Optional[int], minute: int = 0) -> None:
    try:
        from chatbot.operational_storage import OperationalTableRepository
        OperationalTableRepository(fail_soft=True).save_user_summary_hour(app_user_id, hour, minute)
    except Exception as exc:
        logging.warning("onboarding summary hour save failed for %s: %s", app_user_id, exc)


def _confirm_line(metric_type: str, lower, upper, sustain: int) -> str:
    try:
        from chatbot.alert_thresholds import format_threshold_line
        return "Set: " + format_threshold_line(metric_type, lower, upper, sustain, is_korean=False)
    except Exception:
        return "Set."


# ==========================================================================
# Step transitions (entry) + special-step processors
# ==========================================================================
def _set_step(app_user_id: str, step: str) -> None:
    update_user_profile_fields(app_user_id, {"InterviewStep": step, "InterviewUpdatedAt": _utc_now_iso()})


def _enter_step(app_user_id: str, step: str, prefix: str = "") -> Tuple[str, bool]:
    """Move to ``step`` and return its entry message. Handles the conditional jumps:
    no collected vitals skips the whole alert block; an exhausted metric queue moves
    on to the summary opt-in; a metric with no medical reference skips the auto/manual
    question and goes straight to manual entry.
    """
    # ---- age confirmation (resume safety) ----
    if step == "age_confirm":
        # Resuming into a pending age confirmation (e.g. /start mid-flow): the pending
        # value may be gone, so re-ask age cleanly instead of a dangling yes/no.
        _set_step(app_user_id, "age")
        return _with_prefix(prefix, _question_for_step("age")).strip(), False

    # ---- gender confirmation (resume safety) ----
    if step == "gender_confirm":
        _set_step(app_user_id, "gender")
        return _with_prefix(prefix, _question_for_step("gender")).strip(), False

    # ---- timezone confirmation (resume safety) ----
    if step == "timezone_confirm":
        # Resuming into a dangling timezone confirmation: re-show the region menu.
        _set_step(app_user_id, "timezone")
        return _with_prefix(prefix, _question_for_step("timezone")).strip(), False

    # ---- bound/duration confirmation (resume safety) ----
    if step == "alert_bound_confirm":
        # Resuming into a dangling bound confirmation: re-ask the current metric's
        # lower bound (or move on if the alert queue is gone).
        queue = _get_alert_queue(app_user_id)
        if not queue:
            return _enter_step(app_user_id, "summary_optin", prefix=prefix)
        _set_step(app_user_id, "alert_lower")
        return _with_prefix(prefix, _alert_lower_question(queue[0], app_user_id)).strip(), False

    # ---- alerts: opt-in ----
    if step == "alert_optin":
        vitals = _collected_vitals(app_user_id)
        if not vitals:
            return _enter_step(app_user_id, "summary_optin",
                               prefix=(prefix + _no_vitals_guidance() + "\n\n").lstrip())
        update_user_profile_fields(app_user_id, {
            "InterviewStep": "alert_optin",
            "OnboardingVitals": json.dumps(vitals),
            "InterviewUpdatedAt": _utc_now_iso(),
        })
        return _with_prefix(prefix, _alert_optin_question()).strip(), False

    if step == "alert_select":
        vitals = _stored_vitals(app_user_id) or _collected_vitals(app_user_id)
        if not vitals:
            return _enter_step(app_user_id, "summary_optin",
                               prefix=(prefix + _no_vitals_guidance() + "\n\n").lstrip())
        _set_step(app_user_id, "alert_select")
        return _with_prefix(prefix, _alert_select_question(vitals)).strip(), False

    # ---- alerts: per-metric mode (auto/manual) ----
    if step == "alert_mode":
        queue = _get_alert_queue(app_user_id)
        if not queue:
            return _enter_step(app_user_id, "summary_optin", prefix=prefix)
        metric = queue[0]
        if not _has_auto(metric):
            # No medical reference (e.g. SPEED) -> go straight to manual entry.
            _set_step(app_user_id, "alert_lower")
            note = f"{_metric_label(metric)} has no standard medical range, so let's set it manually.\n\n"
            return _with_prefix(prefix, note + _alert_lower_question(metric, app_user_id)).strip(), False
        _set_step(app_user_id, "alert_mode")
        return _with_prefix(prefix, _alert_mode_question(metric)).strip(), False

    if step == "alert_lower":
        queue = _get_alert_queue(app_user_id)
        if not queue:
            return _enter_step(app_user_id, "summary_optin", prefix=prefix)
        _set_step(app_user_id, "alert_lower")
        return _with_prefix(prefix, _alert_lower_question(queue[0], app_user_id)).strip(), False

    if step == "alert_upper":
        queue = _get_alert_queue(app_user_id)
        if not queue:
            return _enter_step(app_user_id, "summary_optin", prefix=prefix)
        _set_step(app_user_id, "alert_upper")
        return _with_prefix(prefix, _alert_upper_question(queue[0], app_user_id)).strip(), False

    if step == "alert_duration":
        queue = _get_alert_queue(app_user_id)
        if not queue:
            return _enter_step(app_user_id, "summary_optin", prefix=prefix)
        _set_step(app_user_id, "alert_duration")
        return _with_prefix(prefix, _alert_duration_question(queue[0])).strip(), False

    # ---- summary ----
    if step == "summary_optin":
        _set_step(app_user_id, "summary_optin")
        return _with_prefix(prefix, _summary_optin_question()).strip(), False

    if step == "summary_time":
        _set_step(app_user_id, "summary_time")
        return _with_prefix(prefix, _summary_time_question()).strip(), False

    # Static profile-question entry (name/age/gender) and timezone.
    _set_step(app_user_id, step)
    return _with_prefix(prefix, _question_for_step(step)).strip(), False


def _finish(app_user_id: str, prefix: str = "") -> Tuple[str, bool]:
    complete_interview(app_user_id)
    try:  # clear onboarding scratch fields
        update_user_profile_fields(app_user_id, {
            "OnboardingAlertQueue": "", "OnboardingVitals": "", "OnboardingThresholdScratch": "",
            "OnboardingPendingAge": "", "OnboardingPendingGender": "",
            "OnboardingBoundPending": "", "OnboardingPendingTz": "",
        })
    except Exception:
        pass
    return _with_prefix(prefix, _completion_message()).strip(), True


def _advance_after_metric(app_user_id: str, confirm: str) -> Tuple[str, bool]:
    """Pop the just-configured metric and move to the next one (or the summary
    opt-in when the queue is empty)."""
    queue = _get_alert_queue(app_user_id)
    if queue:
        queue.pop(0)
        _set_alert_queue(app_user_id, queue)
    _clear_threshold_scratch(app_user_id)
    return _enter_step(app_user_id, "alert_mode", prefix=(confirm + "\n\n") if confirm else "")


def _after_timezone(app_user_id: str, prefix: str = "") -> Tuple[str, bool]:
    """After the timezone step, move into the alert opt-in step."""
    return _enter_step(app_user_id, "alert_optin", prefix=prefix)


def _tz_confirm_question(iana: str, site_code: str) -> str:
    """Yes/no confirmation for a timezone the resolver had to INFER (typo / free text)."""
    try:
        from chatbot.timezone_onboarding import SITE_LABELS_EN
        label = SITE_LABELS_EN.get(site_code, "")
    except Exception:
        label = ""
    where = label if label else iana  # site label already names the city
    return f"Just to confirm — did you mean {where}?\n1) Yes   2) No"


def _save_tz_and_advance(app_user_id: str, iana: str, site_code: str) -> Tuple[str, bool]:
    """Persist a confirmed timezone, clear the pending scratch, and move on to alerts."""
    try:
        from chatbot.timezone_onboarding import save_resolved_timezone
        save_resolved_timezone(app_user_id, iana, site_code, source="interview")
    except Exception as exc:
        logging.warning("interview timezone save failed for %s: %s", app_user_id, exc)
    try:
        update_user_profile_fields(app_user_id, {"OnboardingPendingTz": ""})
    except Exception:
        pass
    return _after_timezone(app_user_id, prefix=f"Timezone set to {iana}.\n\n")


def _process_timezone_step(app_user_id: str, user_text: str) -> Tuple[str, bool]:
    """Resolve the region/timezone. A designated input (menu number, known
    country/city) is saved directly; a fuzzy/LLM-inferred match (typo like
    "koera", free text) is confirmed via ``timezone_confirm`` before saving."""
    try:
        from chatbot.timezone_onboarding import (
            build_menu_message, resolve_choice_ex, is_other_choice, other_prompt_message,
        )
    except Exception:
        return _after_timezone(app_user_id)

    # "Other" (option 6 / "other"): ask for the country/city, stay on this step.
    if is_other_choice(user_text):
        return other_prompt_message(False), False

    iana, site_code, source = resolve_choice_ex(user_text)
    if not iana:
        menu = build_menu_message(False, intro=False)
        return ("I couldn't recognize that. Reply with a number, or type your country/city "
                "(e.g. London, New York, Tokyo).\n\n" + menu).strip(), False

    if source in ("fuzzy", "llm"):  # inferred -> confirm before saving
        _set_pending_tz(app_user_id, iana, site_code)
        _set_step(app_user_id, "timezone_confirm")
        return _tz_confirm_question(iana, site_code), False

    return _save_tz_and_advance(app_user_id, iana, site_code)


def _process_timezone_confirm_step(app_user_id: str, user_text: str) -> Tuple[str, bool]:
    """Handle the yes/no reply to an inferred timezone. Yes saves it and advances;
    No re-shows the region menu; unclear re-asks the yes/no."""
    pending = _get_pending_tz(app_user_id)
    iana = str(pending.get("iana") or "")
    site_code = str(pending.get("site") or "")

    from chatbot.timezone_onboarding import build_menu_message
    if not iana:  # lost the pending value — re-show the menu
        _set_step(app_user_id, "timezone")
        return build_menu_message(False, intro=False), False

    from chatbot.nlu import interpret_optin
    choice = interpret_optin(user_text)
    if choice is True:
        return _save_tz_and_advance(app_user_id, iana, site_code)
    if choice is False:
        try:
            update_user_profile_fields(app_user_id, {"OnboardingPendingTz": ""})
        except Exception:
            pass
        _set_step(app_user_id, "timezone")
        return ("No problem — let's set your region again.\n\n"
                + build_menu_message(False, intro=False)).strip(), False
    return ("Please reply 1 (yes) or 2 (no).\n\n" + _tz_confirm_question(iana, site_code)).strip(), False


def _save_age_and_advance(app_user_id: str, age: int) -> Tuple[str, bool]:
    """Persist a confirmed age, clear the pending scratch, and move to the gender step
    with a confirmation notice (its own bubble)."""
    update_user_profile_fields(app_user_id, {
        "Age": int(age),
        "OnboardingPendingAge": "",
        "InterviewUpdatedAt": _utc_now_iso(),
    })
    return _enter_step(app_user_id, "gender", prefix=_field_confirmation("age", age) + "\n\n")


def _process_age_step(app_user_id: str, user_text: str) -> Tuple[str, bool]:
    """Parse the age answer. A BARE integer ("30") is stored right away; anything the
    parser had to interpret — spelled-out ("thirty one"), embedded ("i'm 30"), or
    typo'd — is confirmed via the yes/no ``age_confirm`` step before it is stored."""
    from chatbot.nlu import parse_age_with_source
    age, source = parse_age_with_source(user_text)
    if age is None:
        return "Please tell me your age, e.g. 30 (or 'thirty').", False
    if age < 1 or age > 150:
        return "Please enter a realistic age (1–150).\n[e.g. 30]", False
    if source != "exact":
        # Anything that isn't a bare integer ("thirty one", "i'm 30", a typo) was
        # interpreted -> confirm before storing.
        _set_pending_age(app_user_id, age)
        _set_step(app_user_id, "age_confirm")
        return _age_confirm_question(age), False
    return _save_age_and_advance(app_user_id, age)


def _process_age_confirm_step(app_user_id: str, user_text: str) -> Tuple[str, bool]:
    """Handle the yes/no reply to an inferred age. Yes stores it and advances; No
    (or an empty pending value) re-asks the age; anything unclear re-asks the yes/no."""
    from chatbot.nlu import interpret_optin
    pending = _get_pending_age(app_user_id)
    choice = interpret_optin(user_text)
    if choice is True and pending is not None:
        return _save_age_and_advance(app_user_id, pending)
    if choice is False:
        _clear_pending_age(app_user_id)
        _set_step(app_user_id, "age")
        return "No problem — how old are you?\n[e.g. 30]", False
    if pending is None:  # lost the pending value — re-ask cleanly
        _set_step(app_user_id, "age")
        return "How old are you?\n[e.g. 30]", False
    return ("Please reply 1 (yes) or 2 (no).\n\n" + _age_confirm_question(pending)).strip(), False


def _save_gender_and_advance(app_user_id: str, gender: str) -> Tuple[str, bool]:
    """Persist a confirmed gender, clear the pending scratch, and move to the timezone
    step with a confirmation notice."""
    stored = str(gender).replace(" ", "_")
    update_user_profile_fields(app_user_id, {
        "Gender": stored,
        "OnboardingPendingGender": "",
        "InterviewUpdatedAt": _utc_now_iso(),
    })
    return _enter_step(app_user_id, "timezone", prefix=_field_confirmation("gender", stored) + "\n\n")


def _process_gender_step(app_user_id: str, user_text: str) -> Tuple[str, bool]:
    """Parse a gender answer, storing recognized English synonyms directly.

    A value the parser had to infer (a typo such as "mele", or free text) is
    confirmed through the yes/no ``gender_confirm`` step before it is stored.
    """
    from chatbot.nlu import parse_gender_with_source
    gender, source = parse_gender_with_source(user_text)
    if not gender:
        return "Please enter one of: male, female, other, prefer not to say.\n[e.g. male]", False
    if source != "exact":  # typo / free text -> confirm
        _set_pending_gender(app_user_id, gender)
        _set_step(app_user_id, "gender_confirm")
        return _gender_confirm_question(gender), False
    return _save_gender_and_advance(app_user_id, gender)


def _process_gender_confirm_step(app_user_id: str, user_text: str) -> Tuple[str, bool]:
    """Handle the yes/no reply to an inferred gender. Yes stores it and advances; No
    (or an empty pending value) re-asks gender; unclear re-asks the yes/no."""
    pending = _get_pending_gender(app_user_id)
    if not pending:  # lost the pending value — re-ask cleanly
        _set_step(app_user_id, "gender")
        return "Your gender? (male / female / other / prefer not to say)\n[e.g. male]", False
    from chatbot.nlu import interpret_optin
    choice = interpret_optin(user_text)
    if choice is True:
        return _save_gender_and_advance(app_user_id, pending)
    if choice is False:
        _clear_pending_gender(app_user_id)
        _set_step(app_user_id, "gender")
        return "No problem — your gender? (male / female / other / prefer not to say)\n[e.g. male]", False
    return ("Please reply 1 (yes) or 2 (no).\n\n" + _gender_confirm_question(pending)).strip(), False


def _process_alert_optin_step(app_user_id: str, user_text: str) -> Tuple[str, bool]:
    from chatbot.nlu import interpret_optin
    choice = interpret_optin(user_text)
    if choice is True:
        return _enter_step(app_user_id, "alert_select")
    if choice is False:
        return _enter_step(app_user_id, "summary_optin", prefix=_no_alert_guidance() + "\n\n")
    return ("Please reply 1 (yes) or 2 (no).\n\n" + _alert_optin_question()).strip(), False


def _process_alert_select_step(app_user_id: str, user_text: str) -> Tuple[str, bool]:
    text = (user_text or "").strip()
    if text.lower() in _PASS_TOKENS:
        return _enter_step(app_user_id, "summary_optin", prefix=_no_alert_guidance() + "\n\n")

    vitals = _stored_vitals(app_user_id) or _collected_vitals(app_user_id)
    if not vitals:  # vitals vanished between turns — skip gracefully
        return _enter_step(app_user_id, "summary_optin", prefix=_no_vitals_guidance() + "\n\n")

    from chatbot.nlu import interpret_metric_selection
    selected, invalid = interpret_metric_selection(text, vitals)
    if invalid or not selected:
        bad = (" I didn't recognize: " + ", ".join(invalid)) if invalid else ""
        return ("Please pick from the list." + bad + "\n\n" + _alert_select_question(vitals)).strip(), False

    _set_alert_queue(app_user_id, selected)
    return _enter_step(app_user_id, "alert_mode")


def _process_alert_mode_step(app_user_id: str, user_text: str) -> Tuple[str, bool]:
    queue = _get_alert_queue(app_user_id)
    if not queue:
        return _enter_step(app_user_id, "summary_optin")
    metric = queue[0]

    from chatbot.nlu import interpret_auto_manual
    mode = interpret_auto_manual(user_text)
    if mode == "manual":
        _set_step(app_user_id, "alert_lower")
        return _alert_lower_question(metric, app_user_id), False
    if mode == "auto":
        auto = _auto_for(metric, app_user_id)
        if not auto:  # shouldn't happen (mode only offered when auto exists), but be safe
            _set_step(app_user_id, "alert_lower")
            return _alert_lower_question(metric, app_user_id), False
        ok = _save_threshold(app_user_id, auto["metric_type"], auto["lower"], auto["upper"], auto["sustain_minutes"])
        if not ok:
            return ("Sorry, I couldn't save that (storage error). Please try again.\n\n"
                    + _alert_mode_question(metric)).strip(), False
        confirm = _confirm_line(auto["metric_type"], auto["lower"], auto["upper"], auto["sustain_minutes"])
        # Blank line between the setting line and the medical rationale (readability).
        confirm = confirm + "\n\n" + str(auto.get("rationale") or "")
        return _advance_after_metric(app_user_id, confirm.strip())
    return ("Please reply 1 (auto) or 2 (manual).\n\n" + _alert_mode_question(metric)).strip(), False


def _ask_bound_confirm(app_user_id: str, field: str, value) -> Tuple[str, bool]:
    """Store an inferred bound/duration and ask the user to confirm it (yes/no)."""
    _set_bound_pending(app_user_id, field, value)
    _set_step(app_user_id, "alert_bound_confirm")
    return _bound_confirm_question(field, value), False


def _apply_lower(app_user_id: str, metric: str, value) -> Tuple[str, bool]:
    """Store the (confirmed) lower bound and move to the upper-bound question."""
    scratch = _get_threshold_scratch(app_user_id)
    _set_threshold_scratch(app_user_id, value, scratch.get("upper"))
    _set_step(app_user_id, "alert_upper")
    return _alert_upper_question(metric, app_user_id), False


def _apply_upper(app_user_id: str, metric: str, value) -> Tuple[str, bool]:
    """Validate lower<upper / at-least-one-bound, then move to the duration question.
    On a validation failure it re-asks the upper bound."""
    scratch = _get_threshold_scratch(app_user_id)
    lower = scratch.get("lower")
    upper = value
    if lower is None and upper is None:
        _set_step(app_user_id, "alert_upper")
        return ("You need at least one bound. Please enter an upper number (or set a lower one).\n\n"
                + _alert_upper_question(metric, app_user_id)).strip(), False
    if lower is not None and upper is not None and lower >= upper:
        _set_step(app_user_id, "alert_upper")
        return (f"The upper bound must be greater than the lower bound ({lower:g}). Please re-enter.\n\n"
                + _alert_upper_question(metric, app_user_id)).strip(), False
    _set_threshold_scratch(app_user_id, lower, upper)
    _set_step(app_user_id, "alert_duration")
    return _alert_duration_question(metric), False


def _apply_duration(app_user_id: str, metric: str, minutes: int) -> Tuple[str, bool]:
    """Save the alert with the (confirmed) duration and advance to the next metric."""
    scratch = _get_threshold_scratch(app_user_id)
    lower, upper = scratch.get("lower"), scratch.get("upper")
    if not _save_threshold(app_user_id, metric, lower, upper, minutes):
        _set_step(app_user_id, "alert_duration")
        return ("Sorry, I couldn't save that alert (storage error). Please try the duration again.\n\n"
                + _alert_duration_question(metric)).strip(), False
    confirm = _confirm_line(metric, lower, upper, minutes)
    return _advance_after_metric(app_user_id, confirm)


def _process_alert_lower_step(app_user_id: str, user_text: str) -> Tuple[str, bool]:
    queue = _get_alert_queue(app_user_id)
    if not queue:
        return _enter_step(app_user_id, "summary_optin")
    metric = queue[0]
    from chatbot.nlu import interpret_bound
    status, value, source = interpret_bound(user_text)
    if status == "error":
        return ("Please enter a number (e.g. 50), or '-' for no lower bound.\n\n"
                + _alert_lower_question(metric, app_user_id)).strip(), False
    if source in ("words", "llm"):  # inferred -> confirm before applying
        return _ask_bound_confirm(app_user_id, "lower", value)
    return _apply_lower(app_user_id, metric, value)


def _process_alert_upper_step(app_user_id: str, user_text: str) -> Tuple[str, bool]:
    queue = _get_alert_queue(app_user_id)
    if not queue:
        return _enter_step(app_user_id, "summary_optin")
    metric = queue[0]
    from chatbot.nlu import interpret_bound
    status, value, source = interpret_bound(user_text)
    if status == "error":
        return ("Please enter a number (e.g. 120), or '-' for no upper bound.\n\n"
                + _alert_upper_question(metric, app_user_id)).strip(), False
    if source in ("words", "llm"):
        return _ask_bound_confirm(app_user_id, "upper", value)
    return _apply_upper(app_user_id, metric, value)


def _process_alert_duration_step(app_user_id: str, user_text: str) -> Tuple[str, bool]:
    queue = _get_alert_queue(app_user_id)
    if not queue:
        return _enter_step(app_user_id, "summary_optin")
    metric = queue[0]
    from chatbot.nlu import interpret_duration
    minutes, source = interpret_duration(user_text)
    if minutes is None:
        return ("Please enter a duration in minutes, e.g. 10.\n\n"
                + _alert_duration_question(metric)).strip(), False
    if source in ("words", "llm"):
        return _ask_bound_confirm(app_user_id, "duration", minutes)
    return _apply_duration(app_user_id, metric, minutes)


def _process_alert_bound_confirm_step(app_user_id: str, user_text: str) -> Tuple[str, bool]:
    """Handle the yes/no reply to an inferred bound/duration. Yes applies it (and
    advances); No re-asks that field; unclear re-asks the yes/no."""
    queue = _get_alert_queue(app_user_id)
    if not queue:
        return _enter_step(app_user_id, "summary_optin")
    metric = queue[0]
    pending = _get_bound_pending(app_user_id)
    field = str(pending.get("field") or "")
    value = pending.get("value")

    if not field:  # lost the pending value — restart at the lower bound
        _set_step(app_user_id, "alert_lower")
        return _alert_lower_question(metric, app_user_id), False

    from chatbot.nlu import interpret_optin
    choice = interpret_optin(user_text)
    if choice is True:
        _clear_bound_pending(app_user_id)
        if field == "lower":
            return _apply_lower(app_user_id, metric, value)
        if field == "upper":
            return _apply_upper(app_user_id, metric, value)
        return _apply_duration(app_user_id, metric, int(round(float(value))))
    if choice is False:
        _clear_bound_pending(app_user_id)
        _set_step(app_user_id, "alert_" + field)
        reask = {
            "lower": _alert_lower_question(metric, app_user_id),
            "upper": _alert_upper_question(metric, app_user_id),
            "duration": _alert_duration_question(metric),
        }[field]
        return ("No problem — let's try again.\n\n" + reask).strip(), False
    return ("Please reply 1 (yes) or 2 (no).\n\n" + _bound_confirm_question(field, value)).strip(), False


def _process_summary_optin_step(app_user_id: str, user_text: str) -> Tuple[str, bool]:
    from chatbot.nlu import interpret_optin
    choice = interpret_optin(user_text)
    if choice is True:
        return _enter_step(app_user_id, "summary_time")
    if choice is False:
        _save_summary_hour(app_user_id, None)
        return _finish(app_user_id, prefix="No daily summary — you can add one later with /set_scheduled_alert.\n\n")
    return ("Please reply 1 (yes) or 2 (no).\n\n" + _summary_optin_question()).strip(), False


def _process_summary_time_step(app_user_id: str, user_text: str) -> Tuple[str, bool]:
    text = (user_text or "").strip()
    if text.lower() in _PASS_TOKENS:
        _save_summary_hour(app_user_id, None)
        return _finish(app_user_id, prefix="No daily summary set. You can add one later with /set_scheduled_alert.\n\n")

    try:
        from chatbot.alert_thresholds import parse_clock_time
        parsed = parse_clock_time(text)
    except Exception:
        parsed = None
    if parsed is None:
        return ("That's not a valid time. Try 09:00, 17:20, 11pm, or 23.\n\n"
                + _summary_time_question()).strip(), False

    hour, minute = parsed
    try:
        from chatbot.alert_thresholds import round_to_5
        rhour, rminute = round_to_5(hour, minute)
    except Exception:
        rhour, rminute = hour, minute
    _save_summary_hour(app_user_id, rhour, rminute)
    rounded_note = (f" (rounded from {hour:02d}:{minute:02d})"
                    if (rhour, rminute) != (hour, minute) else "")
    return _finish(
        app_user_id,
        prefix=(f"Daily summary set for ~{rhour:02d}:{rminute:02d} your local time{rounded_note}.\n\n"),
    )


# ==========================================================================
# Public API
# ==========================================================================
def _special_step_handlers():
    """Return the single dispatch inventory for all non-generic steps."""

    return {
        "age": _process_age_step,
        "age_confirm": _process_age_confirm_step,
        "gender": _process_gender_step,
        "gender_confirm": _process_gender_confirm_step,
        "timezone": _process_timezone_step,
        "timezone_confirm": _process_timezone_confirm_step,
        "alert_optin": _process_alert_optin_step,
        "alert_select": _process_alert_select_step,
        "alert_mode": _process_alert_mode_step,
        "alert_lower": _process_alert_lower_step,
        "alert_upper": _process_alert_upper_step,
        "alert_duration": _process_alert_duration_step,
        "alert_bound_confirm": _process_alert_bound_confirm_step,
        "summary_optin": _process_summary_optin_step,
        "summary_time": _process_summary_time_step,
    }


def begin_interview(app_user_id: str) -> str:
    """Start or resume onboarding without erasing saved user settings."""

    user = get_registered_user(app_user_id)
    existing = str((user or {}).get("InterviewStep", "") or "")
    step = (
        existing
        if existing and existing != "done" and existing in INTERVIEW_STEPS
        else INTERVIEW_STEPS[0]
    )
    update_user_profile_fields(app_user_id, {
        "InterviewStatus": "in_progress",
        "InterviewStep": step,
        "InterviewUpdatedAt": _utc_now_iso(),
    })
    greeting = "Welcome! A few quick questions to set you up. (/cancel to stop.)"
    # The greeting and the first question are sent as SEPARATE bubbles (split on
    # MESSAGE_BREAK by the Telegram layer).
    if step in _special_step_handlers():
        message, _ = _enter_step(app_user_id, step)
        return greeting + MESSAGE_BREAK + message
    return greeting + MESSAGE_BREAK + _question_for_step(step)


def begin_interview_update(app_user_id: str) -> str:
    """Re-run the interview to update the profile, showing current values."""
    user = get_registered_user(app_user_id)
    existing = ""
    if user:
        parts = []
        if user.get("Name"):
            parts.append(f"Name: {user['Name']}")
        if user.get("Age"):
            parts.append(f"Age: {user['Age']}")
        if user.get("Gender"):
            parts.append(f"Gender: {user['Gender']}")
        if parts:
            existing = "Your current profile:\n" + "\n".join(parts) + "\n\n"

    update_user_profile_fields(app_user_id, {
        "InterviewStatus": "in_progress",
        "InterviewStep": INTERVIEW_STEPS[0],
        "InterviewUpdatedAt": _utc_now_iso(),
    })
    header = (
        "Let's update your profile — I'll ask again and overwrite the old values. "
        "(/cancel to abort.)"
    )
    return existing + header + MESSAGE_BREAK + _question_for_step(INTERVIEW_STEPS[0])


def process_interview_answer(app_user_id: str, user_text: str) -> Tuple[str, bool]:
    """Process one onboarding answer. Returns (response_message, is_complete)."""
    user = get_registered_user(app_user_id)
    if not user:
        return "User not found. Please register first.", False

    current_step = str(user.get("InterviewStep", INTERVIEW_STEPS[0]))
    if current_step not in INTERVIEW_STEPS:
        current_step = INTERVIEW_STEPS[0]

    # Non-generic steps own their parsing, confirmation, and side effects.
    special = _special_step_handlers()
    if current_step in special:
        return special[current_step](app_user_id, user_text)

    # Name is the only generic field step; all other steps are dispatched above.
    value, err = _validate_answer(current_step, user_text)
    if err:
        return err, False

    field_name = STEP_FIELD_MAP[current_step]
    current_idx = INTERVIEW_STEPS.index(current_step)
    update_fields = {field_name: value, "InterviewUpdatedAt": _utc_now_iso()}
    next_step = INTERVIEW_STEPS[current_idx + 1]
    update_fields["InterviewStep"] = next_step
    ok = update_user_profile_fields(app_user_id, update_fields)
    if not ok:
        logging.error("interview update failed for %s at step %s", app_user_id, current_step)
        return "A temporary error occurred. Please send your answer again.", False
    # Confirm the saved value as its own bubble, then ask the next question.
    confirm = _field_confirmation(current_step, value)
    return _with_prefix(confirm, _question_for_step(next_step)).strip(), False


def cancel_interview(app_user_id: str) -> str:
    """Cancel an in-progress interview, keeping whatever was saved."""
    user = get_registered_user(app_user_id)
    status = str((user or {}).get("InterviewStatus", "not_started"))
    if status == "completed":
        return "Your profile is already complete. Use /update_info to change it."
    update_user_profile_fields(app_user_id, {
        "InterviewStatus": "not_started",
        "InterviewStep": "name",
        "InterviewUpdatedAt": _utc_now_iso(),
    })
    return (
        "Interview cancelled. Your partial answers have been saved.\n"
        "You can restart anytime with /update_info or /start."
    )


# ==========================================================================
# Validation (simple field steps)
# ==========================================================================
def _validate_answer(step: str, text: str) -> Tuple[Any, Optional[str]]:
    """Return (parsed_value, error_message_or_None) for simple field steps."""
    text = (text or "").strip()
    if not text:
        return None, "Please provide an answer (it cannot be empty)."

    if step == "name":
        if len(text) > 100:
            return None, "Name is too long. Please keep it under 100 characters."
        return text, None

    # Defensive fallback for callers outside the public interview dispatcher.
    return text, None
