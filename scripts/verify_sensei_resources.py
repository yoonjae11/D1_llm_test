"""End-to-end deployment verification for the Sensei main Function App.

This is a superset of ``verify_chatbot_operational_storage.py``. It reuses that
script's Table/Queue/ADX-mapping checks and adds the extra wiring checks required
by the v7 work spec:

- Phone curated tables / materialized views exist in ADX
- ``TelegramConversationLog`` exists in ADX
- Telegram bot token is configured
- STEPS proactive producer env hints (informational)
- the queue Storage Account that the main app reads is reported so the operator
  can confirm the webhook app and the phone-ingest producer enqueue to the SAME
  account
- (optional) an Azure OpenAI Responses API smoke test, only when explicitly
  requested with ``--openai-smoke``

It performs READ-ONLY checks. It never creates tables, queues, or ADX objects.
Run it from a machine that has Azure credentials (``az login`` or a managed
identity) and the app settings available via ``local.settings.json`` or the
process environment.

Usage::

    python scripts/verify_sensei_resources.py
    python scripts/verify_sensei_resources.py --skip-adx
    python scripts/verify_sensei_resources.py --openai-smoke
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import List

# Reuse the existing operational-storage verifier instead of duplicating logic.
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import verify_chatbot_operational_storage as base
except Exception as exc:  # pragma: no cover - import guard for partial checkouts.
    print(f"Could not import verify_chatbot_operational_storage: {exc}")
    sys.exit(2)


PHONE_ADX_TABLES = [
    "HealthDataFlat",
    "PhoneCumulativeIntervalDedupMV",
    "PhoneCumulativeHourlyMV",
    "PhoneCumulativeDailyMV",
    "TelegramConversationLog",
]


def _storage_account_name(conn_str: str) -> str:
    for part in str(conn_str or "").split(";"):
        if part.strip().lower().startswith("accountname="):
            return part.split("=", 1)[1].strip()
    return "(unknown)"


def _check_phone_adx_tables() -> List[str]:
    errors: List[str] = []
    try:
        client, database = base._kusto_client()
    except Exception as exc:
        return [f"ADX phone-table verification failed: {exc}"]
    for table in PHONE_ADX_TABLES:
        try:
            client.execute_mgmt(database, f".show table {table} schema as json")
        except Exception as exc:
            errors.append(f"missing or inaccessible ADX table/MV: {table}: {exc}")
    return errors


def _telegram_token_warnings() -> List[str]:
    warnings: List[str] = []
    if not os.environ.get("TELEGRAM_BOT_TOKEN"):
        warnings.append("TELEGRAM_BOT_TOKEN is not set (proactive alerts cannot be delivered).")
    return warnings


def _informational() -> List[str]:
    notes: List[str] = []
    main_storage = os.environ.get("AzureWebJobsStorage", "")
    notes.append(f"Main app queue Storage Account = {_storage_account_name(main_storage)}")
    notes.append(
        "Confirm the webhook app (WITHINGS_QUEUE_CONN_STR) and the phone-ingest "
        "producer (SYSTEM_PHONE_INGEST_EVENT_CONN_STR) point to this SAME account."
    )
    notes.append(
        "STEPS proactive producer lives in sensory-health-sync-functions and must set "
        "SYSTEM_PHONE_INGEST_EVENT_ENABLED=true to emit events."
    )
    return notes


def _openai_smoke() -> List[str]:
    """Minimal Azure OpenAI Responses API reachability check (opt-in)."""
    provider = os.environ.get("AI_PROVIDER", "")
    base_url = os.environ.get("AZURE_OPENAI_BASE_URL", "")
    deployment = (
        os.environ.get("AZURE_OPENAI_DEPLOYMENT_NAME")
        or os.environ.get("AZURE_OPENAI_DEFAULT_DEPLOYMENT")
        or os.environ.get("AZURE_OPENAI_CHAT_DEPLOYMENT")
        or ""
    )
    api_key = os.environ.get("AZURE_OPENAI_API_KEY", "")
    if not base_url or not deployment:
        return ["OpenAI smoke skipped: AZURE_OPENAI_BASE_URL or deployment name is missing."]
    try:
        from openai import OpenAI
    except Exception as exc:
        return [f"OpenAI smoke failed: openai SDK unavailable: {exc}"]
    try:
        normalized = base_url.rstrip("/") + "/"
        client = OpenAI(base_url=normalized, api_key=api_key or "missing", timeout=20)
        # Prefer the Responses API surface; fall back to chat.completions.
        try:
            resp = client.responses.create(model=deployment, input="ping")
            _ = getattr(resp, "id", None)
        except Exception:
            client.chat.completions.create(
                model=deployment,
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=1,
            )
        return []
    except Exception as exc:
        return [f"OpenAI smoke failed (provider={provider}): {exc}"]


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify Sensei main Function App resources end-to-end.")
    parser.add_argument("--app-user-id", default="kist_8401", help="Optional ChatbotUserProfile seed row to verify.")
    parser.add_argument("--skip-adx", action="store_true", help="Skip ADX table/mapping checks.")
    parser.add_argument("--openai-smoke", action="store_true", help="Run an Azure OpenAI Responses API smoke test.")
    args = parser.parse_args()

    base._load_local_settings()
    connection_string = os.environ.get("AzureWebJobsStorage", "")

    errors: List[str] = []
    warnings: List[str] = []

    if not connection_string:
        errors.append("AzureWebJobsStorage is missing")
    else:
        for table_name in base.TABLES:
            error = base._check_table(connection_string, table_name)
            if error:
                errors.append(error)
        for queue_name in base.QUEUES:
            error = base._check_queue(connection_string, queue_name)
            if error:
                errors.append(error)
        error = base._check_user_profile(connection_string, "ChatbotUserProfile", args.app_user_id)
        if error:
            errors.append(error)

    if not args.skip_adx:
        try:
            errors.extend(base._check_adx())
            errors.extend(_check_phone_adx_tables())
        except Exception as exc:
            errors.append(f"ADX verification failed: {exc}")

    warnings.extend(_telegram_token_warnings())

    if args.openai_smoke:
        smoke = _openai_smoke()
        # Treat smoke skips as warnings and real failures as errors.
        for item in smoke:
            (warnings if "skipped" in item.lower() else errors).append(item)

    errors = list(dict.fromkeys(errors))
    warnings = list(dict.fromkeys(warnings))

    print("Informational:")
    for note in _informational():
        print(f"- {note}")
    if warnings:
        print("\nWarnings:")
        for warning in warnings:
            print(f"- {warning}")

    if errors:
        print("\nResource verification FAILED:")
        for error in errors:
            print(f"- {error}")
        return 1

    print("\nAll required Sensei main app resources are present.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
