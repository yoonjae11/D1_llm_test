# Chatbot application package

> Status: **Active runtime package**

## Purpose

This package implements the transport-independent health-data assistant used by
the Main Azure Function App. Telegram transport and Azure trigger bindings live
in [`../functions/`](../functions/); this package owns the conversation graph,
health-data query policy, provider boundaries, prompt resources, response
construction, alerts, and chatbot-specific operational storage.

The public transport entry point is `chatbot.entrypoint.handle_user_message`.
The Telegram handler uses the same entrypoint. New transports should also
start there or at the Sensor Analytics service rather than importing individual
graph nodes.

## At a glance

| Path | Responsibility | Implementation status |
|---|---|---|
| [`graph/`](graph/) | Shared state and the six-stage top-level LangGraph. | Active runtime. |
| [`nodes/`](nodes/) | Common stages, generic planning/orchestration, operational responders, and D1-D4 subgraphs. The root contains no executable node modules. | D1-D4 are active with typed, fail-closed domain results. |
| [`policies/`](policies/) | Deterministic domain, time-range, plan, KQL, availability, result, follow-up, and formatting rules. | Active runtime. |
| [`prompts/`](prompts/) | Versioned prompt resources and their central loader. D3/D4 contain ownership documentation because their initial implementations are deterministic. | Active for model-backed paths; D3/D4 currently make no model call. |
| [`services/`](services/) | Application-facing Sensor Analytics, LLM, ADX, and operational-storage boundaries. | Active runtime. |
| [`adapters/`](adapters/) | Lazy concrete adapters over the established provider and storage modules. | Active runtime. |
| [`schemas/`](schemas/) | Dependency-light type contracts for domain plans/results, tool results, and transport-facing results. | Active runtime. |
| [`resources/`](resources/) | Versioned non-prompt resources, currently the D2 coaching catalogue and source registry. | Active for D2; additional domain resources are added only with implemented consumers. |
| [`i18n/`](i18n/) | Deterministic English user-visible text and compatibility locale keys. | Active runtime; broader localization is planned. |
| [`proactive/`](proactive/) | Initial threshold/STEPS alerts and scheduled daily reports, outside the reactive graph. | Active for the documented threshold, STEPS, and daily-report flows. |

### Node and domain packages

| Path | Meaning | Implementation status |
|---|---|---|
| [`nodes/common/`](nodes/common/) | Prepares shared context, aggregates domain results, generates the final answer, and persists trace/context. | Active runtime. |
| [`nodes/planning/`](nodes/planning/) | Classifies intent, selects D1-D4 work, builds the execution plan, and validates dependencies/stages. | Active runtime. |
| [`nodes/orchestration/`](nodes/orchestration/) | Runs the generic domain registry with bounded parallel/sequential DAG execution and dependency handling. | Active runtime. |
| [`nodes/d1_sensor_data/`](nodes/d1_sensor_data/) | Plans sensor questions, generates and guards KQL, performs bounded repair, executes ADX queries, and produces grounded evidence. | Implemented and active. |
| [`nodes/d2_coaching_strategy/`](nodes/d2_coaching_strategy/) | Prepares context and produces a validated Advisor coaching strategy for final-response delivery. | Implemented and active. |
| [`nodes/d3_health_knowledge/`](nodes/d3_health_knowledge/) | Retrieves from the approved, versioned local health corpus, validates provenance, applies safety precedence, and abstains without evidence. | Implemented initial corpus/index; external retrieval expansion is planned. |
| [`nodes/d4_multiturn_context/`](nodes/d4_multiturn_context/) | Builds candidates from the current session, resolves reply-to/latest referents, validates source-turn provenance, and asks for clarification when unresolved. | Implemented for current-session context; semantic model fallback and long-term memory are not enabled. |
| [`nodes/operational/`](nodes/operational/) | Handles onboarding, capability questions, and alert/daily-summary configuration outside normal domain evidence work. | Active runtime. |

### Important top-level files

