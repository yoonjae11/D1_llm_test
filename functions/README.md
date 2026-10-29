# Azure Function blueprints

> Status: **Active and deployed runtime package**

## Purpose

This package contains the Azure Functions transport and background-processing
layer for the Main Function App. [`../function_app.py`](../function_app.py)
creates one `azure.durable_functions.DFApp` and registers the eleven blueprints
defined here. Business rules for health queries and chatbot responses belong in
[`../chatbot/`](../chatbot/), including the active conversation and episode
state. [`../memory/`](../memory/) is retained only for research and local-test
compatibility and is not deployed.

## At a glance

`function_app.py` imports and registers every blueprint in this table. Add a
new trigger here only when the root registration file is updated in the same
change.

### Registered blueprint files

| Module | Azure functions | Responsibility |
|---|---|---|
| `telegram_bp.py` | `telegram_webhook` HTTP trigger | Verifies Telegram requests, handles PIN pairing, registration/login and commands, invokes the chatbot graph, sends replies, and stages conversation events. |
| `sensei_api_bp.py` | `send_message` HTTP trigger | Exposes the Function-key-authenticated, channel-neutral Sensei boundary used by Conversational Care; validates opaque identities, consumes bare six-digit pairing messages for unmapped users, and handles structured login/register/message operations before invoking the chatbot core. |
| `auth_bp.py` | `withings_auth` HTTP trigger | Verifies the signed OAuth state, exchanges the Withings authorization code, stores tokens, starts a backfill, and subscribes the user to the configured webhook. |
| `backfill_bp.py` | `backfill_orchestrator` orchestration trigger | Fans historical Withings date windows into `fetch_and_ingest` activities. |
| `ingest_bp.py` | `fetch_and_ingest` activity trigger | Fetches Withings activity, sleep, intraday, and workout data, applies deduplication/cursors, and ingests records into ADX. |
| `webhook_bp.py` | `WithingsWebhookQueueWorker` queue trigger | Consumes `withings-webhook-events`, fetches changed Withings data, and applies bounded retry behavior for empty or partial responses. |
| `ops_bp.py` | `sync_now`, `diag_queue`, `enqueue_ping`, `diag_telegram_conversation_log`, `flush_telegram_conversation_log_now` HTTP triggers | Provides Function-key-protected synchronization and operational diagnostics. |
| `threshold_monitor_bp.py` | `ThresholdAlertTimer`, `DailyReportTimer`; three HTTP operations | Schedules threshold evaluation and daily summaries and exposes controlled manual/diagnostic runs. |
| `chatbot_context_refresh_bp.py` | availability timer, queue worker, and HTTP operation | Enqueues and performs per-user Phone/Withings metric-availability refreshes. |
| `system_initiated_steps_bp.py` | two queue triggers, two timers, and two HTTP operations | Processes Smartphone App STEPS events, retries, fallback/reconciliation scans, and diagnostics. |
| `telegram_conversation_log_bp.py` | `TelegramConversationFlushTimer` timer trigger | Flushes staged, privacy-controlled Telegram conversation records to ADX. |

The root [README](../README.md) is the authoritative route, trigger, schedule,
and queue manifest. Function names and queue names are deployment contracts;
coordinate changes with producers, consumers, application settings, and tests.

### Supporting files

| Module | Responsibility |
|---|---|
| `config.py` | Reads Function-layer Withings, Telegram, ADX, conversation-log, backfill, retry, and storage settings from the process environment. |
| `pairing.py` | Validates HMAC-addressed, expiring pairing requests; enforces guess throttling; and atomically claims/finalizes one-time codes shared with the Smartphone Function App. |
| `shared.py` | Established integration seam for Telegram delivery, user/mapping/state tables, passcode throttling, Withings tokens/cursors/dedup/leases, and ADX ingestion. |
| `security.py` | Constant-time secret matching, passcode verification, redirect construction, and signed/expiring Withings OAuth state. |
| `interview.py` | Registration follow-up flow for profile, timezone, alert thresholds, and scheduled summaries. |
| `fetch_helpers.py` | Withings API request helpers for activity, sleep summary/detail, intraday data, and workouts. |
| `telegram_conversation_log.py` | Redaction, hashing, staging, lease/ETag-safe export, ADX ingestion, retry, checkpoint, and diagnostics logic. |
| `__init__.py` | Marks the blueprint package; blueprints are registered explicitly by `function_app.py`. |

## Principal request flows

### Telegram

