# Azure Application Settings and Runtime Resources

This document describes the application settings and Azure resources used by the Sensei Main Azure Function App. It follows the current implementation in `functions/config.py`, `chatbot/config.py`, `queue_contracts.py`, and the active Storage repositories.

Azure Functions Core Tools publishes code and dependencies; it does not copy `local.settings.json` into Azure and does not create application settings. Configure production values in **Function App > Configuration > Application settings**, with Azure CLI, or through the project's approved secret-management workflow.

Never commit `local.settings.json`, `.env`, connection strings, bot tokens, OAuth credentials, or API keys. The local loader uses this precedence:

1. Existing process environment values
2. `local.settings.json`
3. `.env.local`
4. `.env`

Azure Application Settings are process environment values and therefore take precedence in the deployed app.

## Where configuration and secrets live

The runtime does not search an Azure Storage account for arbitrary secrets.
Each configuration category has a distinct source of truth:

| Configuration or resource | Authoritative location | How this app receives or uses it |
|---|---|---|
| Function runtime values, endpoints, feature flags, bot token, webhook secret, OAuth credentials, and API keys | **Azure Portal > Function App > sensoryllm-azure-func > Settings > Environment variables**; older portal: **Configuration > Application settings** | Azure injects each Application Setting into the Python process environment. The code reads it with `os.environ`. |
| Sensitive Function App values managed centrally | Azure Key Vault | Put an `@Microsoft.KeyVault(SecretUri=https://<vault>.vault.azure.net/secrets/<name>/)` reference in the corresponding Function App Application Setting and grant the Function App managed identity access to the secret. |
| Function host state, application tables, and queues | Azure Storage account | Set `AzureWebJobsStorage` and, when used, `AzureWebJobsAppStorage` in the Function App. Merely creating the Storage account or placing data in a table does not create an environment variable. |
| ADX tables, materialized views, mappings, and database roles | Azure Data Explorer | Provision them with the repository's `azure_adx_kql` assets. Put the ADX endpoint and database name in Function App Application Settings. |
| Telegram webhook URL and its copy of the webhook secret | Telegram Bot API | Call `setWebhook` after configuring strict mode. Telegram must receive the exact same secret stored in `TELEGRAM_WEBHOOK_SECRET_TOKEN`. |
| Local developer values | Untracked `local.settings.json`, `.env.local`, or `.env` | Used only for local execution. They are excluded from Git and the Azure deployment package. |

An Azure Storage access key is not a Telegram token, and
`TELEGRAM_BOT_TOKEN` is not a webhook secret. Do not store an arbitrary secret
only in Table/Blob Storage and expect the Function host to discover it. The
current implementation supports direct Application Setting values and Azure
Functions-resolved Key Vault references.

## 1. Required core settings

| Setting | Required | Value or default | Purpose |
|---|---|---|---|
| `AzureWebJobsStorage` | Yes | No application default | Azure Functions host storage, Durable Functions state, application tables, and all four runtime queues. |
| `AzureWebJobsAppStorage` | Yes for registration, login, and recommended for pairing | No application default | Connection string for the `Users` and shared `PairingRequests` tables. It may point to the same account as `AzureWebJobsStorage`; pairing falls back to the latter when this setting is absent. |
| `FUNCTIONS_WORKER_RUNTIME` | Yes | `python` | Selects the Python worker. |
| `ENV_NAME` | Recommended | `local` | Environment label used by safety checks and diagnostics. Use a production-specific value in Azure. |
| `BASE_URL` | Withings OAuth only | No application default | Public origin of this app, for example `https://<main-app-hostname>`. Do not append `/api`; a legacy trailing `/api` is normalized. |
| `TELEGRAM_BOT_TOKEN` | Telegram features | No application default | Telegram Bot API credential used for replies and proactive notifications. |
| `ADX_QUERY_URI` or `ADX_CLUSTER_URI` | Health-data questions | No application default | ADX query endpoint. `ADX_CLUSTER_URI` takes precedence when both are set. |
| `ADX_INGEST_URI` | Withings ingestion, alerts, or conversation export | No application default | ADX ingestion endpoint. |
| `ADX_DATABASE` | Any ADX feature | No application default | Target ADX database. |
| `KQL_GENERATE_METHOD` | Recommended; required explicitly in production | `llm` | Selects `llm` or `deterministic` KQL generation. Production should use `llm` after validating the deployed Sensor contract; invalid values fail configuration validation. |
| `PAIRING_HMAC_SECRET` | Yes when PIN pairing is enabled | No application default | Random secret of at least 32 UTF-8 bytes, identical in the Smartphone and Main Function Apps. Never expose it to the Flutter client. |