| Module | Responsibility and main API |
|---|---|
| `entrypoint.py` | Converts a transport request into graph state and returns `ChatbotResponse` through `handle_user_message(...)`. |
| `config.py` | Defines `ChatbotSettings` and the cached `get_settings()` environment boundary. |
| `adx_client.py` | Owns the established ADX query client singleton and deployed/local authentication selection. New nodes use the ADX service boundary. |
| `llm_client.py` | Owns the established Azure OpenAI/OpenAI completion and Responses API calls. New nodes use the LLM service boundary. |
| `operational_storage.py` | Defines chatbot profiles, conversation state, availability, thresholds, trigger state, and `OperationalTableRepository`. |
| `availability_refresh.py` | Queries recent normalized/curated coverage, records optional vendor-only coverage separately, and persists the canonical per-user metric-availability cache. |
| `alert_thresholds.py` | Parses, labels, recommends, and formats supported threshold and daily-summary settings. |
| `proactive/threshold_monitor.py` | Evaluates threshold rules and builds/sends scheduled daily summaries. |
| `proactive/system_steps_alert.py` | Evaluates proactive STEPS events, retry/fallback policy, ADX audit records, and Telegram notification delivery. |
| `timezone_onboarding.py` | Builds the site/timezone menu, resolves user input, and persists a validated IANA timezone. |
| `nlu.py` | Deterministic-first parsers with bounded LLM fallback for onboarding and alert values. |
| `episode_service.py` | Implements `/new`, `/resume`, and optional proactive interruption for chatbot conversation state. |
| `response_chain.py` | Validates whether a stored Responses API chain remains fresh enough to continue. |
| `__init__.py` | Package marker; intentionally defines no eager runtime exports. |

## Request lifecycle

```text
transport receives a registered user's message
  -> prepare_context
     -> restore profile, availability, episode, and conversation context
  -> plan_domain_execution
     -> jointly classify intent and validate generic D1-D4 dependencies/stages
  -> execute_domains
     -> selected D1: plan -> generate -> guard -> [repair -> guard] -> execute -> result
     -> selected D2: prepare -> plan -> validate -> result
     -> selected D3: approved-corpus retrieval, grounding and safety result
     -> selected D4: within-session source-turn resolution result
     -> block any runner whose required dependency did not succeed
  -> aggregate_domain_results
  -> generate_final_response
     -> use all domain statuses, successful D1 facts, and any D2 strategy
  -> persist_trace_and_context
     -> persist conversation, episode, and latest domain trace context
  -> return ChatbotResponse to the transport
```

The canonical topology is documented in [`graph/README.md`](graph/README.md),
and the canonical graph-node imports are documented in
[`nodes/README.md`](nodes/README.md).

### Simple and computed data plans

Simple values, series, and latest measurements retain the established version-1
plan path. Questions that require an explicit aggregate, workout filter,
numeric condition, entity count, or first/last/top/bottom selection use the
physical-schema-free version-2 computation algebra in
[`policies/query_algebra.py`](policies/query_algebra.py). After strict
validation, [`policies/query_algebra_kql.py`](policies/query_algebra_kql.py)
compiles the plan into user- and time-bounded KQL, including selection and
condition evidence for the response. Unsupported combinations stop before ADX;
they are never degraded into a broader legacy query.

## Health-data resource selection

The planner selects logical evidence resources rather than physical ADX names:
`activity.daily`, `activity.hourly`, `activity.interval`, `health.samples`,
`sleep.nightly`, and `workout.sessions`. The default
`sensei_sensor_contract_v1` profile binds them to the six corresponding
`Sensor*` tabular functions. Production and evaluation expose the same names,
signatures, columns, and units; `ADX_DATABASE` selects the backend. The LLM
prompt and metric-routing policy therefore contain neither production backing
objects nor dataset-specific raw columns.

Production's `HealthDataFlat`, activity materialized views, and sleep/workout
normalization remain behind those ADX functions. They are implementation
details of the production adapter, not conversational D1 query sources.

