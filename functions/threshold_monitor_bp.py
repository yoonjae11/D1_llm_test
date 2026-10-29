# -*- coding: utf-8 -*-
"""Blueprint: user threshold alerts (15-min check) + scheduled daily reports.

The daily-report timer checks each user's saved local schedule every five
minutes. HTTP endpoints allow manual runs and diagnostics.
"""

import datetime as dt
import json
import logging
from zoneinfo import ZoneInfo

import azure.functions as func
import azure.durable_functions as df

from chatbot.config import get_settings
from chatbot.operational_storage import OperationalTableRepository
from chatbot.proactive.threshold_monitor import (
    build_daily_report,
    evaluate_user_thresholds,
    format_daily_report,
    send_daily_report,
)

bp = df.Blueprint()


def _local_now(timezone: str) -> dt.datetime:
    try:
        return dt.datetime.now(ZoneInfo(timezone))
    except Exception:
        return dt.datetime.now(dt.timezone.utc)


def _has_telegram_delivery_target(app_user_id: str) -> bool:
    """Cheap delivery check used before any proactive ADX/LLM work."""

    try:
        from functions.shared import get_chat_id_by_user

        return bool(get_chat_id_by_user(app_user_id))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Threshold check — every 15 min
# ---------------------------------------------------------------------------
@bp.function_name(name="ThresholdAlertTimer")
@bp.timer_trigger(schedule="0 */15 * * * *", arg_name="timer", run_on_startup=False, use_monitor=True)
def threshold_alert_timer(timer: func.TimerRequest) -> None:
    settings = get_settings()
    if not settings.threshold_alert_enabled:
        return
    try:
        repo = OperationalTableRepository(fail_soft=True)
        users = repo.list_users_with_alert_thresholds()
        checked = 0
        for uid in users:
            try:
                if not _has_telegram_delivery_target(uid):
                    continue
                evaluate_user_thresholds(uid, repository=repo)
                checked += 1
            except Exception as exc:
                logging.warning("threshold check failed for %s: %s", uid, exc)
        logging.info("ThresholdAlertTimer checked=%s", checked)
    except Exception:
        logging.exception("ThresholdAlertTimer failed")


# ---------------------------------------------------------------------------
# Daily report — runs every 5 minutes. Scheduled summary times are stored on a
# 5-minute grid (set-time rounds 14:46 -> 14:45), so a 5-min tick matches them
# exactly. Each user has zero or more scheduled times (onboarding / commands /
# chat); users with none are skipped. Real work (ADX + LLM) happens only on a
# matched tick, keeping the cadence cheap and load spread across 5-min buckets.
# ---------------------------------------------------------------------------
@bp.function_name(name="DailyReportTimer")
@bp.timer_trigger(schedule="0 */5 * * * *", arg_name="timer", run_on_startup=False, use_monitor=True)
def daily_report_timer(timer: func.TimerRequest) -> None:
    settings = get_settings()
    if not settings.daily_report_enabled:
        return
    try:
        repo = OperationalTableRepository(fail_soft=True)
        sent = 0
        for profile in repo.list_active_user_profiles():
            try:
                # Per-user schedule: zero or more local (hour, minute) times.
                # None/never-set and opted-out users have an empty list -> skipped.
                schedule = profile.summary_report_times
                if not schedule:
                    continue
                local = _local_now(profile.timezone_iana or settings.default_timezone)
                now_total = local.hour * 60 + local.minute
                local_date = local.date().isoformat()
                for (target_hour, target_min) in schedule:
                    # Times are stored on the 5-min grid; round here too as a safety
                    # net for any legacy off-grid value so it still lands on a tick.
                    target_total = int(round((int(target_hour) * 60 + int(target_min)) / 5.0)) * 5
                    if target_total >= 24 * 60:
                        target_total = 23 * 60 + 55
                    # Fire at the matching 5-min tick; 60-min catch-up covers a missed
                    # tick (deploy / cold start). Dedup -> once per (day + time).
                    delta = now_total - target_total
                    if delta < 0 or delta > 60:
                        continue
                    # Dedup per (local day + that time): re-setting to a new time on
                    # the same day re-arms it, while the same time won't double-send.
                    sent_key = f"{local_date}:{int(target_hour):02d}{int(target_min):02d}"
                    if repo.daily_report_already_sent(profile.app_user_id, sent_key):
                        continue
                    if not _has_telegram_delivery_target(profile.app_user_id):
                        continue
                    if send_daily_report(profile.app_user_id, repository=repo):
                        repo.mark_daily_report_sent(profile.app_user_id, sent_key)
                        sent += 1
            except Exception as exc:
                logging.warning("daily report failed for %s: %s", profile.app_user_id, exc)
        logging.info("DailyReportTimer sent=%s", sent)
    except Exception:
        logging.exception("DailyReportTimer failed")


