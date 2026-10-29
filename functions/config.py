# -*- coding: utf-8 -*-
"""
Environment variables and constants for the Withings data pipeline.
"""

import os
import logging
from runtime_environment import env_bool, env_int, load_local_environment


load_local_environment()


def _env_bool(name: str, default: bool) -> bool:
    return env_bool(name, default, blank_uses_default=False)


_env_int = env_int

# --------------------------------------------------------------------------
# Withings OAuth
# --------------------------------------------------------------------------
CLIENT_ID = os.environ.get("WITHINGS_CLIENT_ID")
CLIENT_SECRET = os.environ.get("WITHINGS_CLIENT_SECRET")

# --------------------------------------------------------------------------
# Azure Storage
# --------------------------------------------------------------------------
TABLE_CONN_STR = os.environ.get("AzureWebJobsStorage")
APP_TABLE_CONN_STR = os.environ.get("AzureWebJobsAppStorage")
APP_USERS_TABLE_NAME = os.environ.get("APP_USERS_TABLE_NAME", "Users")
TELEGRAM_REGISTER_STATE_TABLE = os.environ.get("TELEGRAM_REGISTER_STATE_TABLE", "TelegramRegisterState")

# Temporary chat-account pairing.  The Smartphone Function App issues the
# short-lived code into this table; this app consumes it from either supported
# chat transport.  Falling back to host storage keeps deployments that already
# colocate application tables backward compatible.
PAIRING_TABLE_CONN_STR = APP_TABLE_CONN_STR or TABLE_CONN_STR
PAIRING_REQUESTS_TABLE_NAME = os.environ.get(
    "PAIRING_REQUESTS_TABLE_NAME", "PairingRequests"
)
PAIRING_HMAC_SECRET = os.environ.get("PAIRING_HMAC_SECRET", "")
PAIRING_CODE_TTL_SECONDS = min(
    900, max(60, _env_int("PAIRING_CODE_TTL_SECONDS", 300))
)
PAIRING_MAX_ATTEMPTS = max(1, _env_int("PAIRING_MAX_ATTEMPTS", 5))
PAIRING_ATTEMPT_WINDOW_SECONDS = max(
    60, _env_int("PAIRING_ATTEMPT_WINDOW_SECONDS", 300)
)
PAIRING_LOCKOUT_SECONDS = max(60, _env_int("PAIRING_LOCKOUT_SECONDS", 900))

# --------------------------------------------------------------------------
# URLs
# --------------------------------------------------------------------------
BASE_URL = os.environ.get("BASE_URL")
WITHINGS_WEBHOOK_URL = os.environ.get("WITHINGS_WEBHOOK_URL")
# A dedicated state key is preferred. Falling back to the already-required
# Withings client secret keeps existing deployments compatible while still
# replacing the formerly unsigned state value.
WITHINGS_OAUTH_STATE_SECRET = (
    os.environ.get("WITHINGS_OAUTH_STATE_SECRET") or CLIENT_SECRET or ""
)
WITHINGS_OAUTH_STATE_TTL_SECONDS = _env_int("WITHINGS_OAUTH_STATE_TTL_SECONDS", 600)
WITHINGS_OAUTH_ALLOW_LEGACY_LOCAL_STATE = _env_bool(
    "WITHINGS_OAUTH_ALLOW_LEGACY_LOCAL_STATE", False
)

# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}" if TELEGRAM_BOT_TOKEN else None
TELEGRAM_WEBHOOK_SECRET_TOKEN = os.environ.get("TELEGRAM_WEBHOOK_SECRET_TOKEN", "")
TELEGRAM_WEBHOOK_REQUIRE_SECRET = _env_bool(
    "TELEGRAM_WEBHOOK_REQUIRE_SECRET", False
)
if TELEGRAM_BOT_TOKEN and not TELEGRAM_WEBHOOK_SECRET_TOKEN:
    if TELEGRAM_WEBHOOK_REQUIRE_SECRET:
        logging.error(
            "[BOOT] Telegram webhook secret enforcement is enabled, but "
            "TELEGRAM_WEBHOOK_SECRET_TOKEN is missing."
        )
    else:
        logging.warning(
            "[BOOT] Telegram webhook secret validation is disabled for backward "
            "compatibility. Configure TELEGRAM_WEBHOOK_SECRET_TOKEN and set "
            "TELEGRAM_WEBHOOK_REQUIRE_SECRET=true for production enforcement."
        )
TELEGRAM_PASSCODE_MAX_ATTEMPTS = max(1, _env_int("TELEGRAM_PASSCODE_MAX_ATTEMPTS", 5))
TELEGRAM_PASSCODE_LOCKOUT_SECONDS = max(
    60, _env_int("TELEGRAM_PASSCODE_LOCKOUT_SECONDS", 900)
)

