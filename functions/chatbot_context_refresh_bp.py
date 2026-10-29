# -*- coding: utf-8 -*-
"""Blueprint for chatbot operational context refresh."""

import json
import logging

import azure.functions as func
import azure.durable_functions as df
from azure.storage.queue import QueueClient
from azure_storage_resources import create_queue_client

from chatbot.availability_refresh import refresh_metric_availability
from chatbot.operational_storage import OperationalTableRepository
from queue_contracts import METRIC_AVAILABILITY_QUEUE

bp = df.Blueprint()


def _queue_client() -> QueueClient:
    import os

    connection_string = os.environ.get("AzureWebJobsStorage", "")
    if not connection_string:
        raise RuntimeError("AzureWebJobsStorage is missing.")
    return create_queue_client(connection_string, METRIC_AVAILABILITY_QUEUE)


@bp.function_name(name="ChatbotMetricAvailabilityRefreshTimer")
@bp.timer_trigger(
    schedule="0 0 3 * * *",
    arg_name="timer",
    run_on_startup=False,
    use_monitor=True,
)
def chatbot_metric_availability_refresh_timer(timer: func.TimerRequest) -> None:
    try:
        repository = OperationalTableRepository(fail_soft=False)
        profiles = repository.list_active_user_profiles()
        qc = _queue_client()
        enqueued = 0
        for profile in profiles:
            qc.send_message(json.dumps({"appUserId": profile.app_user_id}, ensure_ascii=False))
            enqueued += 1
        logging.info("ChatbotMetricAvailabilityRefreshTimer enqueued=%s", enqueued)
    except Exception:
        logging.exception("ChatbotMetricAvailabilityRefreshTimer failed")


@bp.function_name(name="ChatbotMetricAvailabilityRefreshQueue")
@bp.queue_trigger(
    arg_name="msg",
    queue_name=METRIC_AVAILABILITY_QUEUE,
    connection="AzureWebJobsStorage",
)
def chatbot_metric_availability_refresh_queue(msg: func.QueueMessage) -> None:
    try:
        payload = json.loads(msg.get_body().decode("utf-8"))
    except Exception:
        logging.exception("invalid metric availability refresh payload")
        return
    app_user_id = str(payload.get("appUserId") or payload.get("userId") or "").strip()
    if not app_user_id:
        logging.warning("metric availability payload missing appUserId")
        return
    try:
        result = refresh_metric_availability(app_user_id)
        logging.info("ChatbotMetricAvailabilityRefreshQueue result=%s", result)
    except Exception:
        logging.exception("ChatbotMetricAvailabilityRefreshQueue failed app_user_id=%s", app_user_id)
        raise


@bp.route(
    route="refresh_chatbot_metric_availability",
    methods=["POST", "GET"],
    auth_level=func.AuthLevel.FUNCTION,
)
def refresh_chatbot_metric_availability(req: func.HttpRequest) -> func.HttpResponse:
    try:
        app_user_id = req.params.get("app_user_id") or req.params.get("user_id") or req.params.get("userid")
        enqueue = str(req.params.get("enqueue") or "").strip().lower() in {"1", "true", "yes"}
        if not app_user_id:
            try:
                body = req.get_json()
            except Exception:
                body = {}
            if isinstance(body, dict):
                app_user_id = str(body.get("appUserId") or body.get("userId") or "")
                enqueue = enqueue or bool(body.get("enqueue"))
        if not app_user_id:
            return func.HttpResponse("Missing app_user_id", status_code=400)
        if enqueue:
            _queue_client().send_message(json.dumps({"appUserId": app_user_id}, ensure_ascii=False))
            result = {"queued": True, "appUserId": app_user_id}
        else:
            result = refresh_metric_availability(str(app_user_id))
        return func.HttpResponse(json.dumps(result, ensure_ascii=False, default=str), status_code=200, mimetype="application/json")
    except Exception as exc:
        logging.exception("refresh_chatbot_metric_availability failed")
        return func.HttpResponse(f"refresh_chatbot_metric_availability error: {exc}", status_code=500)