Missing Storage settings break registration, state, queue, and timer workflows. Missing ADX query settings allow the Function host to start, but health-data questions cannot execute successfully.

`KQL_GENERATE_METHOD` is the sole generation-strategy switch. In
`deterministic` mode, the sanitized plan goes directly to the deterministic
compiler; a compiler miss fails closed and does not invoke an LLM to create KQL.
In `llm` mode, the same sanitized plan is sent to the direct-KQL prompt and then
through the same guard, repair, executor, and response stages. The latter is an
production default; `deterministic` remains an evaluation/ablation option. The older
`CHATBOT_ENABLE_GENERIC_KQL_FALLBACK` setting is retained only as a deprecated
compatibility surface and must not be used to select the generation method.

## 2. Telegram webhook settings

| Setting | Runtime default | Purpose |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Empty | Bot API authentication. |
| `TELEGRAM_WEBHOOK_SECRET_TOKEN` | Empty | Expected value of `X-Telegram-Bot-Api-Secret-Token`. If a value is configured, every request must match it. |
| `TELEGRAM_WEBHOOK_REQUIRE_SECRET` | `false` | Fail-closed switch. If `true` while the secret is missing, the webhook returns HTTP 503. |
| `TELEGRAM_PASSCODE_MAX_ATTEMPTS` | `5` | Failed login/linking attempts before lockout. Minimum effective value is 1. |
| `TELEGRAM_PASSCODE_LOCKOUT_SECONDS` | `900` | Lockout duration. Minimum effective value is 60 seconds. |

Backward-compatible mode is an empty `TELEGRAM_WEBHOOK_SECRET_TOKEN` with `TELEGRAM_WEBHOOK_REQUIRE_SECRET=false`. It accepts the existing secretless Telegram webhook so a code-only deployment does not silently disable an older bot.