Activity materialized views apply a strict native-source policy to avoid
double-counting mirrored providers. Sleep is source-agnostic and resolves
sessions and overlapping sources inside `PhoneSleepNights()`. MAD resources are
reserved for the supported personal-baseline and anomaly paths.

For sleep, `PhoneSleepSessionDedupMV` is an input-level session deduplication
view, not an interchangeable nightly answer source. `PhoneSleepNights()` joins
session and stage evidence, handles missing session windows, removes overlap,
attributes a night to its wake date, resolves multiple sources, and identifies
main sleep versus naps. Sleep duration uses `AsleepMin`; the enclosing session
span is separately exposed as `TimeInBedMin`. If a session has no asleep-stage
evidence, `AsleepMin` remains null rather than treating the entire session as
sleep or reporting a misleading zero.

Direct `Withings*` bindings are retained only as an opt-in legacy compatibility
surface outside the canonical six-function contract. Keep
`CHATBOT_ENABLE_OPTIONAL_WITHINGS_TABLES=false` for shared-contract D1 runs.
They are never a fallback for empty Sensor results, and raw activity rows in
`HealthDataFlat` are diagnostic evidence rather than a replacement aggregate.

## Dependency direction

```text
functions / other transports
  -> chatbot.entrypoint or chatbot.services.sensor_analytics
  -> chatbot.graph
  -> chatbot.nodes
  -> chatbot.policies + chatbot.prompts + chatbot.services
  -> chatbot.adapters
  -> chatbot.llm_client / chatbot.adx_client / chatbot.operational_storage
```

Policies must remain deterministic and provider-independent. Nodes may assemble
state and prompts but should call external systems through services. Adapters
may import established concrete clients lazily; the reverse dependency is not
allowed.

## Prompts and deterministic text

Researchers who modify conversational behavior should begin with
[`prompts/README.md`](prompts/README.md). It identifies every prompt resource,
its runtime consumer, placeholders, and expected output contract.

- Keep prompt instructions, few-shot examples, and runtime responses in English.
- Put fixed user-visible text in `i18n/catalog.py`, not in graph nodes.
- Preserve runtime placeholders and `[SYSTEM]`/`[USER]` role markers unless the
  consuming loader call, prompt inventory, and package validation are changed
  in the same revision.
- A KQL prompt cannot bypass `policies.kql_safety` or the graph's guard route.

## Storage and external services

The chatbot reads normalized and curated health data from ADX and stores
operational state in Azure Table Storage. It does not ingest raw mobile
payloads. Optional direct-Withings ingestion remains a separate legacy
integration and is not the default conversational query path.

Use `AzureWebJobsStorage` for chatbot operational tables unless an existing
configuration explicitly states otherwise. The registered-user `Users` table
is an established integration contract accessed through `functions.shared` and
may use `AzureWebJobsAppStorage`.

## Contributor workflow

1. Put only the six top-level nodes and their linear edges in
   `graph/workflow.py`.
2. Put domain nodes, edges, and conditional transitions in the owning domain's
   `nodes/d*/workflow.py`; keep planning and generic execution in their
   respective `nodes/planning/` and `nodes/orchestration/` packages.
3. Put deterministic reusable behavior in `policies/`, and provider access in
   `services/` plus `adapters/`.
4. Update the prompt inventory when adding, renaming, or removing a prompt.
5. Import each node from its single canonical file; do not add compatibility
   facades or duplicate grouped implementations.
6. Record repeatable smoke, package, and protected runtime evidence for every
   behavior or contract change.

Run the no-network smoke check from the Function App directory:

```bash
python local_test.py smoke
```

When external behavior changed, use a protected non-production identity to
exercise the actual graph and preview the system-initiated message path:

```bash
python local_test.py ask --user-id "<test-app-user-id>" --message "Show my steps for today"
python local_test.py system-preview
```

The `ask` command may query ADX and update operational conversation state. CI
package validation provides the required automated syntax, import, graph,
Azure Function registration, and prompt-resource gate before deployment.

See [`../docs/refactoring/README.md`](../docs/refactoring/README.md) for the
historical-to-current migration record.
