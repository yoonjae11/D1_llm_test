# -*- coding: utf-8 -*-
"""Blueprint for Phone STEPS system-initiated alert processing."""

import json
import logging

import azure.functions as func
import azure.durable_functions as df

from chatbot.proactive.system_steps_alert import (
    diagnostics_for_user,
    process_due_user_profiles,
    process_steps_event,
)
from queue_contracts import SYSTEM_PHONE_INGEST_EVENT_QUEUE, SYSTEM_STEPS_RETRY_QUEUE

bp = df.Blueprint()


def _payload_from_queue(msg: func.QueueMessage) -> dict:
    try:
        return json.loads(msg.get_body().decode("utf-8"))
    except Exception:
        logging.exception("invalid system steps queue payload")
        return {}


@bp.function_name(name="system_initiated_phone_ingest_queue")
@bp.queue_trigger(
    arg_name="msg",
    queue_name=SYSTEM_PHONE_INGEST_EVENT_QUEUE,
    connection="AzureWebJobsStorage",
)
def system_initiated_phone_ingest_queue(msg: func.QueueMessage) -> None:
    payload = _payload_from_queue(msg)
    if not payload:
        return
    try:
        result = process_steps_event(payload)
        logging.info("system_initiated_phone_ingest_queue result=%s", result)
    except Exception:
        logging.exception("system_initiated_phone_ingest_queue failed")
        raise


@bp.function_name(name="system_initiated_steps_retry_queue")
@bp.queue_trigger(
    arg_name="msg",
    queue_name=SYSTEM_STEPS_RETRY_QUEUE,
    connection="AzureWebJobsStorage",
)
def system_initiated_steps_retry_queue(msg: func.QueueMessage) -> None:
    payload = _payload_from_queue(msg)
    if not payload:
        return
    try:
        payload["retryCount"] = int(payload.get("retryCount") or 0)
        result = process_steps_event(payload, allow_missing_step_date=True)
        logging.info("system_initiated_steps_retry_queue result=%s", result)
    except Exception:
        logging.exception("system_initiated_steps_retry_queue failed")
        raise


@bp.function_name(name="SystemInitiatedStepsFallbackTimer")
@bp.timer_trigger(
    schedule="0 */15 * * * *",
    arg_name="timer",
    run_on_startup=False,
    use_monitor=True,
)
def system_initiated_steps_fallback_timer(timer: func.TimerRequest) -> None:
    try:
        result = process_due_user_profiles("fallback")
        logging.info("SystemInitiatedStepsFallbackTimer result=%s", result)
    except Exception:
        logging.exception("SystemInitiatedStepsFallbackTimer failed")


@bp.function_name(name="SystemInitiatedStepsReconciliationTimer")
@bp.timer_trigger(
    schedule="0 */15 * * * *",
    arg_name="timer",
    run_on_startup=False,
    use_monitor=True,
)
def system_initiated_steps_reconciliation_timer(timer: func.TimerRequest) -> None:
    try:
        result = process_due_user_profiles("reconciliation")
        logging.info("SystemInitiatedStepsReconciliationTimer result=%s", result)
    except Exception:
        logging.exception("SystemInitiatedStepsReconciliationTimer failed")


@bp.route(
    route="diag_system_initiated_steps",
    methods=["GET", "POST"],
    auth_level=func.AuthLevel.FUNCTION,
)
def diag_system_initiated_steps(req: func.HttpRequest) -> func.HttpResponse:
    try:
        app_user_id = req.params.get("app_user_id") or req.params.get("user_id") or req.params.get("userid")
        if not app_user_id:
            try:
                body = req.get_json()
            except Exception:
                body = {}
            app_user_id = str((body or {}).get("appUserId") or (body or {}).get("userId") or "")
        if not app_user_id:
            return func.HttpResponse("Missing app_user_id", status_code=400)
        result = diagnostics_for_user(str(app_user_id))
        return func.HttpResponse(json.dumps(result, ensure_ascii=False, default=str), status_code=200, mimetype="application/json")
    except Exception as exc:
        logging.exception("diag_system_initiated_steps failed")
        return func.HttpResponse(f"diag_system_initiated_steps error: {exc}", status_code=500)


@bp.route(
    route="run_system_initiated_steps_now",
    methods=["POST"],
    auth_level=func.AuthLevel.FUNCTION,
)
def run_system_initiated_steps_now(req: func.HttpRequest) -> func.HttpResponse:
    try:
        body = req.get_json()
        if not isinstance(body, dict):
            body = {}
        result = process_steps_event(body, allow_missing_step_date=True)
        return func.HttpResponse(json.dumps(result, ensure_ascii=False, default=str), status_code=200, mimetype="application/json")
    except Exception as exc:
        logging.exception("run_system_initiated_steps_now failed")
        return func.HttpResponse(f"run_system_initiated_steps_now error: {exc}", status_code=500)