Production mode should set a random `TELEGRAM_WEBHOOK_SECRET_TOKEN`, set `TELEGRAM_WEBHOOK_REQUIRE_SECRET=true`, and call Telegram `setWebhook` with the exact same secret. Publishing the Function App does not register or update the Telegram webhook. Follow [README.md](README.md#configure-and-register-telegram-after-publishing) for commands that avoid placing secrets in source files or shell history.

These two Telegram credentials serve different purposes:

| Value | Stored in the Function App? | Also configured elsewhere? |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Yes | Created and managed through BotFather; used for outbound Bot API calls. |
| `TELEGRAM_WEBHOOK_SECRET_TOKEN` | Yes in strict mode | Yes. Pass the identical value as `secret_token` to Telegram `setWebhook`. |

The original bot did not require a webhook secret. Therefore, a correctly
configured older deployment can contain every previously required token and
still lack `TELEGRAM_WEBHOOK_SECRET_TOKEN`. A code revision that makes that new
setting unconditionally mandatory will return HTTP 503 even though the bot
token, Storage settings, ADX settings, and LLM credentials are all present.
Core Tools publishes code and preserves existing Application Settings, but it
does not invent a newly introduced setting or synchronize it with Telegram.

## 3. LLM provider settings

Choose exactly one provider.

### Azure OpenAI

| Setting | Runtime default | Purpose |
|---|---|---|
| `AI_PROVIDER` | `openai` | Set to `azure_openai_v1`. |
| `AZURE_OPENAI_BASE_URL` | Empty | OpenAI-compatible Azure endpoint, normally `https://<resource>.openai.azure.com/openai/v1/`. |
| `AZURE_OPENAI_DEFAULT_DEPLOYMENT` | Empty | Deployment used by routing, planning, query repair, and responses. |
| `AZURE_OPENAI_CHAT_DEPLOYMENT` | Empty | Backward-compatible fallback when `AZURE_OPENAI_DEFAULT_DEPLOYMENT` is absent. |
| `AZURE_OPENAI_AUTH_MODE` | `api_key` | `api_key` or `managed_identity`. |
| `AZURE_OPENAI_API_KEY` | Empty | Required only when authentication mode is `api_key`. |

When using `managed_identity`, grant the Function App identity the required Azure OpenAI role. The code requests the `https://ai.azure.com/.default` scope.

### OpenAI API

| Setting | Runtime default | Purpose |
|---|---|---|
| `AI_PROVIDER` | `openai` | Selects the OpenAI API client. |
| `OPENAI_API_KEY` | Empty | Required OpenAI API credential. |
| `OPENAI_CHAT_MODEL` | `gpt-5` | Model used when no call-specific model is supplied. |

### Shared LLM controls

| Setting | Runtime default | Purpose |
|---|---|---|
| `OPENAI_TIMEOUT_SECONDS` | `120` | Request timeout. |
| `OPENAI_MAX_RETRIES` | `4` | SDK retry count, clamped to the range 0-10. |
| `CHATBOT_INTERNAL_REASONING_EFFORT` | `low` | Reasoning effort for routing, planning, NLU, and KQL repair on supported reasoning models. |
| `CHATBOT_RESPONSE_REASONING_EFFORT` | `low` | Reasoning effort for user-facing Responses API calls. |
| `CHATBOT_INSECURE_SSL` | `false` | Disables TLS certificate verification when enabled. Keep `false` in production. |

## 4. ADX resources and settings

The Function App identity needs query access for chatbot reads. Features that ingest Withings data, alert candidates, baselines, or conversation logs also need ingestion access. Apply the schema, mappings, and permissions from [../azure_adx_kql/](../azure_adx_kql/) before enabling those features.

### Shared chatbot query contract

Conversational D1 queries use this database-local, read-only contract:

| Logical resource | Stored function |
|---|---|
| Daily activity | `SensorActivityDaily(TargetUserId, StartLocal, EndLocal)` |
| Hourly activity | `SensorActivityHourly(TargetUserId, StartLocal, EndLocal)` |
| Interval activity | `SensorActivityInterval(TargetUserId, StartLocal, EndLocal)` |
| Sampled health values | `SensorHealthSamples(TargetUserId, StartLocal, EndLocal)` |
| Nightly sleep | `SensorSleepNights(TargetUserId, StartLocal, EndLocal)` |
| Workout sessions | `SensorWorkoutSessions(TargetUserId, StartLocal, EndLocal)` |

The six names, signatures, output-column order, and ADX scalar types must be
identical in every supported database. Python policies, planners, compilers, and
prompts know only the logical resources and these `Sensor*` functions. They must
not contain production materialized-view/table names or evaluation dataset table
names. Only the stored-function definitions in the ADX deployment layer may know
their backing objects.

| Runtime | `ADX_DATABASE` | Contract installation | Time behavior |
|---|---|---|---|
| Production | `healthdb` (or the environment's production database name) | Apply [`11_sensor_query_contract_production.kql`](../azure_adx_kql/11_sensor_query_contract_production.kql) after its raw/normalized/activity/sleep dependencies. | Uses the real timezone-aware current time. Normal questions resolve to explicit finite windows. The current production path does not resolve `all_available`; it rejects that mode instead of issuing an unbounded scan. |
| PHIA evaluation | `healthdb_eval_phia` | In a separate database, apply [`evaluation/0_phia_raw_tables.kql`](../azure_adx_kql/evaluation/0_phia_raw_tables.kql), ingest the evaluation fixtures, then apply [`evaluation/1_phia_sensor_query_contract.kql`](../azure_adx_kql/evaluation/1_phia_sensor_query_contract.kql). | The evaluation runner injects per-persona summary and workout minimum/reference dates. Relative windows and `all_available` are resolved to finite absolute bounds before KQL generation. |

Changing from production to PHIA evaluation must not require Python, prompt, or
metric-catalog edits. The controlled differences are `ADX_DATABASE` and the
request-local evaluation time context. `KQL_GENERATE_METHOD` can be varied
independently only when performing the deterministic-versus-LLM ablation. Do not
point the deployed production Function App at the evaluation database; run PHIA
through the evaluation runner. PHIA has no hourly or interval activity evidence,
so its corresponding contract functions intentionally return schema-correct
empty results rather than invented values.

### Contract-first deployment order

For production:

1. Confirm the intended cluster/database and managed identities, then apply
   `1_database_permissions.kql` with reviewed environment-specific IDs.
2. Apply `2_phone_raw_to_flat.kql`, `3_phone_activity_mv.kql`, and
   `4_phone_sleep_mv.kql` in their documented step order.
3. Apply `11_sensor_query_contract_production.kql`, one complete stored-function
   definition at a time.
4. Verify that all six functions exist and run bounded calls against a dedicated
   test user. Empty data is acceptable; resolution, schema, or permission errors
   are not.
5. Set `ADX_DATABASE` and `KQL_GENERATE_METHOD=llm` in the Function App,
   then publish the Main App and perform the end-to-end regression checks.

For PHIA evaluation, use an isolated evaluation database: apply the P0 raw-table
file, ingest the fixtures, apply the P1 adapter file, verify all six contract
functions, and only then run the evaluator with its explicit time context. These
evaluation files must never be applied to the production database.

Additional ADX resources used by optional workflows are:

| Setting | Runtime default | Resource |
|---|---|---|
| `ADX_ALERT_BASELINE_TABLE` | `UserMetricMadBaselineSnapshot` | STEPS baseline snapshots. |
| `ADX_ALERT_BASELINE_MAPPING` | `UserMetricMadBaselineSnapshotJsonMapping` | JSON ingestion mapping for baselines. |
| `ADX_ALERT_CANDIDATE_TABLE` | `AlertCandidate` | Proactive alert candidates. |
| `ADX_ALERT_CANDIDATE_MAPPING` | `AlertCandidateJsonMapping` | JSON ingestion mapping for candidates. |
| `ADX_TELEGRAM_CONVERSATION_TABLE` | `TelegramConversationLog` | Optional lean conversation-event export. |
| `ADX_TELEGRAM_CONVERSATION_MAPPING` | `TelegramConversationLogJsonMapping` | JSON ingestion mapping for conversation events. |

## 5. Azure Table Storage resources

Several existing repositories idempotently ensure their own tables when first used. The PIN pairing handlers deliberately do not create infrastructure at request time, so `PairingRequests` must be provisioned and validated before deployment. Production infrastructure should provision every required table explicitly, especially when the runtime identity or connection string has restricted create permissions.

### Registration and Telegram state

| Setting | Runtime default |
|---|---|
| `APP_USERS_TABLE_NAME` | `Users` |
| `TELEGRAM_REGISTER_STATE_TABLE` | `TelegramRegisterState` |

### Temporary chat-account pairing

| Setting | Runtime default | Purpose |
|---|---|---|
| `PAIRING_REQUESTS_TABLE_NAME` | `PairingRequests` | Shared table used by the Smartphone issuer and Main App consumer. |
| `PAIRING_CODE_TTL_SECONDS` | `300` | Maximum accepted PIN lifetime. The Smartphone Function App clamps it to 60–900 seconds; use the same value in both apps. |
| `PAIRING_MAX_ATTEMPTS` | `5` | Invalid guesses allowed per hashed channel identity before lockout. |
| `PAIRING_ATTEMPT_WINDOW_SECONDS` | `300` | Rolling failure-counter window. Minimum effective value is 60 seconds. |
| `PAIRING_LOCKOUT_SECONDS` | `900` | Guess lockout duration. Minimum effective value is 60 seconds. |

The Smartphone Function App creates a `PairingCode` row and a per-user
`ActivePointer` row in `PartitionKey=PairingRequest`. Their RowKeys are derived
with HMAC-SHA256 using `PAIRING_HMAC_SECRET`; no raw PIN is stored. The Main App
claims both rows with an ETag-conditional same-partition transaction, writes the
channel-specific `UserMapping`, and then finalizes the code. The Main and
Smartphone Function Apps must resolve `PAIRING_REQUESTS_TABLE_NAME` to the same
physical Storage table and use the exact same secret.

Conversational Care requires no pairing-specific API operation: for an unmapped
approved user it continues forwarding the bare six-digit text through the
existing `operation=message`, `external_user_id`, `conversation_id`, and
`message` contract. Telegram uses the same code but creates its own mapping.

`UserMapping` is also required and currently has a fixed name. Existing rows use
`PartitionKey=Telegram`; the external Sensei API adds
`PartitionKey=ConversationalCare` rows with opaque HMAC RowKeys. Both partitions
map to the same `AppUserId` and require no new Sensei connection setting.

The following values belong only to Pablo's Conversational Care deployment and
must not be added to this Function App: `SENSEI_API_URL`,
`SENSEI_FUNCTION_KEY`, `SENSEI_REQUEST_TIMEOUT_SEC`, and
`SENSEI_EXTERNAL_ID_SECRET`. The Function key is managed by Azure Functions and
sent to this app in the `x-functions-key` request header. Never commit it.

### Chatbot operational state

| Setting | Runtime default |
|---|---|
| `CHATBOT_CONVERSATION_STATE_TABLE` | `ChatbotConversationState` |
| `CHATBOT_USER_PROFILE_TABLE` | `ChatbotUserProfile` |
| `CHATBOT_METRIC_AVAILABILITY_TABLE` | `ChatbotMetricAvailability` |
| `CHATBOT_ALERT_THRESHOLD_TABLE` | `ChatbotUserAlertThreshold` |
| `SYSTEM_STEPS_TRIGGER_STATE_TABLE` | `SystemInitiatedDailyTriggerState` |

### Withings state

| Setting | Runtime default |
|---|---|
| `WITHINGS_DEDUP_TABLE_NAME` | `WithingsIngestDedup` |
| `WITHINGS_WEBHOOK_LEASE_TABLE` | `WithingsWebhookLease` |
| `WITHINGS_SYNC_STATE_TABLE_NAME` | `WithingsSyncState` |

`WithingsTokens` is also required when Withings is enabled and currently has a fixed name.

## 6. Queue contracts

Queue trigger names are fixed deployment contracts in `queue_contracts.py`; they are not environment-variable overrides in this app.

| Queue | Producer | Consumer |
|---|---|---|
| `chatbot-metric-availability-refresh` | Main App timer or HTTP refresh route | `ChatbotMetricAvailabilityRefreshQueue` |
| `system-initiated-phone-ingest-events` | Smartphone Function App | `system_initiated_phone_ingest_queue` |
| `system-initiated-steps-daily-retry` | Main App proactive workflow | `system_initiated_steps_retry_queue` |
| `withings-webhook-events` | Withings Webhook Function App | `WithingsWebhookQueueWorker` |

All producers must enqueue to the same Azure Storage account referenced by this app's `AzureWebJobsStorage`. The Smartphone App setting `SYSTEM_PHONE_INGEST_EVENT_QUEUE` and the Withings Webhook App setting for its queue must retain these exact names even though those settings belong to the sibling apps.

## 7. Chatbot behavior settings

| Setting | Runtime default | Purpose |
|---|---|---|
| `CHATBOT_DEFAULT_TIMEZONE` | `Asia/Seoul` | Fallback timezone. |
| `CHATBOT_SUPPORTED_SITE_TIMEZONES_JSON` | Seoul, London, Madrid, Canary, Ceuta map | Site-code to IANA timezone map. |
| `CHATBOT_STRICT_TIMEZONE_ONBOARDING` | `false` | Blocks health-data queries until timezone onboarding is resolved. |
| `CHATBOT_MAX_RESULT_ROWS` | `40` | Rows retained for response processing. |
| `CHATBOT_MAX_KQL_REPAIR_ATTEMPTS` | `2` | Bounded KQL guard/repair attempts. |
| `KQL_GENERATE_METHOD` | `llm` | Explicit KQL strategy. Allowed values are `llm` and `deterministic`; invalid values fail fast. |
| `CHATBOT_ENABLE_GENERIC_KQL_FALLBACK` | `false` | Deprecated compatibility setting. It is not the generation-strategy selector; keep it `false` and use `KQL_GENERATE_METHOD`. |
| `CHATBOT_DEFAULT_TREND_DAYS` | `14` | Default trend range. |
| `CHATBOT_AVAILABILITY_RECENT_DAYS` | `4` | Recent-data window used by availability refresh. |
| `CHATBOT_ENABLE_OPTIONAL_WITHINGS_TABLES` | `false` | Opts the chatbot into scanning and querying separately maintained direct-Withings tables for vendor-only metrics. Keep `false` when all supported data flows through `HealthDataFlat`; direct Withings is never a generic missing-data fallback. |
| `CHATBOT_RECENT_TURN_LIMIT` | `12` | Recent turns provided to the graph. |
| `CHATBOT_DEBUG` | `false` | Extra diagnostic behavior. Keep disabled in production. |
| `CHATBOT_DEBUG_ERRORS` | `false` | Exposes additional Telegram error detail. Keep disabled in production. |

### Response-chain and episode controls

| Setting | Runtime default |
|---|---|
| `CHATBOT_USE_AZURE_RESPONSES_API` | `false` |
| `CHATBOT_RESPONSE_STORE` | `true` |
| `CHATBOT_RESPONSE_CHAIN_MAX_AGE_DAYS` | `25` |
| `CHATBOT_SUSPENDED_EPISODE_LIMIT` | `3` |
| `CHATBOT_FALLBACK_SUMMARY_MAX_CHARS` | `2000` |
| `CHATBOT_PROACTIVE_EPISODE_INTERRUPT_ENABLED` | `true` |

The top-level `memory/` package and its `MULTITURN_*` settings are retained for
source-only research compatibility. The package is excluded by
`.funcignore`; deployed conversation and episode state uses
`ChatbotConversationState` and the settings above.

## 8. Threshold, report, and proactive STEPS settings

| Setting | Runtime default |
|---|---|
| `CHATBOT_THRESHOLD_ALERT_ENABLED` | `true` |
| `CHATBOT_THRESHOLD_ALERT_SUSTAIN_MINUTES` | `10` |
| `CHATBOT_THRESHOLD_ALERT_LOOKBACK_MINUTES` | `60` |
| `CHATBOT_THRESHOLD_ALERT_LLM` | `1` (enabled) |
| `CHATBOT_DAILY_REPORT_ENABLED` | `true` |
| `SYSTEM_STEPS_ALERT_ENABLED` | `true` |
| `SYSTEM_STEPS_ALERT_RETRY_DELAYS_SECONDS_JSON` | `[300, 900, 1800]` |
| `SYSTEM_STEPS_ALERT_TRIGGER_HOUR_LOCAL` | `8` |
| `SYSTEM_STEPS_ALERT_EARLIEST_EVALUATION_HOUR_LOCAL` | `8` |
| `SYSTEM_STEPS_ALERT_EARLIEST_EVALUATION_MINUTE_LOCAL` | `30` |
| `SYSTEM_STEPS_ALERT_FALLBACK_HOUR_LOCAL` | `11` |
| `SYSTEM_STEPS_ALERT_RECONCILIATION_HOUR_LOCAL` | `14` |
| `SYSTEM_STEPS_BASELINE_DAYS` | `14` |
| `SYSTEM_STEPS_BASELINE_MIN_OBSERVED_DAYS` | `10` |
| `SYSTEM_STEPS_BASELINE_MIN_COVERAGE_RATIO` | `0.70` |
| `SYSTEM_STEPS_BASELINE_RULE_VERSION` | `steps_mad_v1` |
| `SYSTEM_STEPS_ALERT_ZMAD_THRESHOLD` | `-3.5` |

The Smartphone Function App must independently enable its event producer and point `SYSTEM_PHONE_INGEST_EVENT_CONN_STR` at the same Storage account used here. Those producer settings do not belong to this Main App.

## 9. Conversation-log settings

Conversation export is opt-in and disabled by default.

| Setting | Runtime default | Purpose |
|---|---|---|
| `TELEGRAM_CONVERSATION_LOG_ENABLE` | `false` | Enables staging and ADX export. |
| `TELEGRAM_CONVERSATION_STORE_TEXT` | `false` | Stores message text when explicitly enabled. |
| `TELEGRAM_CONVERSATION_BUFFER_TABLE` | `TelegramConversationBuffer` | Staging table. |
| `TELEGRAM_CONVERSATION_CHECKPOINT_TABLE` | `TelegramConversationCheckpoint` | Flush checkpoint table. |
| `TELEGRAM_CONVERSATION_BATCH_SIZE` | `500` | Maximum rows per runtime flush. The sample file deliberately overrides this to `100`. |
| `TELEGRAM_CONVERSATION_FLUSH_LOOKBACK_DAYS` | `2` | Retry/lookback window. |
| `TELEGRAM_CONVERSATION_MAX_ATTEMPTS` | `5` | Maximum export attempts. |
| `TELEGRAM_CONVERSATION_EXPORT_LEASE_SECONDS` | `900` | Export claim duration. |
| `TELEGRAM_CONVERSATION_TEXT_MAX_CHARS` | `4096` | Stored-text cap. |
| `TELEGRAM_CONVERSATION_PAYLOAD_MAX_CHARS` | `12000` | Serialized-event cap. |
| `TELEGRAM_CONVERSATION_HASH_SALT` | Empty | Salt for pseudonymous identifiers. Set a strong value when logging is enabled. |
| `TELEGRAM_CONVERSATION_ADMIN_KEY` | Empty | Additional protection for conversation diagnostics; Azure Function authorization is still required. |
| `TELEGRAM_CONVERSATION_SCHEMA_VERSION` | `2` | Lean ADX schema version. |

The lean schema does not store raw Telegram updates, raw chat IDs, or generated KQL. Review retention, access, deletion, and research-consent requirements before enabling message-text storage.

## 10. Optional Withings settings

Withings is optional. Leave its settings unset if the deployment only serves smartphone data.

### OAuth and webhook linkage

| Setting | Runtime default |
|---|---|
| `WITHINGS_CLIENT_ID` | Empty |
| `WITHINGS_CLIENT_SECRET` | Empty |
| `WITHINGS_OAUTH_STATE_SECRET` | Falls back to `WITHINGS_CLIENT_SECRET` |
| `WITHINGS_OAUTH_STATE_TTL_SECONDS` | `600` |
| `WITHINGS_OAUTH_ALLOW_LEGACY_LOCAL_STATE` | `false` |
| `WITHINGS_WEBHOOK_URL` | Empty |

Use a dedicated random `WITHINGS_OAUTH_STATE_SECRET` in production. Keep legacy local state disabled. `WITHINGS_WEBHOOK_URL` is the public callback on the separate Withings Webhook Function App, normally `https://<webhook-app>.azurewebsites.net/api/withings_webhook`.

### Fetch, deduplication, and backfill

| Setting | Runtime default |
|---|---|
| `FETCH_INTRADAY_ON_WEBHOOK` | `true` |
| `FETCH_WORKOUTS_ON_WEBHOOK` | `false` |
| `WITHINGS_INTRADAY_DATA_FIELDS` | `steps,elevation,calories,distance,heart_rate,rmssd,sdnn1,hrv_quality` |
| `WITHINGS_DEDUP_DATA_TYPES` | `Activity,Sleep,SleepDetail` |
| `WITHINGS_WEBHOOK_LEASE_ENABLE` | `true` |
| `WITHINGS_WEBHOOK_LEASE_SECONDS` | `60` |
| `WITHINGS_BACKFILL_DAYS` | `100` |
| `WITHINGS_BACKFILL_PARALLEL` | `2` |
| `WITHINGS_HTTP_MAX_ATTEMPTS` | `5` |
| `WITHINGS_HTTP_BASE_BACKOFF_MS` | `400` |
| `WITHINGS_HTTP_TIMEOUT_SEC` | `15` |

### Incremental windows and empty-result retry

| Setting | Runtime default |
|---|---|
| `WITHINGS_SYNC_ACTIVITY_LOOKBACK_DAYS` | `2` |
| `WITHINGS_SYNC_ACTIVITY_MAX_LOOKBACK_DAYS` | `7` |
| `WITHINGS_SYNC_ACTIVITY_OVERLAP_DAYS` | `1` |
| `WITHINGS_SYNC_SLEEP_LOOKBACK_DAYS` | `3` |
| `WITHINGS_SYNC_SLEEP_MAX_LOOKBACK_DAYS` | `14` |
| `WITHINGS_SYNC_SLEEP_OVERLAP_DAYS` | `1` |
| `WITHINGS_SYNC_INTRADAY_LOOKBACK_HOURS` | `6` |
| `WITHINGS_SYNC_INTRADAY_MAX_LOOKBACK_HOURS` | `24` |
| `WITHINGS_SYNC_INTRADAY_OVERLAP_MINUTES` | `30` |
| `WITHINGS_WEBHOOK_RETRY_ON_EMPTY_ENABLE` | `true` |
| `WITHINGS_WEBHOOK_RETRY_ON_EMPTY_MAX` | `5` |
| `WITHINGS_WEBHOOK_RETRY_ON_EMPTY_BASE_DELAY_SECONDS` | `120` |
| `WITHINGS_WEBHOOK_RETRY_SLEEP_DELAYS` | `120,600,1800,3600,10800,21600,43200` |

## 11. Safe post-deployment inspection

Discover the app in the active subscription and list configured setting names without printing secret values:

```bash
APP_NAME="sensoryllm-azure-func"
RESOURCE_GROUP="$(az functionapp list --query "[?name=='${APP_NAME}'] | [0].resourceGroup" --output tsv)"

az functionapp config appsettings list \
  --name "$APP_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --query '[].name' \
  --output tsv | sort
```

Run the read-only resource verifiers from this directory after authenticating to Azure:

```bash
python scripts/verify_sensei_resources.py
python scripts/verify_chatbot_operational_storage.py --app-user-id "<test-app-user-id>"
```

The scripts require the relevant settings in the current process or a protected local configuration file. They do not copy settings from the deployed Function App automatically. For the full Git Bash publish and Telegram registration sequence, see [README.md](README.md#detailed-manual-deployment).