# ---------------------------------------------------------------------------
# HTTP: manual run + diagnostics
# ---------------------------------------------------------------------------
def _app_user_id(req: func.HttpRequest) -> str:
    uid = req.params.get("app_user_id") or req.params.get("appUserId") or ""
    if not uid:
        try:
            body = req.get_json()
            if isinstance(body, dict):
                uid = str(body.get("appUserId") or body.get("app_user_id") or "")
        except Exception:
            pass
    return str(uid or "").strip()


@bp.route(
    route="run_threshold_check_now",
    methods=["POST", "GET"],
    auth_level=func.AuthLevel.FUNCTION,
)
def run_threshold_check_now(req: func.HttpRequest) -> func.HttpResponse:
    uid = _app_user_id(req)
    if not uid:
        return func.HttpResponse("Missing app_user_id", status_code=400)
    try:
        send = str(req.params.get("send") or "true").lower() not in {"false", "0", "no"}
        result = evaluate_user_thresholds(uid, send=send)
        return func.HttpResponse(json.dumps(result, default=str, ensure_ascii=False),
                                 status_code=200, mimetype="application/json")
    except Exception as exc:
        logging.exception("run_threshold_check_now failed")
        return func.HttpResponse(f"error: {exc}", status_code=500)


@bp.route(
    route="run_daily_report_now",
    methods=["POST", "GET"],
    auth_level=func.AuthLevel.FUNCTION,
)
def run_daily_report_now(req: func.HttpRequest) -> func.HttpResponse:
    uid = _app_user_id(req)
    if not uid:
        return func.HttpResponse("Missing app_user_id", status_code=400)
    try:
        preview = str(req.params.get("preview") or "").lower() in {"1", "true", "yes"}
        if preview:
            report = build_daily_report(uid)
            return func.HttpResponse(
                json.dumps({"report": report, "message": format_daily_report(report)}, default=str, ensure_ascii=False),
                status_code=200, mimetype="application/json")
        sent = send_daily_report(uid)
        return func.HttpResponse(json.dumps({"appUserId": uid, "sent": bool(sent)}, ensure_ascii=False),
                                 status_code=200, mimetype="application/json")
    except Exception as exc:
        logging.exception("run_daily_report_now failed")
        return func.HttpResponse(f"error: {exc}", status_code=500)


@bp.route(
    route="diag_threshold_alerts",
    methods=["GET"],
    auth_level=func.AuthLevel.FUNCTION,
)
def diag_threshold_alerts(req: func.HttpRequest) -> func.HttpResponse:
    uid = _app_user_id(req)
    if not uid:
        return func.HttpResponse("Missing app_user_id", status_code=400)
    try:
        repo = OperationalTableRepository(fail_soft=True)
        thresholds = [
            {"metric": t.metric_type, "lower": t.lower, "upper": t.upper,
             "sustainMinutes": t.sustain_minutes, "enabled": t.enabled,
             "lastAlertAtUtc": t.last_alert_at_utc}
            for t in repo.list_alert_thresholds(uid)
        ]
        evaluation = evaluate_user_thresholds(uid, repository=repo, send=False)
        return func.HttpResponse(
            json.dumps({"appUserId": uid, "thresholds": thresholds, "evaluation": evaluation},
                       default=str, ensure_ascii=False),
            status_code=200, mimetype="application/json")
    except Exception as exc:
        logging.exception("diag_threshold_alerts failed")
        return func.HttpResponse(f"error: {exc}", status_code=500)
