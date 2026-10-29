# Sensei Main Azure Function App

![LangGraph multi-agent execution flow](../assets/langgraph-execution-flow.png)

[연구원용 한국어 다중 LLM 평가 안내](README_kr.md) · [현재 D1 프롬프트 한글 검토본](docs/D1_PROMPTS_REVIEW_KR.md)

> **Current graph status:** D1 sensor-data reasoning, D2 coaching strategy,
> D3 approved-corpus health knowledge/safety, and D4 within-session context
> resolution are implemented. D3 fails closed when approved evidence is absent;
> D4 never claims long-term memory.

The Main Function App has two primary runtime layers. [`chatbot/`](chatbot/) is
the transport-independent LangGraph conversational core: it prepares context,
plans and executes domain work, performs guarded health-data retrieval,
constructs responses, and persists conversation context. [`functions/`](functions/)
is the Azure Functions boundary: it defines HTTP, Telegram, timer, queue, and
Durable Functions entry points, validates and adapts incoming events, and then
invokes either the chatbot core or the appropriate service workflow.

Runtime registration and environment/host configuration live alongside those
layers in [`function_app.py`](function_app.py), [`host.json`](host.json),
[`runtime_environment.py`](runtime_environment.py), and the root configuration
helpers. The diagram above describes the reactive conversational graph; service
operations such as ingestion, scheduled monitoring, and queue processing do not
all pass through that graph.

## Purpose and repository role

This directory contains the main backend service for Sensei. It is an Azure Functions application that runs the transport-independent conversational core, Telegram and authenticated external-chat boundaries, user onboarding, health-alert workflows, conversation persistence, and the optional Withings synchronization pipeline.

It is designed to be deployed as a single Python Azure Function App. The service reads health data that was previously ingested into Azure Data Explorer (ADX); it does **not** receive raw smartphone payloads itself. Raw mobile ingestion belongs to [../azure_function_smartphone_app/](../azure_function_smartphone_app/). The optional Withings webhook receiver is deployed from [../azure_function_withings_app/](../azure_function_withings_app/).

