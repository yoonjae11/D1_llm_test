# -*- coding: utf-8 -*-
"""
Blueprint: Webhook queue worker (near-real-time Withings event processing).
"""

import logging
import json

import azure.functions as func
import azure.durable_functions as df

from functions.config import (
    WEBHOOK_RETRY_ON_EMPTY_ENABLE, WEBHOOK_RETRY_ON_EMPTY_MAX,
    WEBHOOK_RETRY_ON_EMPTY_BASE_DELAY_SECONDS,
    RETRY_ON_PARTIAL_EMPTY_ENABLE, WEBHOOK_RETRY_SLEEP_DELAYS,
)
from queue_contracts import WITHINGS_WEBHOOK_QUEUE
from functions.shared import _safe_int, _get_webhook_queue_client
from functions.ingest_bp import fetch_and_ingest

bp = df.Blueprint()


@bp.function_name(name="WithingsWebhookQueueWorker")
@bp.queue_trigger(
    arg_name="msg",
    queue_name=WITHINGS_WEBHOOK_QUEUE,
    connection="AzureWebJobsStorage",
)
def withings_webhook_queue_worker(msg: func.QueueMessage) -> None:
    try:
        payload = json.loads(msg.get_body().decode("utf-8"))
    except Exception as e:
        logging.error(f"[QueueWorker] Invalid queue message (not JSON): {e}")
        return

    if payload.get("ping") is True and not payload.get("userid"):
        logging.info("[QueueWorker] ping message received (no-op).")
        return

    withings_id = str(payload.get("userid") or "")
    if not withings_id:
        logging.warning("[QueueWorker] Queue message missing userid; skipping.")
        return

    appli = payload.get("appli")
    try:
        appli = int(appli) if appli is not None else None
    except Exception:
        appli = None

    retry_count = _safe_int(payload.get("_retry")) or 0

    args = {
        "mode": "webhook",
        "user_id": withings_id,
        "appli": appli,
        "date": payload.get("date"),
        "start_ts": _safe_int(payload.get("startdate")) or _safe_int(payload.get("start_ts")),
        "end_ts": _safe_int(payload.get("enddate")) or _safe_int(payload.get("end_ts")),
        "received_ts": _safe_int(payload.get("received_ts")),
        "retry_count": retry_count,
    }

    result = fetch_and_ingest(args)
    logging.info(f"[QueueWorker] userid={withings_id} appli={appli} retry={retry_count} result={result}")

    if not isinstance(result, dict):
        return

    status = result.get("status")
    if status == "lease_busy":
        try:
            _requeue_webhook_payload(payload, retry_count, delay_seconds=15)
        except Exception:
            pass
        return

    if status != "ok":
        return

    # Enhanced per-appli "empty data" detection with sleep-specific long retry
    want_retry = False
    is_sleep_appli = (appli == 44)
    max_retries = len(WEBHOOK_RETRY_SLEEP_DELAYS) if is_sleep_appli else WEBHOOK_RETRY_ON_EMPTY_MAX

    if WEBHOOK_RETRY_ON_EMPTY_ENABLE and retry_count < max_retries:
        raw_records = _safe_int(result.get("raw_records")) or 0
        ingested = _safe_int(result.get("ingested")) or 0
        counts_by_type = result.get("counts_by_type") or {}

        if raw_records == 0 and ingested == 0:
            want_retry = True

        if RETRY_ON_PARTIAL_EMPTY_ENABLE and not want_retry:
            if appli == 44:
                if (counts_by_type.get("Sleep", 0) + counts_by_type.get("SleepDetail", 0)) == 0:
                    want_retry = True
            elif appli == 16:
                if counts_by_type.get("Activity", 0) == 0 and counts_by_type.get("Workouts", 0) == 0:
                    want_retry = True

    if want_retry:
        if is_sleep_appli and retry_count < len(WEBHOOK_RETRY_SLEEP_DELAYS):
            delay = WEBHOOK_RETRY_SLEEP_DELAYS[retry_count]
        else:
            delay = WEBHOOK_RETRY_ON_EMPTY_BASE_DELAY_SECONDS * (2 ** retry_count)
        _requeue_webhook_payload(payload, retry_count + 1, delay_seconds=delay)
        logging.info(f"[QueueWorker] requeued userid={withings_id} appli={appli} retry={retry_count + 1} delay={delay}s ({delay // 3600}h{(delay % 3600) // 60}m)")


def _requeue_webhook_payload(payload: dict, retry_count: int, delay_seconds: int) -> None:
    try:
        qc = _get_webhook_queue_client()
        new_payload = dict(payload)
        new_payload["_retry"] = int(retry_count)
        qc.send_message(json.dumps(new_payload), visibility_timeout=max(0, int(delay_seconds)))
    except Exception as e:
        logging.error(f"[QueueWorker] Failed to requeue message: {e}")
