# -*- coding: utf-8 -*-
"""
Blueprint: Operational/diagnostic endpoints (sync_now, diag_queue, enqueue_ping).
"""

import json
import time
from typing import Optional

import azure.functions as func
import azure.durable_functions as df
from azure_storage_resources import create_queue_client
from queue_contracts import WITHINGS_WEBHOOK_QUEUE

from functions.config import (
    TABLE_CONN_STR,
    TELEGRAM_CONVERSATION_ADMIN_KEY,
)
from functions.shared import _safe_int, _get_webhook_queue_client
from functions.ingest_bp import fetch_and_ingest
from functions.telegram_conversation_log import (
    flush_pending_conversation_events_to_adx,
    get_conversation_log_diagnostics,
)

bp = df.Blueprint()


def _telegram_log_admin_ok(req: func.HttpRequest) -> bool:
    if not TELEGRAM_CONVERSATION_ADMIN_KEY:
        return True
    supplied = req.headers.get("x-admin-key") or req.params.get("key")
    return supplied == TELEGRAM_CONVERSATION_ADMIN_KEY


@bp.route(
    route="sync_now",
    methods=["POST", "GET"],
    auth_level=func.AuthLevel.FUNCTION,
)
def sync_now(req: func.HttpRequest) -> func.HttpResponse:
    """
    Force-run fetch_and_ingest for a specific Withings user_id (real userid).
    Example: /api/sync_now?userid=123456&appli=44
    """
    try:
        userid = req.params.get("userid")
        appli = req.params.get("appli")
        if not userid:
            return func.HttpResponse("Missing userid", status_code=400)

        args = {
            "mode": "manual",
            "user_id": str(userid),
            "appli": _safe_int(appli) if appli is not None else None,
            "date": req.params.get("date"),
            "start_ts": _safe_int(req.params.get("start_ts")),
            "end_ts": _safe_int(req.params.get("end_ts")),
            "received_ts": int(time.time()),
            "retry_count": 0,
        }
        result = fetch_and_ingest(args)
        return func.HttpResponse(json.dumps(result, ensure_ascii=False), status_code=200, mimetype="application/json")
    except Exception as e:
        return func.HttpResponse(f"Error: {e}", status_code=500)


def _storage_account_name_from_conn_str(conn_str: str) -> Optional[str]:
    try:
        parts = {}
        for p in str(conn_str).split(";"):
            if "=" in p:
                k, v = p.split("=", 1)
                parts[k.strip()] = v.strip()
        return parts.get("AccountName")
    except Exception:
        return None


@bp.route(
    route="diag_queue",
    methods=["GET"],
    auth_level=func.AuthLevel.FUNCTION,
)
def diag_queue(req: func.HttpRequest) -> func.HttpResponse:
    try:
        if not TABLE_CONN_STR:
            return func.HttpResponse("AzureWebJobsStorage is missing.", status_code=500)

        qc = create_queue_client(TABLE_CONN_STR, WITHINGS_WEBHOOK_QUEUE)
        try:
            props = qc.get_queue_properties()
            approx = getattr(props, "approximate_message_count", None)
        except Exception:
            approx = None

        peek = []
        try:
            msgs = qc.peek_messages(max_messages=5)
            for m in msgs:
                peek.append({"id": getattr(m, "id", None), "inserted_on": str(getattr(m, "inserted_on", "")), "content": getattr(m, "content", None)})
        except Exception:
            pass

        out = {
            "binding_queue_name": WITHINGS_WEBHOOK_QUEUE,
            "storage_account": _storage_account_name_from_conn_str(TABLE_CONN_STR),
            "queue_url": getattr(qc, "url", None),
            "approximate_message_count": approx,
            "peek": peek,
        }
        return func.HttpResponse(json.dumps(out, ensure_ascii=False), status_code=200, mimetype="application/json")
    except Exception as e:
        return func.HttpResponse(f"diag_queue error: {e}", status_code=500)


@bp.route(
    route="enqueue_ping",
    methods=["POST", "GET"],
    auth_level=func.AuthLevel.FUNCTION,
)
def enqueue_ping(req: func.HttpRequest) -> func.HttpResponse:
    try:
        qc = _get_webhook_queue_client()
        payload = {"ping": True, "received_ts": int(time.time())}
        qc.send_message(json.dumps(payload))
        return func.HttpResponse(json.dumps({"ok": True, "queue": WITHINGS_WEBHOOK_QUEUE, "payload": payload}), status_code=200, mimetype="application/json")
    except Exception as e:
        return func.HttpResponse(f"enqueue_ping error: {e}", status_code=500)


@bp.route(
    route="diag_telegram_conversation_log",
    methods=["GET"],
    auth_level=func.AuthLevel.FUNCTION,
)
def diag_telegram_conversation_log(req: func.HttpRequest) -> func.HttpResponse:
    try:
        if not _telegram_log_admin_ok(req):
            return func.HttpResponse("Forbidden", status_code=403)
        result = get_conversation_log_diagnostics()
        return func.HttpResponse(json.dumps(result, ensure_ascii=False, default=str), status_code=200, mimetype="application/json")
    except Exception as e:
        return func.HttpResponse(f"diag_telegram_conversation_log error: {e}", status_code=500)


@bp.route(
    route="flush_telegram_conversation_log_now",
    methods=["POST", "GET"],
    auth_level=func.AuthLevel.FUNCTION,
)
def flush_telegram_conversation_log_now(req: func.HttpRequest) -> func.HttpResponse:
    try:
        if not _telegram_log_admin_ok(req):
            return func.HttpResponse("Forbidden", status_code=403)
        limit = _safe_int(req.params.get("limit"))
        result = flush_pending_conversation_events_to_adx(limit=limit)
        return func.HttpResponse(
            json.dumps({"ok": True, "result": result}, ensure_ascii=False, default=str),
            status_code=200,
            mimetype="application/json",
        )
    except Exception as e:
        return func.HttpResponse(f"flush_telegram_conversation_log_now error: {e}", status_code=500)