> **Implementation status:** The core Telegram, Storage, guarded ADX-query, and
> scheduled-worker code is implemented. Some integrations are configuration-
> dependent or default-off. D3 currently uses a small versioned local retrieval
> index rather than an external vector service, and its emergency rule is marked
> provisional pending clinical governance. See
> [Implementation status](#implementation-status) for the exact boundaries.

This directory is the deployment root: [function_app.py](function_app.py),
[host.json](host.json), [requirements.txt](requirements.txt), and
[.funcignore](.funcignore) belong to one Function App package. Run local and
deployment commands here, not from the monorepo root. The deployable runtime is
under `functions/` and `chatbot/`; `docs/`, `scripts/`, `memory/`, and
`proactive/` are source-only support directories excluded from the Azure
package. The versioned `local_test.py` utility is also excluded from deployment.

For the repository-wide component map and deployment order, start with the [repository README](../README.md).

## Directory at a glance

| Path | Role |
|---|---|
| [function_app.py](function_app.py) | Creates the Azure Durable Functions application and registers every runtime blueprint. Start here to see which HTTP, timer, and queue functions Azure loads. |
| [functions/](functions/) | Implements Azure Function boundaries: Telegram and authenticated external-chat endpoints, Withings endpoints, scheduled jobs, queue consumers, operational routes, and registration workflows. These modules translate trigger payloads and call the appropriate application service. |
| [chatbot/](chatbot/) | Implements the channel-neutral conversational core: LangGraph state and routing, D1-D4 domain execution, guarded ADX queries, prompts, policies, schemas, provider boundaries, storage access, and response generation. |
| [host.json](host.json) | Configures the Azure Functions host, Durable Task hub, queue encoding, logging, and Application Insights behavior. |
| [runtime_environment.py](runtime_environment.py), [azure_storage_resources.py](azure_storage_resources.py), [queue_contracts.py](queue_contracts.py) | Define shared environment parsing, Azure Storage resource construction, and queue-name contracts used across runtime modules. |
| [requirements.txt](requirements.txt) | Declares the Python packages installed for local validation and the Azure deployment build. |
| [local.settings.sample.json](local.settings.sample.json) | Provides a non-secret template for local Function settings. Copy values into an ignored `local.settings.json`; never place real credentials in the sample. |
| [local_test.py](local_test.py) | Runs the supported local smoke check and optional explicitly selected live tests. It is tracked in Git but excluded from the Azure package. |
| [memory/](memory/) and [proactive/](proactive/) | Preserve source-only research or compatibility code. They are not the active runtime implementations and are excluded by `.funcignore`. |
| [docs/](docs/) and [scripts/](scripts/) | Hold architecture/refactoring notes and operator verification utilities. They support development but are not deployed. |
| [.gitignore](.gitignore) and [.funcignore](.funcignore) | Serve different purposes: `.gitignore` protects local files from Git, while `.funcignore` removes tests, documentation, secrets, and local tools from the Azure publish package. |

The active runtime is therefore `function_app.py` plus `functions/`, `chatbot/`,
and their imported root modules. Folder-specific READMEs describe those areas in
more detail.

## Shared ADX query contract

Health-question code is isolated from each database's physical layout. The
planner uses logical resources and generated KQL reads only these six bounded,
database-local stored functions:

| Logical evidence | Contract function |
|---|---|
| Daily, hourly, and interval activity | `SensorActivityDaily(...)`, `SensorActivityHourly(...)`, `SensorActivityInterval(...)` |
| Sampled health observations | `SensorHealthSamples(...)` |
| Nightly sleep | `SensorSleepNights(...)` |
| Workout sessions | `SensorWorkoutSessions(...)` |

Every call has the signature
`(TargetUserId:string, StartLocal:datetime, EndLocal:datetime)`, with an exclusive
upper bound. Python policies, prompts, metric definitions, and KQL compilers must
not name the production backing tables/materialized views or evaluation-native
tables. Those names belong only inside reviewed ADX stored-function definitions.

The same application code supports two data environments:

| Use | Required configuration | Clock and range behavior |
|---|---|---|
| Production | `ADX_DATABASE=healthdb` (or the production database name), production `Sensor*` contract deployed, and `KQL_GENERATE_METHOD=llm` | Uses the real timezone-aware current time. Normal ranges are finite. The current runtime rejects unresolved `all_available` requests instead of running an unbounded production scan. |
| Retrospective local chat testing | The configured database containing the user's data in the shared `Sensor*` contract; `local_llm_test.py` | Defaults to reference-table clocks: summary and workout each have a latest date; cross-stream questions use their maximum. Relative and all-history ranges become finite windows. This clock is bound only to the local request, never the deployed Telegram process. |

`KQL_GENERATE_METHOD` accepts only `deterministic` and `llm`; values are trimmed
and lowercased, and any other value fails configuration validation. Production
uses `llm` by default. The `deterministic` option remains available for a
controlled comparison. Both use the same sanitized plan, safety guard,
executor, and response pipeline. LLM generation sees the canonical schema;
when a bounded diagnostic finds flat activity records missing from the daily
aggregate, it can re-query both approved sources to re-evaluate the question.

Before publishing this app, deploy and validate the appropriate database
contract first. Production uses
[`11_sensor_query_contract_production.kql`](../azure_adx_kql/11_sensor_query_contract_production.kql)
after its raw-to-flat, activity, and sleep dependencies. Data already ingested
into that flat schema, including retrospective test users, uses this same
contract without selecting a dataset-specific runtime prompt. The standalone
evaluation tooling also supports optional isolated fixtures via
[`evaluation/0_phia_raw_tables.kql`](../azure_adx_kql/evaluation/0_phia_raw_tables.kql),
fixture ingestion, and then
[`evaluation/1_phia_sensor_query_contract.kql`](../azure_adx_kql/evaluation/1_phia_sensor_query_contract.kql)
in a separate evaluation database; that is not required for local tests of
already-ingested flat data. See the
[ADX deployment guide](../azure_adx_kql/README.md) and the
[evaluation runner guide](../evaluation/README.md) for the complete order.

## Quick deployment

The following is the concise code-only path for the existing Function App. Run
it from **Git Bash on Windows** and replace the subscription placeholder. Review
[Detailed manual deployment](#detailed-manual-deployment) before the first
deployment or whenever the Azure target or configuration may have changed.

```bash
cd ~/workspace/SensoryLLM/main_github/azure_function_main_app

az login
az account set --subscription "<subscription-name-or-id>"

APP_NAME="sensoryllm-azure-func"
RESOURCE_GROUP="$(az functionapp list --query "[?name=='${APP_NAME}'] | [0].resourceGroup" --output tsv)"
test -n "$RESOURCE_GROUP" || { echo "Function App not found; stop deployment."; exit 1; }

az functionapp show \
  --name "$APP_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --query '{name:name,state:state,host:defaultHostName}' \
  --output table

test -d .venv || py -3.12 -m venv .venv
source .venv/Scripts/activate
python -m pip install -r requirements.txt
python local_test.py smoke

func azure functionapp publish "$APP_NAME" --build remote
```

Stop if the discovered app, subscription, resource group, runtime, or required
Application Settings do not match the intended environment. A normal code-only
publish preserves existing Function App settings, Key Vault references, Storage
data, and ADX data; it does not upload `.env` or `local.settings.json` and does
not create missing Azure resources.

## Detailed manual deployment

This is the shortest complete manual deployment path for the existing Sensei
Main Function App. It is written for **Git Bash on Windows**; installed Windows
`az`, `func`, and `py` commands can be called directly from Git Bash. The target
used by this repository is `sensoryllm-azure-func`, and its expected Azure Linux
runtime is `Python|3.12`.

This procedure updates an already provisioned Function App. It does not create
the Azure resources listed under [Prerequisites](#prerequisites).

### 1. Enter the deployment directory and check the tools

```bash
cd ~/workspace/SensoryLLM/main_github/azure_function_main_app

az version
func --version
py -3.12 --version
```

Install Azure CLI, Azure Functions Core Tools 4, and Python 3.12 before
continuing if any command is unavailable.

### 2. Sign in and select the subscription

```bash
az login
az account list --output table
az account set --subscription "<subscription-name-or-id>"
az account show \
  --query '{subscription:name,id:id,tenantId:tenantId}' \
  --output table
```

Do not publish until `az account show` displays the intended subscription. Azure
CLI discovery and deployment are scoped to that selection.

### 3. Discover and verify the target app

```bash
APP_NAME="sensoryllm-azure-func"

RESOURCE_GROUP="$(
  az functionapp list \
    --query "[?name=='${APP_NAME}'] | [0].resourceGroup" \
    --output tsv
)"

if [ -z "$RESOURCE_GROUP" ]; then
  echo "Function App '${APP_NAME}' was not found in the selected subscription."
  exit 1
fi

HOSTNAME="$(
  az functionapp show \
    --name "$APP_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --query defaultHostName \
    --output tsv
)"

PYTHON_RUNTIME="$(
  az functionapp config show \
    --name "$APP_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --query linuxFxVersion \
    --output tsv
)"

az functionapp show \
  --name "$APP_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --query '{name:name,resourceGroup:resourceGroup,state:state,host:defaultHostName}' \
  --output table

printf 'Python runtime: %s\n' "$PYTHON_RUNTIME"

if [ "$PYTHON_RUNTIME" != "Python|3.12" ]; then
  echo "Expected Python|3.12. Stop and align the Azure runtime before deployment."
  exit 1
fi
```

Stop if the app is not `Running`, the hostname is empty, or the runtime is not
Python 3.12. Those results usually indicate the wrong subscription, the wrong
app, or infrastructure drift.

### 4. Verify configuration without printing secret values

List only Application Setting names:

```bash
az functionapp config appsettings list \
  --name "$APP_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --query '[].name' \
  --output tsv |
sort
```

At minimum, verify the settings required by the features being deployed:

| Feature | Setting names to verify |
|---|---|
| Function runtime | `AzureWebJobsStorage`, `FUNCTIONS_WORKER_RUNTIME` |
| Telegram and registration | `AzureWebJobsAppStorage`, `TELEGRAM_BOT_TOKEN` |
| ADX health queries | `ADX_DATABASE`, `KQL_GENERATE_METHOD=llm`, and either `ADX_QUERY_URI` or `ADX_CLUSTER_URI`; all six `Sensor*` functions must already be deployed |
| Azure OpenAI | `AI_PROVIDER`, `AZURE_OPENAI_BASE_URL`, `AZURE_OPENAI_DEFAULT_DEPLOYMENT` or `AZURE_OPENAI_CHAT_DEPLOYMENT`, and `AZURE_OPENAI_AUTH_MODE`; `AZURE_OPENAI_API_KEY` only in API-key mode |
| OpenAI instead of Azure OpenAI | `AI_PROVIDER`, `OPENAI_API_KEY`, and optionally `OPENAI_CHAT_MODEL` |
| ADX-writing features | `ADX_INGEST_URI` |
| Strict Telegram webhook | `TELEGRAM_WEBHOOK_SECRET_TOKEN`, `TELEGRAM_WEBHOOK_REQUIRE_SECRET=true` |

Check that `ENV_NAME` represents the intended environment. Set or change
Application Settings only through the Azure portal, an approved Key Vault
reference, or an explicit command such as the following:

```bash
az functionapp config appsettings set \
  --name "$APP_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --settings \
    "FUNCTIONS_WORKER_RUNTIME=python" \
    "ENV_NAME=production" \
    "KQL_GENERATE_METHOD=llm" \
  --output none
```

That command merges the named settings; it does not delete other Application
Settings. Use `ENV_NAME=production` only when this target is in fact the
production environment.

Azure Storage is not an environment-variable store. Queue, table, and blob
values do not automatically become Function App settings. Store runtime secrets
under the Function App's **Settings > Environment variables** (or the older
**Configuration > Application settings** UI), preferably as Key Vault
references where appropriate.

### 5. Install dependencies and validate the package

Create the virtual environment once. Reuse it on later deployments.

```bash
test -d .venv || py -3.12 -m venv .venv
source .venv/Scripts/activate

python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python local_test.py smoke
```

The smoke command performs an offline import, Function registration, graph,
prompt-resource, and KQL-safety check. Do not publish after a failed validation
without first understanding the failure.

### 6. Publish the code

```bash
func azure functionapp publish "$APP_NAME" --build remote
```

`--build remote` asks Azure to build the Linux Python dependencies. Do not add
the old `--python` argument: it is not a supported publish option in the current
Azure Functions Core Tools 4 command.

### 7. Verify indexing and the live integration

```bash
az functionapp function list \
  --name "$APP_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --query '[].name' \
  --output table

az functionapp show \
  --name "$APP_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --query '{state:state,host:defaultHostName}' \
  --output table
```

Function indexing proves only that Azure loaded the triggers. Complete the
[post-deployment checks](#post-deployment-checks), including a real Telegram
message and one enabled ADX/timer/queue path. An existing Telegram webhook is
normally retained during a code-only publish. For first-time registration or
strict secret validation, follow
[Configure and register Telegram after publishing](#configure-and-register-telegram-after-publishing).

### What the publish command preserves

| Item | Code-only publish behavior |
|---|---|
| Existing Azure Function App Application Settings and Key Vault references | Preserved; they are not replaced by this command. |
| Azure Storage tables, queues, blobs, and Durable state | Preserved; the app restarts, so triggers can resume afterward. |
| ADX schema and stored health data | Preserved. |
| `local.settings.json`, `.env`, virtual environments, tests, and local experiments | Not uploaded; `.funcignore` excludes them. Keep local copies backed up separately. |
| Files already deployed as Function App code | Replaced by the new deployment package. |

Avoid `--publish-local-settings`, `--publish-settings-only`, and
`--overwrite-settings` unless an intentional settings migration has been
reviewed. A normal publish also does **not** create the Function App, create
missing settings, assign managed-identity roles, apply ADX KQL, register a
Telegram webhook, or configure sibling apps to share a Storage queue.

The manual Git Bash procedure above is the verified deployment path. The
repository's current manual GitHub Actions workflow still declares a different
app (`health-sensing`) and Python 3.11.14. Do not use that workflow for
`sensoryllm-azure-func` until its target, Python version, environment, and
`AZURE_FUNCTIONAPP_PUBLISH_PROFILE` secret have all been deliberately aligned.

## Implementation status

The labels below distinguish missing work from features that already have an implementation but require configuration, user state, or another deployed component. A feature listed as conditional must not be described as undeveloped merely because it is disabled in one environment.

| Status | Capability or path | Operational meaning |
|---|---|---|
| **Implemented: core runtime** | Telegram registration and onboarding, LangGraph routing and planning, deterministic guarded ADX queries, user-facing responses, operational Storage state, alert configuration, and registered timers/queues | The code is part of the deployment package. It still requires the documented Telegram, Storage, ADX, and LLM settings and permissions. |
| **Implemented: configuration or infrastructure dependent** | Optional Withings OAuth/backfill/webhook ingestion, Smartphone-triggered STEPS processing, threshold alerts, and daily reports | The runtime paths exist. Withings requires its credentials, webhook app, shared queue, and ADX resources; STEPS events require the Smartphone Function App producer; alerts and reports require eligible user rules or schedules. |
| **Implemented: default-on or explicitly gated** | Direct LLM KQL generation, Azure Responses API chaining, strict timezone onboarding, Telegram conversation export, and Withings workout fetching | Direct LLM KQL is the default, with deterministic KQL retained for controlled comparisons. Generated queries remain subject to the shared safety guard; the other listed features retain their own gates. |
| **Source-only: not deployed** | [memory/](memory/), [proactive/](proactive/), [scripts/](scripts/), [docs/](docs/), and [local_test.py](local_test.py) | These paths support research, compatibility, validation, or documentation and are excluded by [.funcignore](.funcignore). Active conversation state and proactive delivery use the implementations under `chatbot/` and `functions/`. |
| **Implemented outside this deployment package** | [PHIA D1 evaluation runner](../evaluation/README.md) | Reuses production planning/generation/guard/execution components but is not published as part of this Function App. Its fixture clock and evaluation database must not be enabled in the production graph. |
| **D1-D4 domain orchestration** | D1 owns guarded sensor queries; D2 produces a source-traceable Advisor plan from an explicit 38-item BCTTv1 subset; D3 retrieves approved frozen health evidence and enforces safety; D4 resolves current-session referents | See [graph details](chatbot/graph/README.md). A bounded generic DAG dispatches all four runners and passes only declared dependencies. |
| **Planned extensions; not active** | External/vector D3 retrieval, model-assisted D4 semantic fallback, final clinically governed safety policy, and Auditor/MITI scoring | D3 currently uses a small local approved corpus and D4 uses deterministic reply/latest resolution. Evaluation/Auditor logic remains outside the production Advisor runtime. |

Internal transport decomposition and further provider-adapter migration are also deferred refactoring tasks, not missing end-user features. See [docs/refactoring/TO_BE.md](docs/refactoring/TO_BE.md) for the scoped deferred-work record.

For the prompt resources, their exact runtime consumers, activation conditions,
editing rules, and validation procedure, read
[chatbot/prompts/README.md](chatbot/prompts/README.md). It distinguishes active,
conditional, and deterministic no-prompt paths so default-off behavior is not
mistaken for unfinished code.

## Scope

The Main Function App provides the following production responsibilities:

- Receives Telegram updates and sends Telegram replies.
- Registers users, collects onboarding details, and manages conversation episodes.
- Converts supported health-data questions into guarded ADX queries and presents the results in natural language.
- Stores chatbot state, profiles, threshold rules, metric availability, and conversation-memory records in Azure Table Storage.
- Runs scheduled threshold checks, daily summaries, conversation-log flushing, metric-availability refreshes, and proactive STEPS checks.
- Optionally completes Withings OAuth, runs Durable Functions backfills, and consumes Withings webhook events from Azure Storage Queue.

The service is a backend component, not a mobile client or an ADX schema deployment. The Flutter client is in [../custom_logger_app/](../custom_logger_app/), and the database schema, mappings, functions, and permissions are in [../azure_adx_kql/](../azure_adx_kql/).

## System boundaries and data flow

~~~text
Telegram
  -> POST /api/telegram_webhook
  -> registration/onboarding or chatbot graph
  -> guarded query to ADX when health data is needed
  -> reply and conversation state in Azure Storage

Smartphone Function App
  -> HealthDataRaw in ADX
  -> optional system-initiated-phone-ingest-events queue message
  -> Main Function App STEPS worker

Withings (optional)
  -> Withings Webhook Function App
  -> withings-webhook-events queue
  -> Main Function App queue worker
  -> Withings API, ADX ingestion, and Telegram notification paths
~~~

The chatbot execution path is deliberately separated into stages:

~~~text
hydrate context
  -> route intent
  -> build a data plan when needed
  -> generate deterministic or permitted fallback KQL
  -> inspect KQL safety
  -> repair a rejected-but-repairable query within a fixed retry limit
  -> inspect the repaired KQL again
  -> execute an allowed ADX query
  -> generate a user-facing response
  -> persist context
~~~

The six-stage main graph is in [chatbot/graph/](chatbot/graph/). Actual domain
subgraphs and implementations belong to the D1-D4 packages under
[chatbot/nodes/](chatbot/nodes/), alongside common, planning, orchestration and
operational packages. Old root node paths are thin compatibility shims. See the package READMEs before changing graph or
policy boundaries.

## Runtime interfaces

`function_app.py` keeps `ANONYMOUS` as the application default for protocol callbacks. The Telegram route verifies Telegram's secret-token header whenever a secret is configured and can enforce fail-closed production configuration. The Withings callback verifies a signed, expiring OAuth state. Operational routes that mutate or inspect data override the default with Azure Functions `FUNCTION` authorization.

### HTTP routes

| Method | Route | Function auth | Implementation purpose |
|---|---|---|---|
| POST | /api/telegram_webhook | Application-level Telegram secret policy | Telegram update entry point. A configured secret always requires a matching `X-Telegram-Bot-Api-Secret-Token`; strict mode also rejects missing secret configuration. |
| POST | /api/send_message | Function key | Channel-neutral Sensei boundary used by Conversational Care. Supports structured `message`, `login`, and `register` operations and returns only a public response string. |
| GET | /api/withings_auth | Signed OAuth state | Optional Withings OAuth callback and Durable backfill start. |
| GET, POST | /api/sync_now | Function key | Manually fetch and ingest data for a Withings user. |
| GET | /api/diag_queue | Function key | Inspect the optional Withings queue configuration and messages. |
| GET, POST | /api/enqueue_ping | Function key | Enqueue a Withings-worker diagnostic message. |
| GET | /api/diag_telegram_conversation_log | Function key | Inspect staged Telegram conversation logging. |
| GET, POST | /api/flush_telegram_conversation_log_now | Function key | Flush staged conversation events to ADX. |
| GET, POST | /api/refresh_chatbot_metric_availability | Function key | Refresh, or enqueue a refresh of, the user metric-availability cache. |
| GET, POST | /api/run_threshold_check_now | Function key | Run threshold evaluation for a supplied application user. |
| GET, POST | /api/run_daily_report_now | Function key | Produce or send a user's daily report. |
| GET | /api/diag_threshold_alerts | Function key | Return threshold configuration and a no-send evaluation. |
| GET, POST | /api/diag_system_initiated_steps | Function key | Inspect system-initiated STEPS processing for a user. |
| POST | /api/run_system_initiated_steps_now | Function key | Run STEPS event processing with a supplied payload. |

The conversation-log diagnostics and flush endpoints can additionally require `TELEGRAM_CONVERSATION_ADMIN_KEY`. Treat it as defense in depth, not a replacement for the Function key.

`/api/send_message` accepts an opaque `cc_...` external user id; it must never
receive a WhatsApp phone number. Credential commands are converted by the
Conversational Care server to structured `login` or `register` operations, so
their passcodes do not enter the normal message/LangGraph path. Normal messages
must include Conversational Care's stable `current_thread_id`; Sensei stores the
conversation under `conversational_care:<current_thread_id>`, separate from the
existing `telegram:<chat_id>` history. See
[the integration handoff](../docs/CONVERSATIONAL_CARE_SENSEI_HANDOFF.md) for the
request schemas, caller-side redaction requirements, and deployment handoff.

An unmapped user can also send the exact six-digit Authorization PIN issued by
the authenticated Custom Logger App. Conversational Care forwards it as an
ordinary `operation=message` request; no new CC operation or chat URI is
required. Sensei validates the short-lived code before the unmapped-user gate,
atomically claims it, saves `ConversationalCare/<opaque id> -> AppUserId`, and
returns a connection result. Telegram applies the same flow to an unmapped chat,
so one AppUserId may have an independent mapping in each channel. A bare PIN
never replaces an existing channel mapping.

### Timers, queues, and Durable Functions

| Trigger | Binding or schedule | Responsibility |
|---|---|---|
| ThresholdAlertTimer | Every 15 minutes | Evaluates configured health thresholds. |
| DailyReportTimer | Every 5 minutes | Delivers due per-user daily summaries. |
| TelegramConversationFlushTimer | Every 5 minutes | Flushes staged conversation records to ADX. |
| ChatbotMetricAvailabilityRefreshTimer | Daily at 03:00 UTC | Enqueues metric-availability refreshes. |
| ChatbotMetricAvailabilityRefreshQueue | chatbot-metric-availability-refresh | Refreshes an individual user's available metrics. |
| system_initiated_phone_ingest_queue | system-initiated-phone-ingest-events | Handles optional STEPS events emitted by the Smartphone Function App. |
| system_initiated_steps_retry_queue | system-initiated-steps-daily-retry | Retries STEPS processing. |
| SystemInitiatedStepsFallbackTimer | Every 15 minutes | Evaluates fallback STEPS work. |
| SystemInitiatedStepsReconciliationTimer | Every 15 minutes | Reconciles proactive STEPS state. |
| WithingsWebhookQueueWorker | withings-webhook-events | Consumes optional Withings webhook events. |
| backfill_orchestrator / fetch_and_ingest | Durable orchestration and activity | Retrieves and ingests optional historical Withings data. |

The Withings worker binding is fixed to withings-webhook-events. The queue producer in the Webhook Function App and the Main App consumer must point at the same Azure Storage Queue.

## Prerequisites

Before deploying the service, provision and validate the following resources:

| Resource | Why it is needed |
|---|---|
| Azure Function App (Python) | Hosts HTTP, timer, queue, and Durable Functions triggers. |
| Azure Storage Account | Functions host storage, Durable state, queues, and application Table Storage. |
| Azure Data Explorer | Stores raw/normalized health data, optional Withings data, alert outputs, and optional conversation logs. |
| Azure OpenAI or OpenAI API | Supports intent handling, planning, and user-facing responses. |
| Telegram bot | Supplies the inbound webhook and outbound messaging token. |
| Withings developer application | Required only when enabling Withings OAuth and webhook synchronization. |

Apply the ADX definitions in [../azure_adx_kql/](../azure_adx_kql/) before enabling chatbot health queries or ingestion. The Function App managed identity needs the ADX permissions appropriate to its work: at minimum, query access for chatbot reads and ingestion access for the pipelines that write ADX data. Use the repository's ADX permission scripts as the source of truth.

The application uses Azure Table Storage for several feature-specific records. It idempotently ensures its tables and queues for compatibility with the original ZIP and local development, while still working with pre-provisioned resources when the identity lacks create permission. Production deployments should provision and validate these resources through infrastructure automation. Common defaults include:

| Feature | Default tables |
|---|---|
| Registration and channel mapping | Users, UserMapping, TelegramRegisterState |
| Temporary chat-account pairing | PairingRequests |
| Chatbot state, episodes, and preferences | ChatbotConversationState, ChatbotUserProfile, ChatbotMetricAvailability, ChatbotUserAlertThreshold |
| Conversation-log staging | TelegramConversationBuffer, TelegramConversationCheckpoint |
| Optional Withings synchronization | WithingsTokens, WithingsSyncState, WithingsIngestDedup, WithingsWebhookLease |
| Proactive STEPS processing | SystemInitiatedDailyTriggerState |

The `UserMapping` table retains existing `PartitionKey=Telegram` rows and also
stores `PartitionKey=ConversationalCare` rows whose `RowKey` is the caller's
opaque HMAC id. Both row types point to the same `AppUserId`; no second account
or credential table is introduced. The service also uses the queue names listed
in the trigger table. Treat queue/table names as integration contracts: change
a name only after updating every producer, consumer, setting, and deployment
environment that depends on it.

`PairingRequests` is shared with the Smartphone Function App through
`AzureWebJobsAppStorage` (falling back to `AzureWebJobsStorage`). It contains
HMAC-SHA256 row keys, five-minute expiry metadata, a per-user active-code
pointer, and hashed attempt/claim identities; it never stores a raw six-digit
PIN. Successful pairing creates the long-lived record in `UserMapping` and
marks the temporary request consumed.

## Configuration

Use `local.settings.json` only for local development. It is ignored by Git. In Azure, set the same values under **Function App > Configuration > Application settings** or through an approved secret-management workflow. Never copy production secrets into documentation, source files, test fixtures, or client applications.

Start with [local.settings.sample.json](local.settings.sample.json), then add the configuration required by the enabled features.

### Core settings

| Setting | Required when | Notes |
|---|---|---|
| AzureWebJobsStorage | Always | Function runtime, Durable state, queues, and most Table Storage operations. |
| AzureWebJobsAppStorage | Registration is enabled | Connection string used for the application Users table; may intentionally be the same account as AzureWebJobsStorage. |
| PAIRING_HMAC_SECRET | Chat-account PIN pairing is enabled | A random secret of at least 32 UTF-8 bytes. Configure the exact same value in the Smartphone and Main Function Apps; never place it in the mobile client. |
| PAIRING_REQUESTS_TABLE_NAME | Chat-account PIN pairing is enabled | Shared temporary table; defaults to `PairingRequests`. |
| PAIRING_CODE_TTL_SECONDS | Chat-account PIN pairing is enabled | Shared validity limit; defaults to 300 seconds and must match the Smartphone Function App. |
| FUNCTIONS_WORKER_RUNTIME | Always | Set to python. |
| BASE_URL | Withings OAuth is enabled | Public origin only, such as `https://example.azurewebsites.net`; do not include `/api`. A legacy trailing `/api` is normalized for compatibility. |
| TELEGRAM_BOT_TOKEN | Telegram is enabled | Bot API credential. |
| TELEGRAM_WEBHOOK_SECRET_TOKEN | Recommended for Telegram; required when strict mode is enabled | Secret supplied to Telegram `setWebhook`. If set, every webhook request must contain the matching `X-Telegram-Bot-Api-Secret-Token` header. Telegram accepts 1-256 characters from `A-Z`, `a-z`, `0-9`, `_`, and `-`. |
| TELEGRAM_WEBHOOK_REQUIRE_SECRET | Telegram production deployments | Defaults to `false` for compatibility with existing webhooks. Set it to `true` with `TELEGRAM_WEBHOOK_SECRET_TOKEN` in production so a missing secret fails closed. |
| WITHINGS_OAUTH_STATE_SECRET | Withings OAuth is enabled | HMAC key for the time-bounded OAuth state. Use a dedicated random secret; existing deployments fall back to `WITHINGS_CLIENT_SECRET`. |
| ADX_INGEST_URI | ADX ingestion/logging is enabled | ADX ingestion endpoint. |
| ADX_QUERY_URI or ADX_CLUSTER_URI | Chatbot health queries are enabled | ADX query endpoint. |
| ADX_DATABASE | Any ADX feature is enabled | Target ADX database. Use the production database for the deployed app; the separate PHIA database is for the evaluation runner. |
| KQL_GENERATE_METHOD | Explicit in production | `llm` (default) or `deterministic` (comparison). Invalid values fail fast. |

### Select one LLM provider

| Provider | Required settings |
|---|---|
| Azure OpenAI | AI_PROVIDER=azure_openai_v1, AZURE_OPENAI_BASE_URL, AZURE_OPENAI_DEFAULT_DEPLOYMENT (or AZURE_OPENAI_CHAT_DEPLOYMENT), and AZURE_OPENAI_AUTH_MODE. Set AZURE_OPENAI_API_KEY only for API-key authentication. |
| OpenAI API | AI_PROVIDER=openai, OPENAI_API_KEY, and optionally OPENAI_CHAT_MODEL. |

For Azure OpenAI, the supported authentication modes are api_key and managed_identity. Grant any managed identity the appropriate Azure OpenAI role before using the latter.

### Operational, privacy, and feature settings

| Setting family | Purpose |
|---|---|
| CHATBOT_* | Chatbot result limits, timezone behavior, memory, storage table names, alerts, and debugging. |
| TELEGRAM_CONVERSATION_* | Conversation-log collection, redaction-sensitive fields, retention/flush behavior, and diagnostics protection. |
| TELEGRAM_PASSCODE_* | Maximum failed passcode attempts and lockout duration shared by login and Withings-link verification. |
| SYSTEM_PHONE_INGEST_EVENT_* | Optional Smartphone App STEPS event publishing/consumption contract. |
| SYSTEM_STEPS_* | Proactive STEPS retry queue and state configuration. |
| WITHINGS_* | Optional OAuth, backfill, API retry, deduplication, webhook, and synchronization controls. |

Important production defaults to review explicitly:

- Keep CHATBOT_DEBUG disabled.
- Set a strong TELEGRAM_CONVERSATION_HASH_SALT if conversation logging is enabled.
- Conversation logging and message-text storage are disabled unless explicitly enabled. Decide deliberately whether `TELEGRAM_CONVERSATION_STORE_TEXT` should be enabled. The lean logger stores a salted chat-ID hash; it does not store raw Telegram updates, raw chat IDs, or KQL text.
- Keep `WITHINGS_OAUTH_ALLOW_LEGACY_LOCAL_STATE` false. It exists only for an explicit local-development migration test and is ignored outside local/dev/test environments.
- Set `KQL_GENERATE_METHOD=llm` explicitly in production after validating the target ADX contract. Do not use the deprecated `CHATBOT_ENABLE_GENERIC_KQL_FALLBACK` setting as a method selector; keep it `false`.
- Keep `CHATBOT_ENABLE_OPTIONAL_WITHINGS_TABLES=false` for the shared Sensor-contract deployment. Enable it only when the separate optional Withings KQL objects are deployed and vendor-only chatbot fields are intentionally supported.
- Configure TELEGRAM_CONVERSATION_ADMIN_KEY before exposing conversation-log diagnostics.

## Local development

### 1. Create a Python environment

PowerShell:

~~~powershell
cd azure_function_main_app
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
~~~

Git Bash:

~~~bash
cd azure_function_main_app
py -3.12 -m venv .venv
source .venv/Scripts/activate
pip install -r requirements.txt
~~~

### 2. Create local settings

~~~powershell
Copy-Item local.settings.sample.json local.settings.json
~~~

Replace placeholder values only in your local copy. Use Azurite or a development Storage account for the Functions host. Features that call ADX, Telegram, Azure OpenAI, OpenAI, or Withings require their corresponding development resources and credentials.

The ADX data clients are implemented for managed identity in the deployed Function App. Local route and unit testing can proceed without live ADX ingestion, but end-to-end ingestion should be validated in an Azure environment with the required identity and ADX permissions.

### 3. Start the Functions host

~~~powershell
func start
~~~

Azure Functions Core Tools loads local.settings.json into the host environment. Do not commit that file after testing.

### 4. Validate locally

~~~powershell
python local_test.py smoke
python local_test.py system-preview --current 2500 --median 7000
~~~

The smoke command is offline and does not call Azure. To exercise the real
user-initiated graph against configured development resources, run:

~~~powershell
python local_test.py ask --user-id <test-user-id> --message "How many steps did I take yesterday?"
~~~

For an interactive D1 model comparison, run the separate local chat tool:

~~~powershell
python local_llm_test.py --list-models
python local_llm_test.py                         # prompts for AppUserId; blank = kist008, model = gpt-5
python local_llm_test.py --model claude-opus-5-5 --user-id kist008 --clock current
python local_llm_test.py --model gpt-5 --user-id 41 --isolated --question "How many steps did I take yesterday?" --trace-output evaluation-output/u41.json
python local_llm_test.py --isolated --user-id 41
python local_llm_test.py --user-id kist008 --clock current # live Custom Logger data
python local_llm_test.py --models gpt-5 claude-opus-5-5 --user-id 333 --question "How many steps did I take yesterday?" --trace-output evaluation-output/model-comparison.json
python local_llm_test.py --all-models --user-id 333 --question "How many steps did I take yesterday?" --trace-output evaluation-output/all-models.json
~~~

`local_llm_test.py` keeps the same LangGraph and data access for every model.
`--models` and `--all-models` repeat the same supplied questions on each selected
deployment. These comparison modes automatically use isolated in-memory storage
and fresh threads, so one model cannot inherit another's conversations or cached
inventory. All planning, KQL and response calls use the selected model; no fallback
to a different model is performed after a failure. The JSON trace identifies the
model, actual query, returned rows, answer and execution status for each question.
Each question invokes several LangGraph model stages; an all-model comparison is
not a single API call and consumes Azure quota/billable tokens. `--list-models`
lists configured deployment names, not a guarantee of current Azure availability.
For a validated conditional workout plan that also references another measurement
stream, the planner performs one bounded observed-schema review with the same
selected LLM. The prior plan is a hypothesis, not an answer fixture. The review
may keep the independent stream, select an intended session attribute, or ask
about an essential ambiguity; absence alone never justifies substituting metrics.
Unavailable schema discovery keeps the original plan and records the limitation.
The default `--clock referenced-data` discovers summary/daytime and workout
table dates separately, without filtering by the requested metric or activity.
After semantic planning, a summary-only question uses the summary clock, a
workout-only question uses the workout clock, and a cross-stream question uses
the maximum of both clocks. It does not infer a lost summary date from an
overnight start/end timestamp. The bounded lookup covers the most recent
3,660 days and reports missing clock evidence explicitly. Both table clocks
are printed at startup; each trace records the effective question clock.
For an effective today of October 31, yesterday is October 30 and the past
seven days are October 24-30. `--clock latest-data` remains available for the
older single-global-date convention, but is no longer the default.
The shared LLM planner distinguishes completed-period reductions from
chronological latest-event searches. The latter can include today within a
bounded horizon; its typed `include_today` decision is not overwritten by a
keyword parser. This changes the requested period, not the caller's clock.
Sleep-night questions use a declared noon-to-noon `night_day` label: an onset
after midnight and before noon belongs to the preceding night. Explicit wake
or civil-onset questions retain those separate meanings. This label applies
only to sleep, never to workout civil dates. Event retrieval can be padded to
capture crossing sleep observations, but outputs/joins keep the exact logical
period and recorded durations. No producer reporting date is inferred as fact.
Use `--clock current` for the real current date, or paired `--as-of` and
`--data-start` to specify a known logical period explicitly. `--fresh-thread` isolates each supplied
question, and `--trace-output` records the plan, generated KQL, rows and answer.
`--isolated` keeps test profiles, availability and conversation state in memory
instead of Azure Storage; `--timezone` sets that test profile (default
Asia/Seoul). ADX reads and all model calls are still real. Use this for fixture
users that have no app/chat onboarding profile. Without `--isolated`, the
normal configured operational storage is still used. Telegram never imports
this local tool and continues using the real timezone-aware current time.
For explicit evaluation-only unit conventions, use `--stored-speed-unit m/s`
and `--question-speed-unit m/min`. These opt-in settings declare the recorded
field unit and clarify only speed thresholds with omitted units. Recorded speed
values are not rescaled. Explicit units already in the question take precedence.
Neither option is set by production routes. See the
[evaluation unit boundary](../docs/evaluation/LOCAL_EVALUATION_UNIT_BOUNDARY.md)
for the original-reference comparison policy and assumptions.
It selects the model for both KQL generation and the final answer, and sets
`KQL_GENERATE_METHOD=llm` only in this local process. Supported deployment names,
the shared Azure resource address, and API paths are in
[local_models.json](local_models.json). All deployments use the existing
`AZURE_OPENAI_API_KEY` from private local settings; no new secret is required.
Per-model output settings: `max_output_tokens` (default 32768; set near each
deployment's maximum so evaluation answers are never cut off: 65536, Claude
64000, llama 32768 so a looping response still ends within the request
timeout), optional `max_output_tokens_retry` (one larger-budget retry when a
chat response is cut off), and `repetition_retry_frequency_penalty` (one retry
with that penalty when a chat response loops on repeated tokens; used for
llama-4-maverick, which loops deterministically on some inputs at temperature 0).
Running many evaluation processes at once can hit ADX throttling (HTTP 429);
the client retries 429 with backoff, but keep `--workers` at or below the
number of models.

`run_phia_all.py` follows the shared PHIA runner (same question parser,
resumable chunks, `--redo-network-errors`, and `--retry-failed`, which re-asks
only LLM-call/network failures and puts the answers back in place). The PHIA D1
evaluation rules live in that file only; the chatbot code and prompts carry
none of them. Each chunk process (`run_phia_all.py --phia-d1-child <routing>
<local_llm_test arguments>`) installs them in its own process and then runs
`local_llm_test.py` unchanged:

- `--routing d1` (default) sends every question down the D1 data path without
  the domain-routing LLM call; `--routing auto` keeps the chatbot's own routing
  (D1-D4, chat/data/unsupported) for later Custom Logger evaluations.
- The reference date is a completed day of a historical dataset; the D1
  planner receives that fact as evaluation context (not in the question text).
- A benchmark question cannot answer a clarification, so the D1 plan proceeds
  with its stated interpretation, or reports why no query ran.
- Two KQL repair attempts per question (the chatbot allows one).

Chunk logs are written unbuffered, so a running chunk shows its progress. A
chunk whose log shows no new output for `--stall-minutes` (default 20; a normal
question takes one to three minutes) is stopped and counted as failed, and it
runs again when the same command is repeated.

Each trace record has a `timings` object. Every model call is split into connection setup
(DNS/TCP/TLS/upload) and server time (request sent -> response done). Each chunk process first
measures the model's server baseline (median server time of three minimal requests);
LLM inference = server time minus that baseline per request, LLM wait = connection setup +
baseline + retry back-off. This works the same for deployments that buffer the whole response (e.g. grok on
Azure), where token streaming cannot time generation. The graded CSV keeps the original
columns and adds Total Time, LLM Inference Time, LLM Wait Time, ADX Time, Output Tokens,
Reasoning Tokens and LLM Calls; per-stage times, input tokens and streamed timings stay in the trace.
`summarize_phia_results.py` builds the per-model summary table.
Set `SENSEI_LOCAL_LLM_STREAM=0` to call models without streaming.

Answer conventions shared by the chatbot and the grader (team decision, 2026-10-21):
no matching records means no value (NULL), except a count, which is 0; "last/past/previous
week" is the previous Sunday-Saturday calendar week, not the last 7 days; numeric answers
match within 0.01 (`grade_phia_traces.py --tolerance`). Record types are not fixed by rule:
a plan identifier names the quantity, and the query step picks the stored type from the
user's observed inventory (e.g. a once-a-day resting value when no continuous reading exists).
When a query returns no evidence and every record Type or activity label it filters on is
absent from that inventory, query generation runs once more with only that observed fact and
the earlier query (`chatbot/policies/evidence_feedback.py`, traced as
`kql_generation_trace.evidence_feedback`); the model decides whether an observed record answers
the question or the empty result stands.

`ask` is a live integration command and may call Azure OpenAI, ADX, and Table
Storage. `system-reply` additionally writes a synthetic proactive episode and
therefore requires `--confirm-write`; use only a dedicated development user and
storage account. See `python local_test.py --help` for the complete interface.

The following scripts help validate provisioned resources:

~~~powershell
python scripts\verify_sensei_resources.py
python scripts\verify_chatbot_operational_storage.py --app-user-id <test-user-id>
~~~

## Deployment details and integration hardening

Use [Detailed manual deployment](#detailed-manual-deployment)
for the ordered login, subscription selection, target validation, test, publish,
and indexing commands. Deploy this directory as its own Function App package;
do not deploy the monorepo root or a sibling component in its place.

### What publishing does and does not do

The publish command uploads code, asks Azure to build the Python dependencies, and reports the functions that Azure indexed. Its function list does **not** prove that Telegram can deliver updates or that ADX, Storage, and the LLM are configured. Publishing does not:

- create or update the application's required Azure app settings;
- assign managed-identity roles;
- apply the ADX schema, mappings, or permissions;
- register the Telegram webhook; or
- make the Smartphone or Withings Webhook Function Apps use the same queue Storage account.

The repository also includes a manually triggered [GitHub Actions workflow](../.github/workflows/main-azure-function-app.yml) for this component. Review the target app name, environment, and required deployment secret before running it.

Before publishing:

1. Configure app settings and managed identities.
2. Apply the ADX schema, mappings, permissions, and the six-function production
   Sensor contract in the order documented by the ADX guide.
3. Verify all six `Sensor*` functions with bounded test-user calls. Do not deploy
   application code that depends on a contract which has not passed this check.
4. Provision required Storage queues and tables (the runtime's idempotent ensure
   is a compatibility fallback, not a substitute for production infrastructure
   management).
5. Deploy the Smartphone App first if mobile ingestion or proactive STEPS events
   are enabled.
6. Deploy the optional Webhook App before registering a Withings callback.
7. Confirm `.funcignore` excludes local settings, keys, virtual environments,
   tests, and local experiments from the deployment artifact.

### Configure and register Telegram after publishing

The Telegram endpoint is backward compatible. When `TELEGRAM_WEBHOOK_SECRET_TOKEN` is empty and `TELEGRAM_WEBHOOK_REQUIRE_SECRET=false` (the default), the app accepts the existing secretless Telegram webhook. This avoids breaking an older deployment during a code-only publish. It is not the recommended production state.

#### Why an existing bot can stop after a code-only publish

`TELEGRAM_BOT_TOKEN` and `TELEGRAM_WEBHOOK_SECRET_TOKEN` are different
credentials:

- the bot token authorizes this application to call the Telegram Bot API;
- the webhook secret lets this application verify that an incoming HTTP request
  was sent through the Telegram webhook configured for that bot.

The original application accepted Telegram updates without a webhook secret, so
an older Function App normally has only `TELEGRAM_BOT_TOKEN`. A refactoring
revision briefly treated the newly introduced webhook secret as mandatory and
returned HTTP 503 whenever it was absent. `func azure functionapp publish`
uploaded that code but did not create the new Application Setting or call
Telegram `setWebhook`, which explains why Azure indexed all functions while the
bot stopped replying. The current implementation restores secretless
compatibility by default and keeps strict validation as an explicit production
option.

Values stored in an Azure Storage account are not automatically available as
Function App environment variables. Storage holds queues, tables, blobs, and
account credentials; this application reads Telegram settings from the Function
App process environment. Configure them under **Azure Portal > Function App >
sensoryllm-azure-func > Settings > Environment variables** (called
**Configuration > Application settings** in the older portal UI), or use the
Azure CLI. An Application Setting may contain a direct value or an Azure Key
Vault reference.

No new Telegram setting is required for immediate compatibility recovery:
publish the corrected code while retaining the existing `TELEGRAM_BOT_TOKEN`.
If operators prefer to make the compatibility choice explicit, they can set
only the non-secret flag:

~~~bash
az functionapp config appsettings set \
  --name sensoryllm-azure-func \
  --resource-group Minder-ConvAI \
  --settings "TELEGRAM_WEBHOOK_REQUIRE_SECRET=false" \
  --output none
~~~

For production, configure both settings and register the exact same secret with Telegram. The following Git Bash sequence keeps the bot token and generated webhook secret out of the command history and does not print either value:

~~~bash
APP_NAME="sensoryllm-azure-func"
RESOURCE_GROUP="$(az functionapp list --query "[?name=='${APP_NAME}'] | [0].resourceGroup" --output tsv)"
HOSTNAME="$(az functionapp show --name "$APP_NAME" --resource-group "$RESOURCE_GROUP" --query defaultHostName --output tsv)"
WEBHOOK_URL="https://${HOSTNAME}/api/telegram_webhook"

read -rsp "Telegram bot token: " TELEGRAM_BOT_TOKEN
printf '\n'
TELEGRAM_WEBHOOK_SECRET_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"

az functionapp config appsettings set \
  --name "$APP_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --settings \
    "TELEGRAM_WEBHOOK_SECRET_TOKEN=${TELEGRAM_WEBHOOK_SECRET_TOKEN}" \
    "TELEGRAM_WEBHOOK_REQUIRE_SECRET=true" \
  --output none

export TELEGRAM_BOT_TOKEN TELEGRAM_WEBHOOK_SECRET_TOKEN WEBHOOK_URL
python - <<'PY'
import json
import os
import urllib.parse
import urllib.request

token = os.environ["TELEGRAM_BOT_TOKEN"]
payload = urllib.parse.urlencode({
    "url": os.environ["WEBHOOK_URL"],
    "secret_token": os.environ["TELEGRAM_WEBHOOK_SECRET_TOKEN"],
}).encode()
request = urllib.request.Request(
    f"https://api.telegram.org/bot{token}/setWebhook",
    data=payload,
    method="POST",
)
with urllib.request.urlopen(request, timeout=30) as response:
    result = json.load(response)
if not result.get("ok"):
    raise SystemExit(f"Telegram rejected setWebhook: {result}")
print("Telegram webhook registration succeeded.")

with urllib.request.urlopen(
    f"https://api.telegram.org/bot{token}/getWebhookInfo", timeout=30
) as response:
    info = json.load(response).get("result", {})
print(json.dumps({
    "url": info.get("url"),
    "pending_update_count": info.get("pending_update_count"),
    "last_error_message": info.get("last_error_message"),
}, indent=2))
PY
~~~

Changing an Azure app setting restarts the Function App. Wait for the app to return to `Running`, then use these non-destructive probes:

~~~bash
az functionapp show --name "$APP_NAME" --resource-group "$RESOURCE_GROUP" \
  --query '{state:state,host:defaultHostName}' --output table

# Strict mode should reject a request that has no Telegram secret header.
curl -sS -o /dev/null -w 'Unauthenticated probe HTTP %{http_code}\n' \
  -X POST "$WEBHOOK_URL" \
  -H 'Content-Type: application/json' \
  --data '{"update_id":0}'

# This authenticated, message-free update should return HTTP 200 and has no bot side effect.
python - <<'PY'
import json
import os
import urllib.request

request = urllib.request.Request(
    os.environ["WEBHOOK_URL"],
    data=json.dumps({"update_id": 0}).encode(),
    headers={
        "Content-Type": "application/json",
        "X-Telegram-Bot-Api-Secret-Token": os.environ["TELEGRAM_WEBHOOK_SECRET_TOKEN"],
    },
    method="POST",
)
with urllib.request.urlopen(request, timeout=30) as response:
    print(f"Authenticated probe HTTP {response.status}")
PY

unset TELEGRAM_BOT_TOKEN TELEGRAM_WEBHOOK_SECRET_TOKEN
~~~

The unauthenticated probe should return `403`, and the authenticated probe should return `200`. Finally, send a real message to the bot and inspect Application Insights if no reply arrives. Do not test the webhook with a browser `GET`; Telegram invokes it with `POST`.

For Withings, the OAuth redirect handled here is:

~~~text
https://<main-function-app>.azurewebsites.net/api/withings_auth
~~~

The external Withings callback belongs to the separate Webhook App, not this service.

## Post-deployment checks

Perform a controlled end-to-end check using a non-production test user:

1. Confirm the Function App starts without missing-setting errors.
2. Run the strict-mode webhook probes, inspect Telegram `getWebhookInfo`, and complete registration/onboarding through a real bot message.
3. Ask a supported health-data question and verify that the expected ADX data is read.
4. Verify a timer or queue feature that is enabled in the target environment.
5. If mobile STEPS events are enabled, ingest a payload through the Smartphone App and confirm the Main App consumes the queue message.
6. If Withings is enabled, validate the queue handoff before registering a live callback.
7. Review Application Insights and ADX retention/access controls.

## Security, privacy, and operations

- Telegram and OAuth callbacks remain anonymous at the Azure Functions authorization layer because the providers cannot send a Function key. A configured Telegram secret is checked in constant time. Production should set `TELEGRAM_WEBHOOK_REQUIRE_SECRET=true` so missing secret configuration returns `503`; compatibility mode accepts secretless requests only when the secret is absent and strict mode is disabled. Withings callbacks require a valid HMAC-signed state issued within the configured TTL.
- Passcode attempts are counted both per channel subject/account pair and per account, then locked in shared Table Storage after repeated failures. Counter updates use optimistic ETag concurrency so parallel requests cannot overwrite one another. The `Users.Passcode` property remains plaintext only because the separately deployed Smartphone Function App currently depends on that storage contract; migrate both apps together before changing it to a password hash.
- Pairing PINs use a cross-app HMAC-SHA256 contract, expire after five minutes, and are bound atomically to the first unmapped channel identity that claims them. Invalid guesses are throttled per hashed channel identity. Telegram conversation logging redacts a PIN candidate before deriving stored text or command fields.
- Treat health payloads, Telegram content, user IDs, OAuth tokens, connection strings, and API keys as sensitive data.
- Define ADX retention, deletion, access-control, and audit policies before enabling conversational logging or raw health-data storage.
- Do not extend conversation logging to raw Telegram updates, chat IDs, or KQL text without a documented privacy and security review.
- Limit LLM output, ADX result size, backfill windows, queue retries, and timer work according to cost and reliability requirements.
- Rotate Function keys, bot tokens, API keys, and OAuth credentials through an approved secret-management process.

## Related documentation

- [Repository README](../README.md): repository layout and component relationships.
- [chatbot/README.md](chatbot/README.md): chatbot package overview.
- [chatbot/prompts/README.md](chatbot/prompts/README.md): prompt inventory, runtime consumers, placeholders, and editing rules.
- [chatbot/graph/README.md](chatbot/graph/README.md): graph topology and routing.
- [chatbot/nodes/README.md](chatbot/nodes/README.md): node responsibilities and compatibility layers.
- [chatbot/policies/README.md](chatbot/policies/README.md): deterministic policy boundary.
- [ARCHITECTURE.md](ARCHITECTURE.md): additional service architecture notes.
- [Instruction modification guide](Instruction_modification_guide.md): onboarding, prompt, capability, and deterministic-message changes.
- [Environment settings guide](ENVIRONMENT_VARIABLES_TO_SET_AFTER_DEPLOYMENT.md): Azure app settings and required Storage/ADX resources.
- [Smartphone Function App README](../azure_function_smartphone_app/README.md): mobile-data ingestion API.
- [Withings Webhook Function App README](../azure_function_withings_app/README.md): optional Withings webhook handoff.
- [ADX KQL README](../azure_adx_kql/README.md): ADX schema and query assets.