# --------------------------------------------------------------------------
# ADX (Azure Data Explorer)
# --------------------------------------------------------------------------
ADX_INGEST_URI = os.environ.get("ADX_INGEST_URI")
ADX_DATABASE = os.environ.get("ADX_DATABASE")
ADX_TABLE = "WithingsRawData"

# --------------------------------------------------------------------------
# Telegram conversation log -> ADX
# --------------------------------------------------------------------------
TELEGRAM_CONVERSATION_LOG_ENABLE = _env_bool("TELEGRAM_CONVERSATION_LOG_ENABLE", False)
TELEGRAM_CONVERSATION_BUFFER_TABLE = os.environ.get(
    "TELEGRAM_CONVERSATION_BUFFER_TABLE", "TelegramConversationBuffer"
)
TELEGRAM_CONVERSATION_CHECKPOINT_TABLE = os.environ.get(
    "TELEGRAM_CONVERSATION_CHECKPOINT_TABLE", "TelegramConversationCheckpoint"
)

TELEGRAM_CONVERSATION_BATCH_SIZE = _env_int("TELEGRAM_CONVERSATION_BATCH_SIZE", 500)
TELEGRAM_CONVERSATION_FLUSH_LOOKBACK_DAYS = _env_int("TELEGRAM_CONVERSATION_FLUSH_LOOKBACK_DAYS", 2)
TELEGRAM_CONVERSATION_MAX_ATTEMPTS = _env_int("TELEGRAM_CONVERSATION_MAX_ATTEMPTS", 5)
TELEGRAM_CONVERSATION_EXPORT_LEASE_SECONDS = _env_int(
    "TELEGRAM_CONVERSATION_EXPORT_LEASE_SECONDS", 900
)

TELEGRAM_CONVERSATION_STORE_TEXT = _env_bool("TELEGRAM_CONVERSATION_STORE_TEXT", False)

TELEGRAM_CONVERSATION_TEXT_MAX_CHARS = _env_int("TELEGRAM_CONVERSATION_TEXT_MAX_CHARS", 4096)
TELEGRAM_CONVERSATION_PAYLOAD_MAX_CHARS = _env_int("TELEGRAM_CONVERSATION_PAYLOAD_MAX_CHARS", 12000)
TELEGRAM_CONVERSATION_HASH_SALT = os.environ.get("TELEGRAM_CONVERSATION_HASH_SALT", "")
TELEGRAM_CONVERSATION_ADMIN_KEY = os.environ.get("TELEGRAM_CONVERSATION_ADMIN_KEY", "")

ADX_TELEGRAM_CONVERSATION_TABLE = os.environ.get("ADX_TELEGRAM_CONVERSATION_TABLE", "TelegramConversationLog")
ADX_TELEGRAM_CONVERSATION_MAPPING = os.environ.get(
    "ADX_TELEGRAM_CONVERSATION_MAPPING", "TelegramConversationLogJsonMapping"
)

# v2: lean ADX schema (study essentials + Plan); see telegram_query_kql.kql.
TELEGRAM_CONVERSATION_SCHEMA_VERSION = _env_int("TELEGRAM_CONVERSATION_SCHEMA_VERSION", 2)
ENV_NAME = os.environ.get("ENV_NAME", "local")

if TELEGRAM_CONVERSATION_LOG_ENABLE and ENV_NAME.lower() not in {"local", "dev", "development"}:
    if not TELEGRAM_CONVERSATION_HASH_SALT:
        logging.warning("[BOOT] TELEGRAM_CONVERSATION_HASH_SALT is empty; chat/message hashes are unsalted.")

# --------------------------------------------------------------------------
# Webhook fetch options
# --------------------------------------------------------------------------
FETCH_INTRADAY_ON_WEBHOOK = os.environ.get("FETCH_INTRADAY_ON_WEBHOOK", "true").lower() == "true"
FETCH_WORKOUTS_ON_WEBHOOK = os.environ.get("FETCH_WORKOUTS_ON_WEBHOOK", "false").lower() == "true"
INTRADAY_DATA_FIELDS = os.environ.get(
    "WITHINGS_INTRADAY_DATA_FIELDS",
    "steps,elevation,calories,distance,heart_rate,rmssd,sdnn1,hrv_quality",
)

# --------------------------------------------------------------------------
# Dedup settings
# --------------------------------------------------------------------------
DEDUP_TABLE_NAME = os.environ.get("WITHINGS_DEDUP_TABLE_NAME", "WithingsIngestDedup")
DEDUP_DATA_TYPES = set(
    [x.strip() for x in os.environ.get("WITHINGS_DEDUP_DATA_TYPES", "Activity,Sleep,SleepDetail").split(",") if x.strip()]
)

