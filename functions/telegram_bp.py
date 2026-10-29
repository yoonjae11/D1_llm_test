# -*- coding: utf-8 -*-
"""
Blueprint: Telegram webhook handler (bot commands + chatbot integration).
"""

import logging
import datetime
import os
import threading
import time

import azure.functions as func
import azure.durable_functions as df

from functions.config import (
    TELEGRAM_PASSCODE_MAX_ATTEMPTS,
    TELEGRAM_WEBHOOK_REQUIRE_SECRET,
    TELEGRAM_WEBHOOK_SECRET_TOKEN,
)
from functions.security import constant_time_secret_matches
from functions.pairing import (
    PairingConfigurationError,
    PairingStorageError,
    consume_pairing_code,
    finalize_pairing_code,
    is_pairing_code,
)
from functions.shared import (
    TelegramStateStorageError,
    UserMappingStorageError,
    send_telegram_message,
    send_telegram_typing,
    get_app_user_id,
    get_channel_app_user_id,
    save_user_mapping,
    _build_withings_auth_url,
    get_registered_user, upsert_registered_user,
    save_register_withings_state, get_register_withings_state, clear_register_withings_state,
    save_pending_input_state, get_pending_input_state, clear_pending_input_state,
    get_interview_status,
    verify_registered_user_passcode,
)
from functions.telegram_conversation_log import (
    build_conversation_event,
    hash_chat_id,
    stage_conversation_event,
)
from functions.interview import (
    begin_interview,
    begin_interview_update,
    process_interview_answer,
    cancel_interview,
    split_message_bubbles,
)

# Chatbot modules (optional)
CHATBOT_AVAILABLE = False
handle_user_message = None
try:
    from chatbot.entrypoint import handle_user_message
    CHATBOT_AVAILABLE = True
except Exception as _e:
    logging.warning(f"[BOOT] Chatbot modules not available (telegram features disabled): {_e}")

# Metric availability refresh enqueue (best-effort) — used on /start and /new so
# the user's inventory is re-checked promptly, in addition to the daily timer.
enqueue_metric_availability_refresh = None
try:
    from chatbot.availability_refresh import enqueue_metric_availability_refresh
except Exception as _av_e:
    logging.warning(f"[BOOT] availability refresh enqueue unavailable: {_av_e}")


def _refresh_availability_async(app_user_id: str) -> None:
    """Fire-and-forget availability refresh enqueue; never blocks the webhook."""
    if not enqueue_metric_availability_refresh or not app_user_id:
        return
    try:
        threading.Thread(
            target=enqueue_metric_availability_refresh, args=(app_user_id,), daemon=True
        ).start()
    except Exception:
        logging.warning("availability refresh thread spawn failed for %s", app_user_id)


# Timezone onboarding is a light module (no graph/langchain), imported separately
# so the /update_timezone command works even if the heavy chatbot import fails.
TZ_ONBOARDING_AVAILABLE = False
handle_update_timezone_command = None
try:
    from chatbot.timezone_onboarding import handle_update_timezone_command
    TZ_ONBOARDING_AVAILABLE = True
except Exception as _tz_e:
    logging.warning(f"[BOOT] timezone onboarding unavailable: {_tz_e}")

# Episode interrupt/resume commands (/new, /resume) — light module, imported
# separately so they work even if the heavy chatbot import fails.
EPISODE_SERVICE_AVAILABLE = False
handle_resume_command = None
handle_new_command = None
episode_thread_id_for_chat = None
try:
    from chatbot.episode_service import (
        handle_new_command,
        handle_resume_command,
        thread_id_for_chat as episode_thread_id_for_chat,
    )
    EPISODE_SERVICE_AVAILABLE = True
except Exception as _ep_e:
    logging.warning(f"[BOOT] episode service unavailable: {_ep_e}")

# ── Telegram update deduplication ──
# Prevents processing the same Telegram update multiple times when
# Telegram retries the webhook (e.g. due to slow responses or transient errors).
_PROCESSED_UPDATES: dict = {}  # update_id -> timestamp
_PROCESSED_UPDATES_LOCK = threading.Lock()
_DEDUP_TTL_SECONDS = 300  # keep entries for 5 minutes


def _is_duplicate_update(update_id: int) -> bool:
    """Return True if this update_id was already processed recently."""
    now = time.time()
    with _PROCESSED_UPDATES_LOCK:
        # Evict stale entries
        stale = [uid for uid, ts in _PROCESSED_UPDATES.items() if now - ts > _DEDUP_TTL_SECONDS]
        for uid in stale:
            del _PROCESSED_UPDATES[uid]
        if update_id in _PROCESSED_UPDATES:
            return True
        _PROCESSED_UPDATES[update_id] = now
        return False


# ── Continuous typing indicator ──
# Telegram's "typing..." status expires after ~5 seconds, so we resend it
# every _TYPING_INTERVAL_SECONDS to keep the indicator visible until the
# actual response is ready.  This runs in a background thread and stops
# as soon as the cancelled event is set.
_TYPING_INTERVAL_SECONDS = 4


def _keep_typing(chat_id, cancelled: threading.Event):
    """Repeatedly send 'typing...' action until cancelled."""
    while not cancelled.wait(_TYPING_INTERVAL_SECONDS):
        send_telegram_typing(chat_id)


def _message_time_utc(message: dict):
    try:
        ts = message.get("date")
        if ts is not None:
            return datetime.datetime.fromtimestamp(int(ts), tz=datetime.timezone.utc)
    except Exception:
        pass
    return datetime.datetime.now(datetime.timezone.utc)


def _conversation_ids(chat_id, update_id, message_id):
    chat_hash = hash_chat_id(chat_id)
    conversation_id = f"telegram:{chat_hash}" if chat_hash else ""
    if update_id is not None:
        turn_id = f"{conversation_id}:{update_id}" if conversation_id else str(update_id)
    elif message_id is not None:
        turn_id = f"{conversation_id}:message:{message_id}" if conversation_id else str(message_id)
    else:
        turn_id = f"{conversation_id}:event:{int(time.time() * 1000)}" if conversation_id else ""
    return conversation_id, turn_id


def _route_for_inbound(
    command: str,
    register_state: dict = None,
    pending_input: dict = None,
) -> str:
    command = str(command or "").lower()
    if register_state:
        return "register_withings"
    pending_kind = str((pending_input or {}).get("kind") or "").lower()
    if pending_kind == "login":
        return "login"
    if pending_kind == "timezone":
        return "onboarding"
    if pending_kind:
        return "settings"
    if command == "/register_user":
        return "register"
    if command == "/login":
        return "login"
    if command in {"/register_withings", "/regirster_withings"}:
        return "register_withings"
    if command == "/register":
        return "register"
    if command in {"/start", "/update_info"}:
        return "interview"
    if command == "/update_timezone":
        return "onboarding"
    if command in {"/resume", "/new"}:
        return "episode"
    if command == "/cancel":
        return "interview"
    return "chat"


def _start_interview_message(app_user_id: str) -> str:
    """Return the non-destructive `/start` response used by the original bot."""

    if get_interview_status(app_user_id) == "completed":
        return (
            "You're all set! Your profile is already complete.\n"
            "Just type your health question to get started.\n"
            "To update your profile, send /update_info."
        )
    return begin_interview(app_user_id)


# =============================================================================
# Stateful command follow-up ("pending input") flows
# =============================================================================
# A value-needing command opens a flow; the NEXT message is interpreted in that
# command's context (re-prompt on bad input, exit keyword to bail). /register_user
# is intentionally excluded (one-time registration + passcode security).
PENDING_KINDS = {"login", "timezone", "set_threshold", "clear_threshold", "set_summary", "clear_summary"}
PENDING_EXIT_KEYWORDS = {
    "pass", "skip", "cancel", "/cancel",
}
PENDING_STALE_SECONDS = 30 * 60


def _pending_is_stale(updated_at: str) -> bool:
    """True when an open flow is older than the staleness window — a safety net so
    a stuck flow never locks the user out of every other command."""
    if not updated_at:
        return False
    try:
        ts = datetime.datetime.fromisoformat(str(updated_at).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=datetime.timezone.utc)
        age = (datetime.datetime.now(datetime.timezone.utc) - ts).total_seconds()
        return age > PENDING_STALE_SECONDS
    except Exception:
        return False


def _fmt_clock_times(times) -> str:
    return ", ".join(f"{h:02d}:{m:02d}" for (h, m) in (times or [])) or "—"


def _pending_skip_hint() -> str:
    """One-line note (onboarding style) telling the user how to bail out of a flow
    without changing anything."""
    return "To leave it unchanged, type pass."


