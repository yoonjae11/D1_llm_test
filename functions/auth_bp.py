# -*- coding: utf-8 -*-
"""
Blueprint: Withings OAuth authentication + webhook subscription.
"""

import logging
import time
import datetime

import azure.functions as func
import azure.durable_functions as df
from azure.data.tables import UpdateMode
from azure_storage_resources import create_table_client

from functions.config import (
    CLIENT_ID, CLIENT_SECRET, TABLE_CONN_STR, BASE_URL, WITHINGS_WEBHOOK_URL,
    ENV_NAME, WITHINGS_OAUTH_STATE_SECRET, WITHINGS_OAUTH_STATE_TTL_SECONDS,
    WITHINGS_OAUTH_ALLOW_LEGACY_LOCAL_STATE,
)
from functions.security import (
    OAuthStateConfigurationError,
    OAuthStateError,
    build_withings_redirect_uri,
    verify_oauth_state,
)
from functions.shared import (
    withings_post, save_user_mapping, send_telegram_message,
)
from functions.telegram_conversation_log import build_conversation_event, stage_conversation_event

bp = df.Blueprint()


def _stage_telegram_auth_message(chat_id, app_user_id: str, text: str, status: str = "ok", error: str = "") -> None:
    try:
        stage_conversation_event(
            build_conversation_event(
                event_type="bot_reply",
                role="assistant",
                direction="outbound",
                event_time_utc=datetime.datetime.now(datetime.timezone.utc),
                app_user_id=app_user_id or "",
                telegram_chat_id=chat_id,
                message_text=text or "",
                route="withings_auth",
                intent="",
                command="",
                execution_status=status,
                error_message=error,
            )
        )
    except Exception:
        pass


@bp.route(route="withings_auth")
@bp.durable_client_input(client_name="client")
async def withings_auth(req: func.HttpRequest, client: df.DurableOrchestrationClient) -> func.HttpResponse:
    code = req.params.get("code")
    state = req.params.get("state")

    try:
        redirect_uri = build_withings_redirect_uri(BASE_URL, req.url)
    except OAuthStateConfigurationError as exc:
        logging.error("Withings OAuth callback URL configuration error: %s", exc)
        return func.HttpResponse("OAuth callback is not configured.", status_code=500)

    webhook_uri = WITHINGS_WEBHOOK_URL
    if not webhook_uri:
        return func.HttpResponse("Error: WITHINGS_WEBHOOK_URL is missing in App Settings.", status_code=500)

    if not code:
        return func.HttpResponse("Error: Authorization Code Missing", status_code=400)

    try:
        app_user_id, telegram_chat_id = verify_oauth_state(
            state,
            WITHINGS_OAUTH_STATE_SECRET,
            WITHINGS_OAUTH_STATE_TTL_SECONDS,
            allow_legacy_local=WITHINGS_OAUTH_ALLOW_LEGACY_LOCAL_STATE,
            environment_name=ENV_NAME,
        )
    except OAuthStateConfigurationError as exc:
        logging.error("Withings OAuth state configuration error: %s", exc)
        return func.HttpResponse("OAuth state signing is not configured.", status_code=500)
    except OAuthStateError as exc:
        logging.warning("Rejected Withings OAuth callback state: %s", exc)
        return func.HttpResponse("Invalid or expired OAuth state.", status_code=400)

    try:
        res = withings_post(
            "https://wbsapi.withings.net/v2/oauth2",
            headers={},
            data={
                "action": "requesttoken",
                "grant_type": "authorization_code",
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "code": code,
                "redirect_uri": redirect_uri,
            },
        )

        if res.get("status") != 0:
            logging.warning("Withings OAuth token exchange rejected: %s", res)
            return func.HttpResponse(
                "Withings authorization could not be completed.", status_code=400
            )

        body = res["body"]
        withings_real_id = str(body["userid"])

        tc = create_table_client(TABLE_CONN_STR, "WithingsTokens")
        entity = {
            "PartitionKey": "withings",
            "RowKey": withings_real_id,
            "AppUserId": app_user_id,
            "access_token": body["access_token"],
            "refresh_token": body["refresh_token"],
            "expires_at": int(time.time()) + int(body["expires_in"]),
        }
        tc.upsert_entity(mode=UpdateMode.REPLACE, entity=entity)

        subscribe_to_webhook(body["access_token"], webhook_uri, 16)
        subscribe_to_webhook(body["access_token"], webhook_uri, 44)

        if telegram_chat_id:
            if save_user_mapping(telegram_chat_id, app_user_id):
                msg = (
                    f"\U0001f389 Authentication Successful!\n\nID: {app_user_id}\n"
                    "Withings account connected.\nBot is ready."
                )
                send_telegram_message(
                    telegram_chat_id,
                    msg,
                )
                _stage_telegram_auth_message(telegram_chat_id, app_user_id, msg)
            else:
                msg = "\u26a0\ufe0f Auth succeeded, but Telegram mapping save failed."
                send_telegram_message(telegram_chat_id, msg)
                _stage_telegram_auth_message(
                    telegram_chat_id,
                    app_user_id,
                    msg,
                    status="error",
                    error="Telegram mapping save failed.",
                )

        instance_id = f"backfill_{withings_real_id}"
        existing_instance = await client.get_status(instance_id)

        if existing_instance and existing_instance.runtime_status in [
            df.OrchestrationRuntimeStatus.Running,
            df.OrchestrationRuntimeStatus.Pending,
        ]:
            logging.info(f"Backfill already running for {withings_real_id}. Skipping new request.")
            return func.HttpResponse("\u2705 Setup Complete! (Backfill is already running).")

        await client.start_new(
            "backfill_orchestrator",
            instance_id=instance_id,
            client_input={"user_id": withings_real_id},
        )

        return func.HttpResponse("\u2705 Setup Complete! You can close this window now.")

    except Exception:
        logging.exception("Withings OAuth callback failed.")
        return func.HttpResponse(
            "Withings authorization failed due to a server error.", status_code=500
        )


def subscribe_to_webhook(token: str, url: str, appli: int):
    try:
        res = withings_post(
            "https://wbsapi.withings.net/notify",
            headers={"Authorization": f"Bearer {token}"},
            data={"action": "subscribe", "callbackurl": url, "appli": appli},
            timeout=10,
        )
        if res.get("status") != 0:
            logging.warning(f"[Withings] Webhook subscribe failed status={res.get('status')} appli={appli}")
    except Exception as e:
        logging.warning(f"[Withings] Webhook subscribe error appli={appli}: {e}")