# --------------------------------------------------------------------------
# Webhook per-user lease
# --------------------------------------------------------------------------
WEBHOOK_LEASE_TABLE = os.environ.get("WITHINGS_WEBHOOK_LEASE_TABLE", "WithingsWebhookLease")
WEBHOOK_LEASE_SECONDS = int(os.environ.get("WITHINGS_WEBHOOK_LEASE_SECONDS", "60"))
WEBHOOK_LEASE_ENABLE = os.environ.get("WITHINGS_WEBHOOK_LEASE_ENABLE", "true").lower() == "true"

# --------------------------------------------------------------------------
# Backfill controls
# --------------------------------------------------------------------------
BACKFILL_DAYS = int(os.environ.get("WITHINGS_BACKFILL_DAYS", "100"))
BACKFILL_PARALLEL = int(os.environ.get("WITHINGS_BACKFILL_PARALLEL", "2"))

# --------------------------------------------------------------------------
# Withings HTTP retry policy
# --------------------------------------------------------------------------
WITHINGS_HTTP_MAX_ATTEMPTS = int(os.environ.get("WITHINGS_HTTP_MAX_ATTEMPTS", "5"))
WITHINGS_HTTP_BASE_BACKOFF_MS = int(os.environ.get("WITHINGS_HTTP_BASE_BACKOFF_MS", "400"))
WITHINGS_HTTP_TIMEOUT_SEC = int(os.environ.get("WITHINGS_HTTP_TIMEOUT_SEC", "15"))

# --------------------------------------------------------------------------
# Webhook sync cursor (per-user incremental windows)
# --------------------------------------------------------------------------
SYNC_STATE_TABLE_NAME = os.environ.get("WITHINGS_SYNC_STATE_TABLE_NAME", "WithingsSyncState")

SYNC_ACTIVITY_LOOKBACK_DAYS = int(os.environ.get("WITHINGS_SYNC_ACTIVITY_LOOKBACK_DAYS", "2"))
SYNC_ACTIVITY_MAX_LOOKBACK_DAYS = int(os.environ.get("WITHINGS_SYNC_ACTIVITY_MAX_LOOKBACK_DAYS", "7"))
SYNC_ACTIVITY_OVERLAP_DAYS = int(os.environ.get("WITHINGS_SYNC_ACTIVITY_OVERLAP_DAYS", "1"))

SYNC_SLEEP_LOOKBACK_DAYS = int(os.environ.get("WITHINGS_SYNC_SLEEP_LOOKBACK_DAYS", "3"))
SYNC_SLEEP_MAX_LOOKBACK_DAYS = int(os.environ.get("WITHINGS_SYNC_SLEEP_MAX_LOOKBACK_DAYS", "14"))
SYNC_SLEEP_OVERLAP_DAYS = int(os.environ.get("WITHINGS_SYNC_SLEEP_OVERLAP_DAYS", "1"))

SYNC_INTRADAY_LOOKBACK_HOURS = int(os.environ.get("WITHINGS_SYNC_INTRADAY_LOOKBACK_HOURS", "6"))
SYNC_INTRADAY_MAX_LOOKBACK_HOURS = int(os.environ.get("WITHINGS_SYNC_INTRADAY_MAX_LOOKBACK_HOURS", "24"))
SYNC_INTRADAY_OVERLAP_MINUTES = int(os.environ.get("WITHINGS_SYNC_INTRADAY_OVERLAP_MINUTES", "30"))

# --------------------------------------------------------------------------
# Webhook retry on empty data
# --------------------------------------------------------------------------
WEBHOOK_RETRY_ON_EMPTY_ENABLE = os.environ.get("WITHINGS_WEBHOOK_RETRY_ON_EMPTY_ENABLE", "true").lower() == "true"
WEBHOOK_RETRY_ON_EMPTY_MAX = int(os.environ.get("WITHINGS_WEBHOOK_RETRY_ON_EMPTY_MAX", "5"))
WEBHOOK_RETRY_ON_EMPTY_BASE_DELAY_SECONDS = int(os.environ.get("WITHINGS_WEBHOOK_RETRY_ON_EMPTY_BASE_DELAY_SECONDS", "120"))
RETRY_ON_PARTIAL_EMPTY_ENABLE = os.environ.get("WITHINGS_WEBHOOK_RETRY_ON_PARTIAL_EMPTY_ENABLE", "true").lower() == "true"

# Sleep-specific retry delays (seconds): 2m -> 10m -> 30m -> 1h -> 3h -> 6h -> 12h
WEBHOOK_RETRY_SLEEP_DELAYS = [
    int(x.strip())
    for x in os.environ.get(
        "WITHINGS_WEBHOOK_RETRY_SLEEP_DELAYS", "120,600,1800,3600,10800,21600,43200"
    ).split(",")
    if x.strip()
]
