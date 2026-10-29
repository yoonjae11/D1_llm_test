# -*- coding: utf-8 -*-
"""
Main Function App (Durable) - Withings -> Queue -> Fetch -> ADX

Blueprint registration only. See functions/ for implementation details:
- auth_bp:     Withings OAuth authentication
- backfill_bp: Backfill orchestrator (past N days)
- ingest_bp:   fetch_and_ingest activity (data fetching + dedup + ADX)
- webhook_bp:  Queue worker (near-real-time webhook processing)
- telegram_bp: Telegram bot webhook
- ops_bp:      Operational endpoints (sync_now, diag_queue, enqueue_ping)
"""

import azure.functions as func
import azure.durable_functions as df

from chatbot.config import get_settings

# Validate process-wide chatbot settings while the Functions host indexes the
# app. In particular, an invalid KQL_GENERATE_METHOD must stop startup instead
# of silently changing behavior on the first user request.
_startup_settings = get_settings()

from functions.auth_bp import bp as auth_bp
from functions.backfill_bp import bp as backfill_bp
from functions.ingest_bp import bp as ingest_bp
from functions.webhook_bp import bp as webhook_bp
from functions.telegram_bp import bp as telegram_bp
from functions.telegram_conversation_log_bp import telegram_conversation_log_bp
from functions.ops_bp import bp as ops_bp
# Background timers/queues: threshold alerts + daily summary, per-user metric
# availability refresh, and system-initiated STEPS alerts. Required for the
# scheduled daily summary and threshold alerts to actually FIRE.
from functions.threshold_monitor_bp import bp as threshold_monitor_bp
from functions.chatbot_context_refresh_bp import bp as chatbot_context_refresh_bp
from functions.system_initiated_steps_bp import bp as system_initiated_steps_bp
from functions.sensei_api_bp import bp as sensei_api_bp

app = df.DFApp(http_auth_level=func.AuthLevel.ANONYMOUS)

app.register_functions(auth_bp)
app.register_functions(backfill_bp)
app.register_functions(ingest_bp)
app.register_functions(webhook_bp)
app.register_functions(telegram_bp)
app.register_functions(telegram_conversation_log_bp)
app.register_functions(ops_bp)
app.register_functions(threshold_monitor_bp)
app.register_functions(chatbot_context_refresh_bp)
app.register_functions(system_initiated_steps_bp)
app.register_functions(sensei_api_bp)