```text
Telegram POST /api/telegram_webhook
  -> validate X-Telegram-Bot-Api-Secret-Token when configured
  -> deduplicate update
  -> registration/login/interview/command handling when applicable
  -> resolve episode and chatbot context
  -> invoke guarded chatbot graph
  -> send Telegram response
  -> stage privacy-controlled inbound/outbound events
```

For an unmapped Telegram chat, an exact six-digit message is handled before the
registration gate. The same code path is used by Conversational Care through
its existing `operation=message` contract:

```text
Custom Logger App -> Smartphone Function -> PairingRequests (5-minute PIN)
unmapped CC or Telegram identity -> bare six-digit message -> Main Function
  -> atomically claim PIN + active pointer
  -> save channel-specific UserMapping -> finalize PIN as consumed
  -> ordinary chatbot use on subsequent messages
```

The temporary table never contains the raw PIN. A mapping-save failure leaves a
single-identity-bound claim that the same sender can retry until expiry; another
identity or channel cannot consume it.

### Withings

```text
GET /api/withings_auth
  -> verify signed OAuth state and exchange code
  -> persist token
  -> start Durable historical backfill

separate Webhook Function App
  -> withings-webhook-events queue
  -> WithingsWebhookQueueWorker
  -> fetch changed records, deduplicate, and ingest to ADX
```

### Proactive and scheduled work

```text
Phone ingestion event or fallback/reconciliation timer
  -> system_initiated_steps_bp
  -> chatbot.proactive.system_steps_alert
  -> ADX evidence/audit + optional Telegram alert

threshold/daily timers
  -> threshold_monitor_bp
  -> chatbot.proactive.threshold_monitor
  -> Telegram alert or daily report
```

## Authorization and secrets

`function_app.py` uses anonymous HTTP authorization as the application default
because Telegram and Withings cannot supply an Azure Function key for their
callbacks. Those public protocol routes apply their own verification:

- Telegram validates `TELEGRAM_WEBHOOK_SECRET_TOKEN` whenever it is configured.
  Production should also set `TELEGRAM_WEBHOOK_REQUIRE_SECRET=true`; the
  documented default permits an existing secretless webhook for deployment
  compatibility.
- Withings OAuth validates a signed, expiring state value.
- Operational HTTP routes explicitly require Function authorization; selected
  conversation-log operations can also require an admin key.

Never log passcodes, bot tokens, OAuth codes/tokens, connection strings, raw
Telegram updates, or unredacted health payloads. Keep secrets in Azure Function
App settings or an approved secret store, never in source or README examples.

## Storage and queue contracts

- Queue names come from [`../queue_contracts.py`](../queue_contracts.py); do not
  copy string literals into new producers or consumers.
- Shared Table/Queue client construction comes from
  [`../azure_storage_resources.py`](../azure_storage_resources.py).
- `AzureWebJobsStorage` must refer to the same Storage account used by the
  corresponding Smartphone and Withings queue producers.
- `AzureWebJobsAppStorage` may separately host the established `Users` table and
  the shared `PairingRequests` table. Both Function Apps must use the same
  `PAIRING_HMAC_SECRET` and pairing table name.
- ADX query and ingestion endpoints have different purposes; preserve the
  configured query/ingest URI distinction.

## Contributor guidance

1. Keep a blueprint focused on trigger decoding, authorization, orchestration,
   response construction, and provider handoff.
2. Put reusable chatbot policy in `chatbot/`, not in HTTP/queue/timer handlers.
3. Reuse `functions.shared` only for established integration contracts; put new
   provider-independent seams behind `chatbot/services/` where appropriate.
4. Keep Azure function names, route names, queue bindings, and schedules stable
   unless deployment configuration and all integrations migrate together.
5. Use explicit Function authorization for new operator endpoints. A new public
   callback requires a documented protocol-specific verification mechanism.
6. Avoid network or storage work at module import time so the host can index all
   functions and CI package validation can import the application without live
   credentials.

## Validation

From the Function App directory, run:

```bash
python local_test.py smoke
python local_test.py system-preview
```

The smoke command imports `function_app`, verifies Azure Function registration,
compiles the canonical graph, loads packaged prompts, and checks the KQL guard
without network calls. For a protected non-production end-to-end chatbot check,
run:

```bash
python local_test.py ask --user-id "<test-app-user-id>" --message "Show my steps for today"
```

The live command may call Azure OpenAI and ADX and may update operational
conversation state. Require the CI package-validation gate before deployment,
and manually verify any changed HTTP authorization, queue binding, timer
schedule, or Telegram callback contract in a non-production Function App.