def _passcode_lockout_message(retry_after_seconds: int) -> str:
    minutes = max(1, (int(retry_after_seconds or 0) + 59) // 60)
    return f"Too many incorrect passcode attempts. Try again in about {minutes} minute(s)."


def _verify_passcode_with_throttle(chat_id, app_user_id: str, user_entity: dict, supplied: str):
    """Verify a passcode and update the shared persistent attempt counter."""

    verified, _reason, status = verify_registered_user_passcode(
        chat_id, app_user_id, supplied, user_entity=user_entity
    )
    return verified, status


def _handle_pending_input(kind, data, app_user_id, chat_id, text):
    """Interpret the next message as input for an open command flow.

    Returns ``(done, reply_text, new_data)``. ``done=True`` finishes the flow
    (caller clears the pending state). When ``done=False`` and ``new_data`` is not
    None, the caller persists that as the flow's updated sub-state (used by the
    two-step /set_threshold flow: first a metric, then the bounds — like onboarding)."""
    text = str(text or "").strip()
    data = data or {}

    # ---- /login (switch this chat's active UserId; passcode-gated) ----
    # Stages: userid -> passcode. register_user still does register + initial login;
    # /login only SWITCHES an already-registered chat to a different existing account.
    if kind == "login":
        stage = str(data.get("stage") or "userid")
        if stage == "userid":
            target_id = text.strip()
            if not target_id:
                return False, "Please enter the UserId you want to switch to.", {"stage": "userid"}
            try:
                exists = bool(get_registered_user(target_id))
            except Exception:
                exists = False
            if not exists:
                return False, f"No UserId matching '{target_id}'. Try again, or type 'cancel'.", {"stage": "userid"}
            return False, f"Found '{target_id}'. Please enter its passcode.", {"stage": "passcode", "userid": target_id}

        # stage == "passcode"
        target_id = str(data.get("userid") or "")
        try:
            user_ent = get_registered_user(target_id)
        except Exception:
            user_ent = None
        if not user_ent:
            return True, f"'{target_id}' is no longer available. Please run /login again.", None
        verified, attempt_status = _verify_passcode_with_throttle(
            chat_id, target_id, user_ent, text.strip()
        )
        if not verified:
            if not attempt_status["allowed"]:
                return True, _passcode_lockout_message(
                    attempt_status["retry_after_seconds"]
                ), None
            attempts = attempt_status["attempts"]
            return False, f"That passcode doesn't match. Try again, or type 'cancel'. (attempt {attempts}/{TELEGRAM_PASSCODE_MAX_ATTEMPTS})", \
                {"stage": "passcode", "userid": target_id}
        # Authenticated: switch this chat's mapping to the target account.
        if not save_user_mapping(chat_id, target_id):
            return True, "Something went wrong switching accounts. Please run /login again shortly.", None
        try:
            _refresh_availability_async(target_id)
        except Exception:
            pass
        return True, f"Switched to '{target_id}'. I'll use this account's data from now on.", None

    # ---- /update_timezone ----
    if kind == "timezone":
        from chatbot.timezone_onboarding import (
            resolve_choice, is_other_choice, save_resolved_timezone,
            build_menu_message, other_prompt_message,
        )
        if is_other_choice(text):
            return False, other_prompt_message(False), None
        resolved = resolve_choice(text)
        if not resolved:
            head = "I couldn't recognize that region or timezone.\n\n"
            return False, head + build_menu_message(False, intro=False), None
        iana, site_code = resolved
        if not save_resolved_timezone(app_user_id, iana, site_code, source="user_command"):
            return False, "A temporary error occurred while saving your timezone. Please try again shortly.", None
        return True, f"Your timezone is set to {iana}. You can now ask health-data questions.", None

    # Remaining flows touch the operational store + alert helpers.
    from chatbot.operational_storage import MetricAlertThreshold, OperationalTableRepository
    from chatbot.alert_thresholds import (
        parse_set_threshold,
        metric_label, format_threshold_line, parse_clock_time, round_to_5,
        eligible_alert_metrics,
    )
    from chatbot.config import get_settings as _get_settings
    repo = OperationalTableRepository(fail_soft=True)

    # ---- /set_threshold (one value per question, mirrors onboarding) ----
    # Stages: metric -> (auto|manual) -> lower -> upper -> duration. The metric is
    # resolved leniently (typos/synonyms) and a metric with a cited medical range
    # can be auto-configured. A full one-line "hr 50 120 10" still works in one shot.
    if kind == "set_threshold":
        default_sustain = int(_get_settings().threshold_alert_sustain_minutes_default or 10)
        from chatbot.alert_thresholds import auto_threshold, has_auto_threshold
        from chatbot.nlu import (
            resolve_metric_nlu, interpret_auto_manual, interpret_bound, interpret_duration, interpret_optin,
        )
        from functions.interview import (
            _alert_mode_question, _alert_lower_question, _alert_upper_question,
            _alert_duration_question, _bound_confirm_question,
        )

        storage_err = "Failed to save the threshold (storage error). Please try again shortly."

        def _save(metric_type, lower, upper, sustain, rationale=""):
            ok = repo.save_alert_threshold(MetricAlertThreshold(
                app_user_id=app_user_id, metric_type=metric_type, lower=lower, upper=upper,
                sustain_minutes=sustain, enabled=True,
            ))
            if not ok:
                return None
            line = format_threshold_line(metric_type, lower, upper, sustain, False)
            head = "Saved your alert threshold:\n"
            return head + line + (("\n" + rationale) if rationale else "")

        def _advance_from_metric(metric_type):
            """After a metric is chosen, ask auto/manual (when a medical range exists)
            or go straight to the manual lower bound."""
            if has_auto_threshold(metric_type):
                return False, _alert_mode_question(metric_type), {"stage": "mode", "metric": metric_type}
            return False, _alert_lower_question(metric_type, app_user_id), {"stage": "lower", "metric": metric_type}

        stage = str(data.get("stage") or "metric")
        metric = str(data.get("metric") or "")

        # Stage 1: resolve the metric (typo/synonym tolerant).
        if stage == "metric" or not metric:
            tokens = text.split()
            # Power-user one-liner "hr 50 120 10": first token a metric, rest numbers.
            if len(tokens) >= 3:
                m0 = resolve_metric_nlu(tokens[0])
                if m0:
                    parsed, err = parse_set_threshold([m0] + tokens[1:], default_sustain)
                    if not err:
                        saved = _save(m0, parsed["lower"], parsed["upper"], parsed["sustain_minutes"])
                        return (True, saved, None) if saved else (False, storage_err, {"stage": "metric"})
            # Otherwise treat the WHOLE answer as a (possibly multi-word) metric name.
            m = resolve_metric_nlu(text)
            if not m:
                token_help = ", ".join(eligible_alert_metrics())
                return False, f"I couldn't recognize that metric. Try its name or a close form.\nSupported: {token_help}", {"stage": "metric"}
            return _advance_from_metric(m)

        # Stage 2: auto vs manual.
        if stage == "mode":
            mode = interpret_auto_manual(text)
            if mode == "auto":
                prof = get_registered_user(app_user_id) or {}
                auto = auto_threshold(metric, prof.get("Age"), prof.get("Gender"))
                if not auto:
                    return False, _alert_lower_question(metric, app_user_id), {"stage": "lower", "metric": metric}
                saved = _save(metric, auto["lower"], auto["upper"], auto["sustain_minutes"], auto.get("rationale", ""))
                return (True, saved, None) if saved else (False, storage_err, {"stage": "mode", "metric": metric})
            if mode == "manual":
                return False, _alert_lower_question(metric, app_user_id), {"stage": "lower", "metric": metric}
            return False, ("Please reply 1 (auto) or 2 (manual).\n\n" + _alert_mode_question(metric)), {"stage": "mode", "metric": metric}

        # Apply helpers (shared by the direct stages and the confirm stage). Bounds
        # accept "one hundred"-style words; an INFERRED value is confirmed first.
        def _go_upper(lower_val):
            return False, _alert_upper_question(metric, app_user_id), {"stage": "upper", "metric": metric, "lower": lower_val}

        def _apply_upper_cmd(lower_val, upper_val):
            if lower_val is None and upper_val is None:
                return False, ("You need at least one bound. Please enter an upper number.\n\n"
                               + _alert_upper_question(metric, app_user_id)), {"stage": "upper", "metric": metric, "lower": lower_val}
            if lower_val is not None and upper_val is not None and lower_val >= upper_val:
                return False, (f"The upper bound must be greater than {lower_val:g}. Please re-enter.\n\n"
                               + _alert_upper_question(metric, app_user_id)), {"stage": "upper", "metric": metric, "lower": lower_val}
            return False, _alert_duration_question(metric), {"stage": "duration", "metric": metric, "lower": lower_val, "upper": upper_val}

        def _apply_duration_cmd(lower_val, upper_val, minutes_val):
            saved = _save(metric, lower_val, upper_val, minutes_val)
            return (True, saved, None) if saved else (False, storage_err, {"stage": "duration", "metric": metric, "lower": lower_val, "upper": upper_val})

        # Stage 3: lower bound.
        if stage == "lower":
            status, value, source = interpret_bound(text)
            if status == "error":
                return False, ("Please enter a number (e.g. 50), or '-' for no lower bound.\n\n"
                               + _alert_lower_question(metric, app_user_id)), {"stage": "lower", "metric": metric}
            if source in ("words", "llm"):
                return False, _bound_confirm_question("lower", value), {"stage": "bound_confirm", "field": "lower", "value": value, "metric": metric}
            return _go_upper(value)

        # Stage 4: upper bound (validate at least one bound, lower < upper).
        if stage == "upper":
            status, value, source = interpret_bound(text)
            if status == "error":
                return False, ("Please enter a number (e.g. 120), or '-' for no upper bound.\n\n"
                               + _alert_upper_question(metric, app_user_id)), {"stage": "upper", "metric": metric, "lower": data.get("lower")}
            if source in ("words", "llm"):
                return False, _bound_confirm_question("upper", value), {"stage": "bound_confirm", "field": "upper", "value": value, "metric": metric, "lower": data.get("lower")}
            return _apply_upper_cmd(data.get("lower"), value)

        # Stage 5: duration -> save.
        if stage == "duration":
            minutes, source = interpret_duration(text)
            if minutes is None:
                return False, ("Please enter a duration in minutes, e.g. 10.\n\n"
                               + _alert_duration_question(metric)), {**data, "stage": "duration"}
            if source in ("words", "llm"):
                return False, _bound_confirm_question("duration", minutes), {"stage": "bound_confirm", "field": "duration", "value": minutes, "metric": metric, "lower": data.get("lower"), "upper": data.get("upper")}
            return _apply_duration_cmd(data.get("lower"), data.get("upper"), minutes)

        # Stage 6: confirm an inferred bound/duration ("one hundred" -> 100).
        if stage == "bound_confirm":
            field = str(data.get("field") or "")
            value = data.get("value")
            choice = interpret_optin(text)
            if choice is True and field == "lower":
                return _go_upper(value)
            if choice is True and field == "upper":
                return _apply_upper_cmd(data.get("lower"), value)
            if choice is True and field == "duration":
                return _apply_duration_cmd(data.get("lower"), data.get("upper"), int(round(float(value))))
            if choice is False and field:
                reask = {"lower": _alert_lower_question(metric, app_user_id),
                         "upper": _alert_upper_question(metric, app_user_id),
                         "duration": _alert_duration_question(metric)}[field]
                carry = {"stage": field, "metric": metric}
                if field in ("upper", "duration"):
                    carry["lower"] = data.get("lower")
                if field == "duration":
                    carry["upper"] = data.get("upper")
                return False, "No problem — let's try again.\n\n" + reask, carry
            return False, ("Please reply 1 (yes) or 2 (no).\n\n" + _bound_confirm_question(field, value)), data

        # Unknown stage -> restart cleanly at metric.
        return False, _alert_lower_question(metric, app_user_id) if metric else "Which metric?", {"stage": "metric"}

    # ---- /clear_threshold ----
    if kind == "clear_threshold":
        from chatbot.nlu import resolve_metric_nlu
        mt = resolve_metric_nlu(text.split()[0]) if text.split() else None
        if not mt:
            token_help = ", ".join(eligible_alert_metrics())
            return False, f"Unknown metric. Please type the name exactly as shown.\nSupported: {token_help}", None
        repo.delete_alert_threshold(app_user_id, mt)
        return True, f"Cleared the alert threshold for {metric_label(mt, False)}.", None

    # ---- /set_scheduled_alert (adds one time) ----
    if kind == "set_summary":
        parsed = parse_clock_time(text)
        if parsed is None:
            return False, "I couldn't read that time. e.g. 09:00, 17:20, 11pm, 23.", None
        ohour, ominute = parsed
        hour, minute = round_to_5(ohour, ominute)
        rounded = (hour, minute) != (ohour, ominute)
        rnote = (
            f" (rounded to {hour:02d}:{minute:02d}, the nearest 5-minute mark)"
            if rounded else ""
        )
        res = repo.add_summary_time(app_user_id, hour, minute)
        reason = res.get("reason")
        times_txt = _fmt_clock_times(res.get("times") or [])
        if res.get("ok"):
            return True, f"Added a daily summary at {hour:02d}:{minute:02d}{rnote}. You now have: {times_txt}.", None
        if reason == "duplicate":
            return True, f"{hour:02d}:{minute:02d} is already set. You have: {times_txt}.", None
        if reason == "limit":
            mx = getattr(repo, "SUMMARY_TIMES_MAX", 5)
            return True, f"You can have at most {mx} summaries. Remove one first. Current: {times_txt}.", None
        return False, "I couldn't save that (storage error). Please try again.", None

    # ---- /clear_scheduled_alert ----
    if kind == "clear_summary":
        if text.lower() == "all":
            repo.clear_summary_times(app_user_id)
            return True, "Cleared all scheduled daily summaries.", None
        parsed = parse_clock_time(text)
        if parsed is None:
            return False, "I couldn't read that time. e.g. 09:00, 17:20, or 'all'.", None
        h, m = round_to_5(*parsed)
        res = repo.remove_summary_time(app_user_id, h, m)
        left = _fmt_clock_times(res.get("times") or [])
        if res.get("removed"):
            return True, f"Removed the {h:02d}:{m:02d} summary. Remaining: {left}.", None
        return False, f"No summary set for {h:02d}:{m:02d}. Try another time or 'all'. Current: {left}.", None

    return True, "Unknown state.", None


def _safe_app_user_id(chat_id, *, raise_on_storage_error: bool = False):
    try:
        if raise_on_storage_error:
            return (
                get_channel_app_user_id(
                    "Telegram", chat_id, raise_on_storage_error=True
                )
                or ""
            )
        return get_app_user_id(chat_id) or ""
    except Exception:
        if raise_on_storage_error:
            raise
        return ""


def _execution_status_from_state(final_state: dict, fallback: str = "ok") -> str:
    if not isinstance(final_state, dict):
        return fallback
    status = str(final_state.get("execution_status") or "").lower()
    if status in {"success", "ok"}:
        return "ok"
    if status in {"error", "failed"}:
        return "error"
    return fallback


def _plan_summary(plan):
    if not isinstance(plan, dict):
        return {}
    keep = {}
    for key in ("case_type", "metrics", "tables_priority", "granularity", "comparison_requested"):
        if key in plan:
            keep[key] = plan.get(key)
    for key in ("time_range", "baseline_range"):
        if isinstance(plan.get(key), dict):
            keep[key] = plan.get(key)
    return keep


def _stage_inbound_event(
    *,
    chat_id,
    app_user_id,
    update_id,
    message,
    user_text,
    command,
    route,
    conversation_id,
    turn_id,
) -> str:
    try:
        event = build_conversation_event(
            event_type="user_message",
            role="user",
            direction="inbound",
            event_time_utc=_message_time_utc(message),
            app_user_id=app_user_id or "",
            telegram_chat_id=chat_id,
            telegram_update_id=update_id,
            telegram_message_id=message.get("message_id"),
            conversation_id=conversation_id,
            turn_id=turn_id,
            message_text=user_text or "",
            route=route or "",
            intent="",
            command=command or "",
            execution_status="received",
        )
        ok = stage_conversation_event(event)
        logging.info(
            "telegram conversation inbound staged ok=%s turn_id=%s event_id=%s route=%s",
            ok,
            event.get("TurnId"),
            event.get("EventId"),
            route,
        )
        return str(event.get("EventId") or "")
    except Exception as exc:
        logging.warning("Telegram inbound conversation log failed: %s", exc)
        return ""


def _send_logged(
    chat_id,
    text: str,
    *,
    app_user_id: str = "",
    update_id=None,
    parent_event_id: str = "",
    conversation_id: str = "",
    turn_id: str = "",
    response_ordinal: int = 0,
    command: str = "",
    route: str = "",
    intent: str = "",
    final_state: dict = None,
    execution_status: str = "ok",
    error_type: str = "",
    error_message: str = "",
):
    send_telegram_message(chat_id, text)

    try:
        state = final_state if isinstance(final_state, dict) else {}
        final_intent = str(intent or state.get("intent") or "")
        final_route = str(route or final_intent or "chat")
        status = "error" if error_type or error_message else _execution_status_from_state(state, execution_status)
        if status == "error" and not error_message:
            error_message = str(state.get("error") or "")
        if status == "error" and not error_type:
            error_type = str(state.get("error_type") or "")

        event = build_conversation_event(
            event_type="bot_reply",
            role="assistant",
            direction="outbound",
            event_time_utc=datetime.datetime.now(datetime.timezone.utc),
            app_user_id=app_user_id or "",
            telegram_chat_id=chat_id,
            telegram_update_id=update_id,
            telegram_message_id=None,
            conversation_id=conversation_id,
            turn_id=turn_id,
            parent_event_id=parent_event_id,
            response_ordinal=response_ordinal,
            message_text=text or "",
            route=final_route,
            intent=final_intent,
            command=command or "",
            execution_status=status,
            error_message=error_message,
            plan=_plan_summary(state.get("plan") or state.get("last_plan")),
        )
        ok = stage_conversation_event(event)
        logging.info(
            "telegram conversation outbound staged ok=%s turn_id=%s event_id=%s route=%s status=%s",
            ok,
            event.get("TurnId"),
            event.get("EventId"),
            final_route,
            event.get("ExecutionStatus"),
        )
    except Exception as exc:
        logging.warning("Telegram outbound conversation log failed: %s", exc)


bp = df.Blueprint()


@bp.route(route="telegram_webhook", methods=["POST"])
async def telegram_webhook(req: func.HttpRequest) -> func.HttpResponse:
    if not TELEGRAM_WEBHOOK_SECRET_TOKEN and TELEGRAM_WEBHOOK_REQUIRE_SECRET:
        logging.error("TELEGRAM_WEBHOOK_SECRET_TOKEN is missing; refusing webhook traffic.")
        return func.HttpResponse("Telegram webhook is not configured.", status_code=503)
    if TELEGRAM_WEBHOOK_SECRET_TOKEN:
        headers = getattr(req, "headers", {}) or {}
        supplied_secret = (
            headers.get("X-Telegram-Bot-Api-Secret-Token")
            or headers.get("x-telegram-bot-api-secret-token")
            or ""
        )
        if not constant_time_secret_matches(
            TELEGRAM_WEBHOOK_SECRET_TOKEN, supplied_secret
        ):
            logging.warning("Rejected Telegram webhook request with an invalid secret token.")
            return func.HttpResponse("Forbidden", status_code=403)

    try:
        body = req.get_json()

        update_id = body.get("update_id")
        message = body.get("message", {})

        if not message:
            return func.HttpResponse("No Message", status_code=200)

        chat_id = message.get("chat", {}).get("id")
        user_text = (message.get("text", "") or "").strip()

        if not chat_id or not user_text:
            return func.HttpResponse("OK", status_code=200)

        # Telegram reply-to: when the user long-presses a bubble and replies, the
        # quoted message is in reply_to_message. Capture its text + role so the
        # chatbot treats this message as a follow-up to that specific bubble.
        _reply_msg = message.get("reply_to_message") or {}
        reply_to_text = (_reply_msg.get("text") or "").strip()
        reply_to_role = ("assistant" if (_reply_msg.get("from") or {}).get("is_bot") else "user") if reply_to_text else ""

        parts = user_text.split()
        command = parts[0].lower() if parts else ""
        message_id = message.get("message_id")
        try:
            # These reads must distinguish "no active flow" from a storage
            # outage. Otherwise a pending passcode could be misclassified as a
            # normal chat message and persisted before authentication runs.
            app_user_id = _safe_app_user_id(
                chat_id, raise_on_storage_error=True
            )
            register_state_for_log = get_register_withings_state(
                chat_id, raise_on_storage_error=True
            )
            pending_input = get_pending_input_state(
                chat_id, raise_on_storage_error=True
            )
        except (TelegramStateStorageError, UserMappingStorageError) as exc:
            logging.error(
                "Telegram flow-state lookup failed; refusing update before logging: %s",
                exc,
            )
            return func.HttpResponse(
                "Telegram state storage is temporarily unavailable.", status_code=503
            )

        # Mark an update as processed only after security-critical state reads
        # succeed. A 503 response must remain retryable by Telegram.
        if update_id is not None and _is_duplicate_update(update_id):
            logging.info(f"[Telegram] Duplicate update_id={update_id}, skipping.")
            return func.HttpResponse("OK", status_code=200)

        pairing_candidate = bool(
            not app_user_id
            and not register_state_for_log
            and not pending_input
            and is_pairing_code(user_text)
        )
        route = (
            "pairing"
            if pairing_candidate
            else _route_for_inbound(command, register_state_for_log, pending_input)
        )
        conversation_id, turn_id = _conversation_ids(chat_id, update_id, message_id)

        inbound_event_id = _stage_inbound_event(
            chat_id=chat_id,
            app_user_id=app_user_id,
            update_id=update_id,
            message=message,
            user_text=user_text,
            command=command,
            route=route,
            conversation_id=conversation_id,
            turn_id=turn_id,
        )

        response_ordinal = 0

        def reply(
            text,
            *,
            route_override=None,
            app_user_id_override=None,
            intent="",
            final_state=None,
            execution_status="ok",
            error_type="",
            error_message="",
        ):
            nonlocal response_ordinal
            current_response_ordinal = response_ordinal
            response_ordinal += 1
            _send_logged(
                chat_id,
                text,
                app_user_id=app_user_id_override if app_user_id_override is not None else app_user_id,
                update_id=update_id,
                parent_event_id=inbound_event_id,
                conversation_id=conversation_id,
                turn_id=turn_id,
                response_ordinal=current_response_ordinal,
                command=command,
                route=route_override or route,
                intent=intent,
                final_state=final_state,
                execution_status=execution_status,
                error_type=error_type,
                error_message=error_message,
            )

        def reply_interview(text):
            # Onboarding messages may carry MESSAGE_BREAK markers; send each segment
            # as a separate Telegram bubble.
            for segment in split_message_bubbles(text):
                reply(segment, route_override="interview")

        # =================================================================
        # Six-digit app pairing. This runs only for an unmapped chat and before
        # the legacy registration gate. Existing mappings can never be replaced
        # by sending a bare numeric message.
        # =================================================================
        if pairing_candidate:
            try:
                pairing_result = consume_pairing_code(
                    user_text, "Telegram", chat_id
                )
            except (PairingConfigurationError, PairingStorageError):
                logging.error("Telegram pairing is temporarily unavailable.")
                reply(
                    "Account connection is temporarily unavailable. Please try again shortly.",
                    route_override="pairing",
                    execution_status="error",
                    error_type="pairing_unavailable",
                )
                return func.HttpResponse("OK", status_code=200)

            if pairing_result.status == "rate_limited":
                seconds = max(0, int(pairing_result.retry_after_seconds or 0))
                minutes = max(1, (seconds + 59) // 60)
                reply(
                    f"Too many incorrect pairing attempts. Try again in about {minutes} minute(s).",
                    route_override="pairing",
                    execution_status="error",
                    error_type="pairing_rate_limited",
                )
                return func.HttpResponse("OK", status_code=200)

            if not pairing_result.paired:
                reply(
                    "That pairing code is invalid or expired. Generate a new code in the Custom Logger App and try again.",
                    route_override="pairing",
                    execution_status="error",
                    error_type="invalid_pairing_code",
                )
                return func.HttpResponse("OK", status_code=200)

            if not save_user_mapping(
                chat_id, pairing_result.app_user_id, overwrite=False
            ):
                # The claimed code remains bound to this Telegram identity until
                # expiry, so resending it can safely retry a transient failure.
                reply(
                    "The code was verified, but the account connection could not be saved. Please try the same code again shortly.",
                    route_override="pairing",
                    execution_status="error",
                    error_type="pairing_mapping_save_failed",
                )
                return func.HttpResponse("OK", status_code=200)

            try:
                finalize_pairing_code(user_text, "Telegram", chat_id)
            except (PairingConfigurationError, PairingStorageError):
                # UserMapping is already durable; the single-identity claim can
                # safely expire if finalization is temporarily unavailable.
                logging.warning(
                    "Telegram pairing finalization deferred after mapping save."
                )
            _refresh_availability_async(pairing_result.app_user_id)
            reply(
                "Your Sensei sensor account is now connected. You can now use Sensei in this chat.",
                route_override="pairing",
                app_user_id_override=pairing_result.app_user_id,
            )
            return func.HttpResponse("OK", status_code=200)

        # =================================================================
        # Stateful command follow-up: if a value-command opened a flow, the NEXT
        # message is interpreted in that command's context — re-prompt on bad input,
        # an exit keyword bails, and any other command is blocked until done.
        # =================================================================
        if pending_input and _pending_is_stale(pending_input.get("updated_at", "")):
            clear_pending_input_state(chat_id)
            pending_input = None
        if pending_input and pending_input.get("kind") in PENDING_KINDS:
            is_korean = False
            low = user_text.strip().lower()
            if low in PENDING_EXIT_KEYWORDS or command == "/cancel":
                clear_pending_input_state(chat_id)
                reply(("Okay, leaving it unchanged."), route_override="settings")
                return func.HttpResponse("OK", status_code=200)
            if user_text.startswith("/"):
                reply(("Please finish the current step first, or type 'cancel'."),
                      route_override="settings")
                return func.HttpResponse("OK", status_code=200)
            try:
                done, pending_msg, new_data = _handle_pending_input(
                    pending_input["kind"], pending_input.get("data") or {},
                    app_user_id, chat_id, user_text)
            except Exception as exc:
                logging.warning("pending-input handler failed kind=%s: %s", pending_input.get("kind"), exc)
                clear_pending_input_state(chat_id)
                reply(("Something went wrong. Please try again."), route_override="settings")
                return func.HttpResponse("OK", status_code=200)
            if done:
                clear_pending_input_state(chat_id)
            else:
                # Persist any advanced sub-state (e.g. the chosen metric), then remind
                # the user (onboarding style) how to bail out.
                if new_data is not None:
                    if not save_pending_input_state(
                        chat_id, pending_input["kind"], new_data
                    ):
                        clear_pending_input_state(chat_id)
                        reply(
                            (
                                "I couldn't save this step safely. Please restart the command."
                            ),
                            route_override=(
                                "login"
                                if pending_input["kind"] == "login"
                                else "settings"
                            ),
                            execution_status="error",
                            error_type="pending_state_save_failed",
                        )
                        return func.HttpResponse("OK", status_code=200)
                pending_msg = pending_msg.rstrip() + "\n\n" + _pending_skip_hint()
            reply(pending_msg, route_override="settings")
            return func.HttpResponse("OK", status_code=200)

        # =================================================================
        # /help — list available commands (works for everyone, registered or not)
        # =================================================================
        if command == "/help":
            is_korean = False
            help_text = (
                "Available commands:\n\n"
                "/register_user\n"
                "  - Register as a user (do this first, once)\n\n"
                "/login\n"
                "  - Switch to a different UserId (passcode required)\n\n"
                "/start\n"
                "  - Begin the onboarding interview (name/age/gender/region)\n\n"
                "/update_info\n"
                "  - Update your profile answers\n\n"
                "/update_timezone\n"
                "  - Set your timezone\n\n"
                "/set_threshold\n"
                "  - Set a data threshold alert\n\n"
                "/alerts\n"
                "  - View your alerts (thresholds + scheduled summaries)\n\n"
                "/clear_threshold\n"
                "  - Remove a threshold alert\n\n"
                "/set_scheduled_alert\n"
                "  - Add a daily summary time\n\n"
                "/clear_scheduled_alert\n"
                "  - Remove a scheduled summary\n\n"
                "/new\n"
                "  - Start a fresh conversation (clears prior context)\n\n"
                "/cancel\n"
                "  - Cancel an in-progress interview or linking flow\n\n"
                "/help\n"
                "  - Show this help message\n\n"
                "Commands that need values (e.g. /set_threshold, /update_timezone) will show usage and examples when you send them on their own.\n\n"
                "Anything without a command is treated as a health question.\n"
                "Examples: \"How many steps today?\", \"Show my sleep trend for the last 7 days.\""
            )
            reply(help_text, route_override="help")
            return func.HttpResponse("OK", status_code=200)

        # =================================================================
        # /set_scheduled_alert, /clear_scheduled_alert — daily summary schedule
        # =================================================================
        if command in {"/set_scheduled_alert", "/clear_scheduled_alert", "/scheduled_alert"}:
            is_korean = False
            try:
                from chatbot.alert_thresholds import parse_clock_time, round_to_5
                from chatbot.operational_storage import OperationalTableRepository
                repo = OperationalTableRepository(fail_soft=True)

                def _fmt(times):
                    return ", ".join(f"{h:02d}:{m:02d}" for (h, m) in times) if times else (
                        "none")

                arg = " ".join(parts[1:]).strip()

                # ---- /clear_scheduled_alert [HH:MM | all] ----
                if command == "/clear_scheduled_alert":
                    current = repo.get_user_profile(app_user_id).summary_report_times
                    if not arg:
                        if not current:
                            reply(("No scheduled daily summary is set."), route_override="settings")
                        else:
                            # Open a follow-up flow: the next message (a time or 'all')
                            # is read as the removal target.
                            save_pending_input_state(chat_id, "clear_summary")
                            msg = (f"Current summaries: {_fmt(current)}.\n\n"
                                   "Remove one: /clear_scheduled_alert <time>\n"
                                   "Remove all: /clear_scheduled_alert all\n\n"
                                   "e.g. /clear_scheduled_alert 09:00")
                            msg = msg + "\n\n" + _pending_skip_hint()
                            reply(msg, route_override="settings")
                        return func.HttpResponse("OK", status_code=200)
                    if arg.lower() in {"all", "everything"}:
                        repo.clear_summary_times(app_user_id)
                        reply(("Cleared all scheduled daily summaries."), route_override="settings")
                        return func.HttpResponse("OK", status_code=200)
                    parsed = parse_clock_time(arg)
                    if parsed is None:
                        reply(("I couldn't read that time. e.g. 09:00, 17:20, 11pm, 23."),
                              route_override="settings")
                        return func.HttpResponse("OK", status_code=200)
                    h, m = round_to_5(*parsed)
                    res = repo.remove_summary_time(app_user_id, h, m)
                    left = _fmt(res.get("times") or [])
                    if res.get("removed"):
                        reply((f"Removed the {h:02d}:{m:02d} summary. Remaining: {left}."),
                              route_override="settings")
                    else:
                        reply((f"No summary set for {h:02d}:{m:02d}. Current: {left}."),
                              route_override="settings")
                    return func.HttpResponse("OK", status_code=200)

                # ---- /set_scheduled_alert [HH:MM] — adds a time (keeps existing) ----
                if not arg:
                    current = repo.get_user_profile(app_user_id).summary_report_times
                    cur_txt = (
                        (f"Current summaries: {_fmt(current)}.")
                        if current else
                        ("No scheduled daily summary is set yet.")
                    )
                    msg = cur_txt + "\n\n" + (
                        "A scheduled summary sends you that day's health-data summary at the time(s) you pick.\n\n"
                        "Add one by sending a time:\n/set_scheduled_alert <time>\n\n"
                        "e.g. /set_scheduled_alert 09:00 (or 17:20, 9am, 23)\n\n"
                        "You can add several, and times are stored in 5-minute steps (e.g. 14:46 -> 14:45).\n"
                        "To remove: /clear_scheduled_alert <time> or all"
                    )
                    # Open a follow-up flow: the next message (a time) is read as the
                    # summary time to add.
                    save_pending_input_state(chat_id, "set_summary")
                    msg = msg.rstrip() + "\n\n" + _pending_skip_hint()
                    reply(msg, route_override="settings")
                    return func.HttpResponse("OK", status_code=200)

                parsed = parse_clock_time(arg)
                if parsed is None:
                    reply(
                        ("I couldn't read that time. e.g. 09:00, 17:20, 11pm, 23."),
                        route_override="settings",
                    )
                    return func.HttpResponse("OK", status_code=200)
                ohour, ominute = parsed
                hour, minute = round_to_5(ohour, ominute)
                rounded = (hour, minute) != (ohour, ominute)
                rnote = ((f" (rounded to {hour:02d}:{minute:02d}, the nearest 5-minute mark)")
                         if rounded else "")
                res = repo.add_summary_time(app_user_id, hour, minute)
                reason = res.get("reason")
                times_txt = _fmt(res.get("times") or [])
                if res.get("ok"):
                    reply((f"Added a daily summary at {hour:02d}:{minute:02d}{rnote}. You now have: {times_txt}."),
                          route_override="settings")
                elif reason == "duplicate":
                    reply((f"{hour:02d}:{minute:02d} is already set. You have: {times_txt}."),
                          route_override="settings")
                elif reason == "limit":
                    reply((f"You can have at most {repo.SUMMARY_TIMES_MAX} summaries. Remove one first (/clear_scheduled_alert <time>). Current: {times_txt}."),
                          route_override="settings")
                else:
                    reply(
                        ("Failed to save the schedule (storage error). Please try again shortly."),
                        route_override="settings",
                    )
            except Exception as e:
                logging.error(f"scheduled_alert command error app_user_id={app_user_id}: {e}")
                reply(
                    "Server error while updating the scheduled summary. Please try again shortly.",
                    route_override="settings", execution_status="error",
                    error_type=type(e).__name__, error_message=str(e),
                )
            return func.HttpResponse("OK", status_code=200)

        # =================================================================
        # /set_threshold, /alerts, /clear_threshold — user threshold alerts
        # =================================================================
        if command in {"/set_threshold", "/set_threshold_help", "/setting", "/alerts", "/clear_threshold", "/clear_alert"}:
            is_korean = False
            try:
                from chatbot.alert_thresholds import (
                    eligible_alert_metrics, format_threshold_line, metric_label,
                    parse_set_threshold, resolve_metric, supported_metrics_help,
                )
                from chatbot.config import get_settings as _get_settings
                from chatbot.operational_storage import MetricAlertThreshold, OperationalTableRepository
                repo = OperationalTableRepository(fail_soft=True)
                default_sustain = int(_get_settings().threshold_alert_sustain_minutes_default or 10)

                if command in {"/alerts"}:
                    rows = repo.list_alert_thresholds(app_user_id)
                    active = [t for t in rows if t.enabled]
                    # ---- Threshold alerts section ----
                    thr_label = "Threshold alerts:"
                    if not active:
                        thr_section = thr_label + (" not set")
                    else:
                        thr_lines = []
                        for i, t in enumerate(active, 1):
                            line = format_threshold_line(t.metric_type, t.lower, t.upper, t.sustain_minutes, is_korean)
                            # format_threshold_line emits a leading "- " bullet; drop it so
                            # the numbered prefix doesn't read "1. - heart rate: ...".
                            if line.startswith("- "):
                                line = line[2:]
                            thr_lines.append(f"{i}. {line}")
                        thr_section = thr_label + "\n" + "\n".join(thr_lines)
                    # ---- Scheduled daily-summary section (per-user times) ----
                    try:
                        _times = repo.get_user_profile(app_user_id).summary_report_times
                    except Exception:
                        _times = []
                    sched_label = "Scheduled daily summary:"
                    if not _times:
                        sched_section = sched_label + (" not set")
                    else:
                        sched_lines = [f"{i}. {h:02d}:{m:02d}" for i, (h, m) in enumerate(_times, 1)]
                        sched_section = sched_label + "\n" + "\n".join(sched_lines)
                    # ---- Guidance ----
                    guidance = (
                        "To configure more, see /help or just set it via chat."
                    )
                    msg = thr_section + "\n\n" + sched_section + "\n\n" + guidance
                    reply(msg, route_override="settings")
                    return func.HttpResponse("OK", status_code=200)

                if command in {"/clear_threshold", "/clear_alert"}:
                    if len(parts) < 2:
                        rows = [t for t in repo.list_alert_thresholds(app_user_id) if t.enabled]
                        # You can only clear an alert you've already set, so list the
                        # user's CURRENT alerts by their exact metric token (e.g.
                        # HEART_RATE) — the name they type back to remove it. The full
                        # supported-metric set is irrelevant here, so it's omitted. The
                        # follow-up flow reads that bare token, so the instruction must
                        # NOT tell the user to re-issue a /clear_threshold slash command
                        # (slash commands are blocked while a flow is open).
                        if rows:
                            listing = "\n".join(
                                format_threshold_line(t.metric_type, t.lower, t.upper, t.sustain_minutes, is_korean, use_token=True)
                                for t in rows
                            )
                            msg = (
                                "Your current alerts:\n" + listing + "\n\n"
                                "To remove one, reply with the metric name exactly as shown above. (e.g. HEART_RATE)"
                            )
                            # Open a follow-up flow: the next message (a metric token)
                            # is read as the alert to remove.
                            save_pending_input_state(chat_id, "clear_threshold")
                            msg = msg.rstrip() + "\n\n" + _pending_skip_hint()
                        else:
                            msg = ("You have no threshold alerts set.")
                        reply(msg, route_override="settings")
                        return func.HttpResponse("OK", status_code=200)
                    mt = resolve_metric(parts[1])
                    if not mt:
                        reply(f"Unknown metric: {parts[1]}\nSupported: {supported_metrics_help(is_korean)}", route_override="settings")
                        return func.HttpResponse("OK", status_code=200)
                    repo.delete_alert_threshold(app_user_id, mt)
                    reply(
                        (f"Cleared the alert threshold for {metric_label(mt, False)}."),
                        route_override="settings",
                    )
                    return func.HttpResponse("OK", status_code=200)

                # Bare /set_threshold (or /set_threshold_help): list the ACTUAL
                # collected metrics and open a two-step flow exactly like onboarding —
                # the user types a metric, then I ask for the three numbers.
                if command == "/set_threshold_help" or len(parts) < 2:
                    # Threshold alerts only make sense for vital signs whose CURRENT
                    # value is meaningful (heart rate, temperature, ...) — not
                    # cumulative/aggregate metrics (steps, distance, sleep, calories).
                    # Intersect the user's collected metrics with the eligible vital
                    # set so we only offer alerts the user can actually set.
                    read_failed = False
                    try:
                        eligible = set(eligible_alert_metrics())
                        avail = repo.get_metric_availability_map(app_user_id)
                        collected = sorted(
                            mt for mt, it in avail.items()
                            if getattr(it, "is_available", False) and mt in eligible
                        )
                    except Exception as exc:
                        logging.warning("set_threshold availability read failed %s: %s", app_user_id, exc)
                        collected = []
                        read_failed = True
                    if read_failed:
                        listing = "I couldn't read your collected data list right now."
                    elif collected:
                        listing = "Vital data currently collected for you:\n" + "\n".join(f"- {m}" for m in collected)
                    else:
                        listing = "No vital-sign data eligible for alerts is being collected for you yet."
                    msg = (
                        listing + "\n\n"
                        "I can alert you when a vital sign (whose current value matters, like heart rate or temperature) goes out of your normal range and stays there.\n\n"
                        "Type the metric you want an alert on. (e.g. heart rate)\n"
                        "Then pick Auto (a medical reference for your age/sex) or Manual (set the numbers yourself).\n\n"
                        "Check your alerts with /alerts, and remove one with /clear_threshold."
                    )
                    # Open a follow-up flow: the next message (a "<metric> <lower>
                    # <upper> [sustain]" spec) is read as the threshold to set.
                    save_pending_input_state(chat_id, "set_threshold")
                    msg = msg.rstrip() + "\n\n" + _pending_skip_hint()
                    reply(msg, route_override="settings")
                    return func.HttpResponse("OK", status_code=200)

                # /set_threshold (or /setting)
                parsed, err = parse_set_threshold(parts[1:], default_sustain)
                if err:
                    if err.startswith("unknown_metric"):
                        msg = (f"Unknown metric: {err.split(':',1)[1]}\nSupported: {supported_metrics_help(False)}")
                    elif err == "lower_ge_upper":
                        msg = "Lower bound must be smaller than upper bound."
                    elif err in {"bad_number", "bad_sustain", "no_bound"}:
                        msg = ("Check the numbers. Use '-' for a bound you want to leave unset.")
                    else:  # usage
                        msg = (
                            "Usage: /set_threshold <metric> <lower> <upper> [sustain_min]\n"
                            "e.g. /set_threshold hr 50 120 10\n"
                            "e.g. (upper only): /set_threshold temperature - 37.8\n"
                            f"Supported metrics: {supported_metrics_help(False)}"
                        )
                    reply(msg, route_override="settings")
                    return func.HttpResponse("OK", status_code=200)

                ok = repo.save_alert_threshold(MetricAlertThreshold(
                    app_user_id=app_user_id,
                    metric_type=parsed["metric_type"],
                    lower=parsed["lower"],
                    upper=parsed["upper"],
                    sustain_minutes=parsed["sustain_minutes"],
                    enabled=True,
                ))
                if ok:
                    line = format_threshold_line(parsed["metric_type"], parsed["lower"], parsed["upper"], parsed["sustain_minutes"], is_korean)
                    msg = "Saved your alert threshold:\n" + line
                else:
                    msg = ("Failed to save the threshold (storage error). Please try again shortly.")
                reply(msg, route_override="settings")
            except Exception as e:
                logging.error(f"threshold command error app_user_id={app_user_id}: {e}")
                reply(
                    "Server error while updating alert settings. Please try again shortly.",
                    route_override="settings", execution_status="error",
                    error_type=type(e).__name__, error_message=str(e),
                )
            return func.HttpResponse("OK", status_code=200)

        # =================================================================
        # /login — switch this chat to a different EXISTING UserId (passcode-gated)
        # =================================================================
        if command == "/login":
            is_korean = False

            # One-shot form: /login <UserId> <Passcode>
            if len(parts) >= 3:
                target_id = str(parts[1]).strip()
                passcode = str(parts[2]).strip()
                try:
                    user_ent = get_registered_user(target_id)
                except Exception as e:
                    logging.error(f"login lookup error app_user_id={target_id}: {e}")
                    reply("Server error during login. Please try again shortly.",
                          route_override="login", execution_status="error",
                          error_type=type(e).__name__, error_message=str(e))
                    return func.HttpResponse("OK", status_code=200)
                if not user_ent:
                    reply((f"No UserId matching '{target_id}'."), route_override="login")
                    return func.HttpResponse("OK", status_code=200)
                try:
                    verified, attempt_status = _verify_passcode_with_throttle(
                        chat_id, target_id, user_ent, passcode
                    )
                except Exception as e:
                    logging.error("login throttle error app_user_id=%s: %s", target_id, e)
                    reply(
                        "Server error during login. Please try again shortly.",
                        route_override="login",
                        execution_status="error",
                        error_type=type(e).__name__,
                        error_message=str(e),
                    )
                    return func.HttpResponse("OK", status_code=200)
                if not verified:
                    if not attempt_status["allowed"]:
                        message = _passcode_lockout_message(
                            attempt_status["retry_after_seconds"]
                        )
                    else:
                        message = ("That passcode doesn't match.")
                    reply(message, route_override="login")
                    return func.HttpResponse("OK", status_code=200)
                if save_user_mapping(chat_id, target_id):
                    _refresh_availability_async(target_id)
                    reply((f"Switched to '{target_id}'."),
                          route_override="login", app_user_id_override=target_id)
                else:
                    reply(("Something went wrong switching accounts. Please try again shortly."),
                          route_override="login")
                return func.HttpResponse("OK", status_code=200)

            # Interactive: open a follow-up flow that asks for the UserId, then passcode.
            if not save_pending_input_state(chat_id, "login", {"stage": "userid"}):
                reply(
                    (
                        "I couldn't start login safely. Please try again shortly."
                    ),
                    route_override="login",
                    execution_status="error",
                    error_type="pending_state_save_failed",
                )
                return func.HttpResponse("OK", status_code=200)
            msg = (
                "Please enter the UserId you want to switch to.\n"
                "If it exists, I'll ask for its passcode and switch once it checks out.\n\n"
                "(Type 'cancel' to stay on your current account.)"
            )
            reply(msg, route_override="login")
            return func.HttpResponse("OK", status_code=200)

        # =================================================================
        # /register_user
        # =================================================================
        if command == "/register_user":
            is_korean = False
            usage_msg = (
                "Usage: /register_user <AppUserId> <Passcode>\n\n"
                "- AppUserId: an ID of your choice (make one up)\n"
                "- Passcode: a passcode of your choice (make one up)\n\n"
                "e.g. /register_user kist010 1234\n\n"
                "You only need to register once. After that, send /start to begin onboarding."
            )
            if len(parts) < 3:
                reply(usage_msg, route_override="register")
                return func.HttpResponse("OK", status_code=200)

            target_id = str(parts[1]).strip()
            passcode = str(parts[2]).strip()
            if not target_id or not passcode:
                reply(usage_msg, route_override="register")
                return func.HttpResponse("OK", status_code=200)

            try:
                # Enforce a globally-unique AppUserId at the Telegram registration
                # step: if the id already exists, refuse it (prevents two people
                # sharing one id and mixing their health data). The one exception is
                # when THIS chat already owns the id (idempotent re-send), so a user
                # is never locked out of their own id.
                existing_user = get_registered_user(target_id)
                if existing_user:
                    if app_user_id == target_id:
                        save_user_mapping(chat_id, target_id)
                        reply(
                            (f"You're already registered as {target_id}.\n"
                             "Send /start to begin onboarding."),
                            route_override="register",
                            app_user_id_override=target_id,
                        )
                    else:
                        reply(
                            (f"This AppUserId is already taken: {target_id}\n"
                             "Please choose a different AppUserId.\n\n"
                             "e.g. /register_user kist011 1234"),
                            route_override="register",
                            app_user_id_override=target_id,
                        )
                    return func.HttpResponse("OK", status_code=200)

                ok, reason = upsert_registered_user(target_id, passcode)
                if ok:
                    save_user_mapping(chat_id, target_id)
                    app_user_id = target_id
                    clear_register_withings_state(chat_id)
                    reply(
                        f"User registration saved.\nAppUserId: {target_id}",
                        route_override="register",
                        app_user_id_override=target_id,
                    )
                    reply(
                        (
                            "Next, open the Custom Logger App and log in with the AppUserId "
                            f"({target_id}) and Passcode you just registered, so your health "
                            "data starts syncing.\n\n"
                            "Then send /start here to set up your profile and begin using the chatbot."
                        ),
                        route_override="register",
                        app_user_id_override=target_id,
                    )
                elif reason == "duplicate_different_passcode":
                    reply(
                        "This AppUserId already exists with a different passcode. Please use another AppUserId.",
                        route_override="register",
                        app_user_id_override=target_id,
                    )
                else:
                    reply("Registration failed. Check your input and try again.", route_override="register")
            except Exception as e:
                logging.error(f"register_user error app_user_id={target_id}: {e}")
                reply(
                    "Server configuration error. Please contact admin.",
                    route_override="register",
                    execution_status="error",
                    error_type=type(e).__name__,
                    error_message=str(e),
                )

            return func.HttpResponse("OK", status_code=200)

        # =================================================================
        # /register_withings
        # =================================================================
        if command in {"/register_withings", "/regirster_withings"}:
            if len(parts) < 2:
                reply("Usage: /register_withings <AppUserId>", route_override="register_withings")
                return func.HttpResponse("OK", status_code=200)

            target_id = str(parts[1]).strip()
            if not target_id:
                reply("Usage: /register_withings <AppUserId>", route_override="register_withings")
                return func.HttpResponse("OK", status_code=200)

            try:
                user_ent = get_registered_user(target_id)
                if not user_ent:
                    reply(
                        "Unknown AppUserId. Run /register_user <AppUserId> <Passcode> first.",
                        route_override="register_withings",
                        app_user_id_override=target_id,
                    )
                    return func.HttpResponse("OK", status_code=200)

                if not save_register_withings_state(chat_id, target_id):
                    reply(
                        "Could not start passcode verification. Please try again.",
                        route_override="register_withings",
                        app_user_id_override=target_id,
                    )
                    return func.HttpResponse("OK", status_code=200)

                reply(
                    (
                        f"Passcode verification required for {target_id}.\n"
                        "Please enter your passcode in the next message.\n"
                        "Use /cancel to abort."
                    ),
                    route_override="register_withings",
                    app_user_id_override=target_id,
                )
            except Exception as e:
                logging.error(f"register_withings init error app_user_id={target_id}: {e}")
                reply(
                    "Server configuration error. Please contact admin.",
                    route_override="register_withings",
                    app_user_id_override=target_id,
                    execution_status="error",
                    error_type=type(e).__name__,
                    error_message=str(e),
                )

            return func.HttpResponse("OK", status_code=200)

        # =================================================================
        # /register (legacy redirect)
        # =================================================================
        if command == "/register":
            reply(
                (
                    "This command is updated.\n"
                    "1) /register_user <AppUserId> <Passcode>\n"
                    "2) /register_withings <AppUserId>"
                ),
                route_override="register",
            )
            return func.HttpResponse("OK", status_code=200)

        # =================================================================
        # Pending Withings passcode flow
        # =================================================================
        pending_state = register_state_for_log
        if pending_state:
            if command == "/cancel":
                clear_register_withings_state(chat_id)
                reply("Withings registration canceled.", route_override="register_withings")
                return func.HttpResponse("OK", status_code=200)

            if user_text.startswith("/"):
                reply(
                    "Passcode verification is pending. Enter passcode or /cancel.",
                    route_override="register_withings",
                )
                return func.HttpResponse("OK", status_code=200)

            target_id = str(pending_state.get("AppUserId", "")).strip()
            if not target_id:
                clear_register_withings_state(chat_id)
                reply("Invalid pending state. Run /register_withings again.", route_override="register_withings")
                return func.HttpResponse("OK", status_code=200)

            try:
                user_ent = get_registered_user(target_id)
                if not user_ent:
                    clear_register_withings_state(chat_id)
                    reply(
                        "No matching AppUserId found. Run /register_user <AppUserId> <Passcode> first.",
                        route_override="register_withings",
                        app_user_id_override=target_id,
                    )
                    return func.HttpResponse("OK", status_code=200)

                verified, attempt_status = _verify_passcode_with_throttle(
                    chat_id, target_id, user_ent, user_text
                )
                if not verified:
                    if not attempt_status["allowed"]:
                        clear_register_withings_state(chat_id)
                        message = _passcode_lockout_message(
                            attempt_status["retry_after_seconds"]
                        )
                    else:
                        message = "Invalid passcode. Try again or use /cancel."
                    reply(
                        message,
                        route_override="register_withings",
                        app_user_id_override=target_id,
                    )
                    return func.HttpResponse("OK", status_code=200)

                clear_register_withings_state(chat_id)
                auth_url = _build_withings_auth_url(req, target_id, str(chat_id))
                if not auth_url:
                    reply(
                        "Failed to build auth link. Check server settings.",
                        route_override="withings_auth",
                        app_user_id_override=target_id,
                    )
                    return func.HttpResponse("OK", status_code=200)

                reply(
                    (
                        "Click the link below to complete Withings authentication.\n\n"
                        f"{auth_url}\n\n"
                        "Once authenticated, the bot will connect automatically."
                    ),
                    route_override="withings_auth",
                    app_user_id_override=target_id,
                )
                return func.HttpResponse("OK", status_code=200)

            except Exception as e:
                logging.error(f"register_withings passcode check error app_user_id={target_id}: {e}")
                reply(
                    "Server error during passcode verification.",
                    route_override="register_withings",
                    app_user_id_override=target_id,
                    execution_status="error",
                    error_type=type(e).__name__,
                    error_message=str(e),
                )
                return func.HttpResponse("OK", status_code=200)

        # =================================================================
        # Check user registration
        # =================================================================
        app_user_id = app_user_id or _safe_app_user_id(chat_id)
        if not app_user_id:
            reply(
                (
                    "Your chat account is not yet linked to a Sensei sensor account.\n\n"
                    "1. Install the Custom Logger App using the installation link provided by the research team.\n"
                    "2. Register a new account or sign in to your existing account in the app.\n"
                    "3. Complete the initial sensor-data backfill.\n"
                    "4. Select Generate Authorization PIN in the app.\n"
                    "5. Send the six-digit PIN in this chat within five minutes.\n\n"
                    "The existing /register_user and /login commands remain available."
                ),
                route_override="register",
            )
            return func.HttpResponse("OK", status_code=200)

        # =================================================================
        # /update_timezone — set or change the user's timezone (onboarding)
        # =================================================================
        if command == "/update_timezone":
            if not TZ_ONBOARDING_AVAILABLE or handle_update_timezone_command is None:
                reply(
                    "Timezone setting is not available on this server deployment.",
                    route_override="onboarding",
                    execution_status="error",
                    error_type="timezone_onboarding_unavailable",
                )
                return func.HttpResponse("OK", status_code=200)
            arg = " ".join(parts[1:]).strip()
            is_korean = False
            try:
                msg = handle_update_timezone_command(app_user_id, arg, is_korean=is_korean)
                # Bare command: open a follow-up flow so the next message (a number
                # or region) is read as the timezone choice rather than a chat query.
                if not arg:
                    save_pending_input_state(chat_id, "timezone")
                    msg = msg.rstrip() + "\n\n" + _pending_skip_hint()
                reply(msg, route_override="onboarding")
            except Exception as e:
                logging.error(f"update_timezone error app_user_id={app_user_id}: {e}")
                reply(
                    "Server error while setting timezone. Please try again shortly.",
                    route_override="onboarding",
                    execution_status="error",
                    error_type=type(e).__name__,
                    error_message=str(e),
                )
            return func.HttpResponse("OK", status_code=200)

        # =================================================================
        # /new and /resume — conversation episode controls
        # =================================================================
        if command in {"/new", "/resume"}:
            if not EPISODE_SERVICE_AVAILABLE or episode_thread_id_for_chat is None:
                reply(
                    "Conversation episode controls are not available on this server deployment.",
                    route_override="episode",
                    execution_status="error",
                    error_type="episode_service_unavailable",
                )
                return func.HttpResponse("OK", status_code=200)
            is_korean = False
            thread_id = episode_thread_id_for_chat(chat_id)
            try:
                if command == "/new":
                    msg = handle_new_command(app_user_id, thread_id, is_korean=is_korean)
                    _refresh_availability_async(app_user_id)
                else:
                    msg = handle_resume_command(app_user_id, thread_id, is_korean=is_korean)
                reply(msg, route_override="episode")
            except Exception as e:
                logging.error(f"episode command {command} error app_user_id={app_user_id}: {e}")
                reply(
                    "Server error while handling the conversation command. Please try again shortly.",
                    route_override="episode",
                    execution_status="error",
                    error_type=type(e).__name__,
                    error_message=str(e),
                )
            return func.HttpResponse("OK", status_code=200)

        # =================================================================
        # /start — begin interview for first-time or returning users
        # =================================================================
        if command == "/start":
            _refresh_availability_async(app_user_id)
            # `/start` is intentionally idempotent for completed users. Profile
            # and notification settings are changed only through `/update_info`
            # (or their dedicated setting commands), matching the original bot.
            msg = _start_interview_message(app_user_id)
            reply_interview(msg)
            return func.HttpResponse("OK", status_code=200)

        # =================================================================
        # /update_info — re-do interview to update profile
        # =================================================================
        if command == "/update_info":
            msg = begin_interview_update(app_user_id)
            reply_interview(msg)
            return func.HttpResponse("OK", status_code=200)

        # =================================================================
        # Interview in-progress gating
        # =================================================================
        interview_status = get_interview_status(app_user_id)
        if interview_status == "in_progress":
            if command == "/cancel":
                msg = cancel_interview(app_user_id)
                reply(msg, route_override="interview")
                return func.HttpResponse("OK", status_code=200)

            response_msg, is_done = process_interview_answer(app_user_id, user_text)
            reply_interview(response_msg)
            if is_done:
                # Onboarding finished: run the initial data-availability schema check now.
                _refresh_availability_async(app_user_id)
            return func.HttpResponse("OK", status_code=200)

        # =================================================================
        # /cancel with nothing to cancel — answer directly, never forward to
        # the chatbot graph (it would be misread as a vague question).
        # =================================================================
        if command == "/cancel":
            is_korean = False
            reply(
                "There is nothing in progress to cancel. Just type a health question, or send /help to see available commands.",
                route_override="command",
            )
            return func.HttpResponse("OK", status_code=200)

        # Unknown slash command: do not forward to the chatbot graph.
        if command.startswith("/"):
            reply(
                f"Unknown command: {command}\nSend /help to see available commands.",
                route_override="command",
            )
            return func.HttpResponse("OK", status_code=200)

        # =================================================================
        # Chatbot availability check
        # =================================================================
        if not CHATBOT_AVAILABLE:
            reply(
                "Chatbot modules are not available on this server deployment.",
                route_override="chat",
                execution_status="error",
                error_type="chatbot_unavailable",
            )
            return func.HttpResponse("OK", status_code=200)

        # =================================================================
        # Main chatbot (LangGraph)
        # =================================================================
        # Keep "typing..." indicator visible throughout the entire processing.
        send_telegram_typing(chat_id)
        _typing_done = threading.Event()
        typing_thread = threading.Thread(
            target=_keep_typing, args=(chat_id, _typing_done), daemon=True,
        )
        typing_thread.start()

        try:
            if handle_user_message is None:
                raise RuntimeError("chatbot entrypoint is unavailable")
            result = await handle_user_message(
                app_user_id=app_user_id,
                telegram_chat_id=str(chat_id),
                message_text=user_text,
                inbound_payload={
                    "telegram_update_id": update_id,
                    "telegram_message_id": message_id,
                    "conversation_id": conversation_id,
                    "turn_id": turn_id,
                    "source": "telegram_webhook",
                    "reply_to_text": reply_to_text,
                    "reply_to_role": reply_to_role,
                },
            )
            result_payload = result.payload if isinstance(result.payload, dict) else {}
            final_state = (
                result_payload.get("final_state")
                if isinstance(result_payload.get("final_state"), dict)
                else {}
            )
            bot_response = result.text

            _typing_done.set()
            reply(
                bot_response,
                route_override=result.route,
                intent=result.intent or "",
                final_state=final_state,
                execution_status=result.execution_status,
            )
        except Exception as chat_err:
            _typing_done.set()
            logging.error(f"Chatbot error for chat_id={chat_id}: {chat_err}", exc_info=True)
            err_detail = ""
            if str(os.environ.get("CHATBOT_DEBUG_ERRORS", "")).strip().lower() in {"1", "true", "yes", "on"}:
                import traceback as _tb
                frames = _tb.format_exc().strip().splitlines()
                err_detail = (
                    "\n\n[debug] "
                    + "".join(_tb.format_exception_only(type(chat_err), chat_err)).strip()
                    + ("\n" + "\n".join(frames[-6:]) if frames else "")
                )
            reply(
                "Sorry, something went wrong while processing your question. Please try again shortly." + err_detail,
                route_override="chat",
                execution_status="error",
                error_type=type(chat_err).__name__,
                error_message=str(chat_err),
            )

        return func.HttpResponse("OK", status_code=200)

    except Exception as e:
        logging.error(f"Telegram Webhook Error: {e}", exc_info=True)
        # Always return 200 to prevent Telegram from retrying the same update,
        # which would cause duplicate/repeated responses to the user.
        return func.HttpResponse("OK", status_code=200)
