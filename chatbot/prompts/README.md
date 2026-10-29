# Prompt runtime and ownership guide

Each UTF-8 `.txt` prompt resource lives under
the package that owns the model-calling task. There are no duplicate legacy
copies and no prompt-policy Python modules.

D3 and D4 currently make no model call. Their implemented deterministic
subgraphs are documented under `nodes/`, and their prompt ownership directories
contain documentation only rather than unused prompts. The deterministic
System STEPS alert also has no prompt.

## Physical layout

```text
prompts/
|-- common/
|   `-- generate_final_response/
|       |-- chat.txt
|       |-- coaching.txt
|       |-- sensor_data.txt
|       `-- unsupported_metric.txt
|-- planning/
|   `-- plan_domain_execution.txt
|-- d1_sensor_data/
|   |-- plan_sensor_query.txt
|   |-- review_measurement_scope.txt
|   |-- generate_sensor_query.txt
|   |-- repair_sensor_query.txt
|   |-- recheck_daily_with_flat.txt
|   `-- adx_schema_context.txt
|-- d2_coaching_strategy/
|   |-- plan_coaching_strategy.txt
|   `-- advisor_policy/
|       |-- 01_nhs_role_and_personalised_care.txt
|       |-- 02_comb_framework.txt
|       |-- 03_bct_framework.txt
|       |-- 04_mi_framework.txt
|       |-- 05_sdt_framework.txt
|       `-- 06_interaction_depth_and_scope.txt
|-- d3_health_knowledge/
|   `-- README.md
|-- d4_multiturn_context/
|   `-- README.md
|-- operational/
|   |-- alert_config_responder.txt
|   |-- capabilities_responder.txt
|   `-- guided_input/
|       |-- age.txt
|       |-- auto_manual.txt
|       |-- duration.txt
|       |-- gender.txt
|       |-- metric.txt
|       |-- opt_in.txt
|       |-- threshold_bound.txt
|       `-- timezone.txt
|-- proactive/
|   `-- threshold_monitor/
|       |-- rewrite_threshold_alert.txt
|       `-- daily_report_coach.txt
|-- loader.py
|-- __init__.py
`-- README.md
```

Prompt directory ownership does not imply that a model call exists. D3 and D4
currently have model-free retrieval/resolution subgraphs, so they contain
documentation only. Add prompts only if a reviewed model-backed step is added.

## Ownership and activation

| Prompt owner | Runtime consumer | Activation |
|---|---|---|
| `planning/plan_domain_execution.txt` | `nodes/planning/plan_domain_execution.py` | Nontrivial intent and D1-D4 execution planning; exact greetings and thanks use a deterministic fast path. |
| `d1_sensor_data/plan_sensor_query.txt` | `nodes/d1_sensor_data/plan_sensor_query.py` | D1 sensor-plan generation. |
| `d1_sensor_data/review_measurement_scope.txt` | `nodes/d1_sensor_data/plan_sensor_query.py` | One independent LLM scope review when a workout plan has conditions across measurement streams and bounded schema discovery succeeds. No dataset-specific routing or answer labels. |
| `d1_sensor_data/generate_sensor_query.txt` | `nodes/d1_sensor_data/generate_sensor_query.py` | Default LLM KQL generation using the common Sensor schema and sanitized plan; deterministic generation remains an explicit comparison option. |
| `d1_sensor_data/repair_sensor_query.txt` | `nodes/d1_sensor_data/repair_sensor_query.py` | Bounded repair after the guard rejects an LLM candidate; every repaired candidate returns to the guard. |
| `d1_sensor_data/recheck_daily_with_flat.txt` | `nodes/d1_sensor_data/daily_flat_recheck.py` | Model-generated re-evaluation only after bounded coverage evidence finds uncovered daily metric/date keys. |
| `d1_sensor_data/adx_schema_context.txt` | D1 generation and repair | Shared D1 schema context; it is not a standalone model call. |
| `d2_coaching_strategy/plan_coaching_strategy.txt` and `advisor_policy/*.txt` | `nodes/d2_coaching_strategy/plan_coaching_strategy.py` | One deterministic policy bundle for one typed Advisor strategy call. It does not produce a final answer or evaluation score. |
| `common/generate_final_response/*.txt` | `nodes/common/generate_final_response.py` | General chat, grounded sensor response, optional coaching delivery, or unsupported-metric response. |
| `operational/alert_config_responder.txt` | `nodes/operational/alert_config_responder.py` | Natural-language threshold and scheduled-summary configuration. |
| `operational/capabilities_responder.txt` | `nodes/operational/capabilities_responder.py` | Profile, timezone, inventory, and capability classification. |
| `operational/guided_input/*.txt` | `chatbot.nlu` and `chatbot.timezone_onboarding` | Model fallback after deterministic guided-input parsing does not resolve the value. |
| `proactive/threshold_monitor/rewrite_threshold_alert.txt` | `chatbot.proactive.threshold_monitor` | Optional wording pass after deterministic threshold detection and fact construction. |
| `proactive/threshold_monitor/daily_report_coach.txt` | `chatbot.proactive.threshold_monitor` | Daily narrative only when collected report data exists. |

## Reactive request flow

```text
prepare_context
  -> planning/plan_domain_execution.txt
  -> execute_domains
       D1 -> d1_sensor_data/plan_sensor_query.txt
             -> optional bounded schema + d1_sensor_data/review_measurement_scope.txt
             -> observed schema metadata -> d1_sensor_data/generate_sensor_query.txt (default)
             -> guard_sensor_query
             -> optional d1_sensor_data/repair_sensor_query.txt -> guard_sensor_query
             -> execute_sensor_query -> produce_sensor_result
       D2 -> d2_coaching_strategy/plan_coaching_strategy.txt
             + d2_coaching_strategy/advisor_policy/*.txt
       D3 -> model-free approved-corpus retrieval and safety result
       D4 -> model-free within-session context resolution result
  -> common/generate_final_response/{chat,sensor_data,coaching,unsupported_metric}.txt
     as selected by validated state
```

Operational routes can end in deterministic onboarding, capability, or
configuration responses. Fixed English wording comes from
`chatbot/i18n/catalog.py`. Error, missing-data, and onboarding branches can
bypass final model generation and keep their deterministic responses.

System-initiated threshold and daily-report flows run outside this graph. A
reply to a proactive message enters the same `prepare_context` and planning
flow as any other user message.

## Resource inventory and input contracts

The values below are Python `str.format` variables. Policy-only resources have
no template variables because their owning nodes attach typed schemas and
allowlisted runtime context as separate messages.

| Resource | Input variables |
|---|---|
| `planning/plan_domain_execution.txt` | none |
| `d1_sensor_data/adx_schema_context.txt` | `user_id` |
| `d1_sensor_data/plan_sensor_query.txt` | `available_metrics`, `history`, `last_assistant_message`, `last_plan`, `memory_context`, `question`, `today_date`, `today_weekday`, `current_local_time`, `user_available_metrics`, `validation_feedback` |
| `d1_sensor_data/generate_sensor_query.txt` | `health_samples_resource`, `workout_sessions_resource`, `now`, `plan_json`, `question`, `query_feedback`, `schema`, `schema_observations`, `user_id`, `start_local_bound`, `end_local_bound` |
| `d1_sensor_data/repair_sensor_query.txt` | `error_hint`, `kql`, `plan_json`, `question`, `schema`, `user_id` |
| `d1_sensor_data/recheck_daily_with_flat.txt` | `schema`, `user_id`, `question`, `plan_json`, `start_local_bound`, `end_local_bound`, `primary_kql`, `primary_rows`, `gap_evidence`, `activity_daily_resource`, `health_samples_resource` |
| `d2_coaching_strategy/plan_coaching_strategy.txt` | none |
| `d2_coaching_strategy/advisor_policy/*.txt` | none |
| `common/generate_final_response/chat.txt` | `answer_language`, `history`, `memory_context`, `question` |
| `common/generate_final_response/coaching.txt` | `answer_language`, `coaching_strategy`, `coverage_context`, `data_json`, `history`, `memory_context`, `question` |
| `common/generate_final_response/sensor_data.txt` | `answer_language`, `assumptions`, `case_type`, `computation`, `coverage_context`, `data_json`, `memory_context`, `metrics`, `previous_assistant`, `question`, `time_range`, `today_date` |
| `common/generate_final_response/unsupported_metric.txt` | `answer_language`, `question` |
| `operational/alert_config_responder.txt` | `history`, `pending_spec`, `question` |
| `operational/capabilities_responder.txt` | `question` |
| `operational/guided_input/age.txt` | `text` |
| `operational/guided_input/auto_manual.txt` | `text` |
| `operational/guided_input/duration.txt` | `text` |
| `operational/guided_input/gender.txt` | `text` |
| `operational/guided_input/metric.txt` | `codes`, `text` |
| `operational/guided_input/opt_in.txt` | `text` |
| `operational/guided_input/threshold_bound.txt` | `text` |
| `operational/guided_input/timezone.txt` | `text` |
| `proactive/threshold_monitor/rewrite_threshold_alert.txt` | `deterministic`, `facts` |
| `proactive/threshold_monitor/daily_report_coach.txt` | `data_lines`, `part_of_day` |

## Loader contract

`loader.py` provides:

- `load_prompt(path)` for raw UTF-8 text;
- `load_chat_prompt(path)` for the established single-message
  `ChatPromptTemplate` behavior;
- `split_role_prompt(text)` for one `[SYSTEM]` and one `[USER]` section; and
- `load_role_prompt(path)` to load and split a role-bundled resource.

Paths must be relative `.txt` paths below this directory. Absolute paths, path
traversal, missing files, empty resources, and malformed role sections fail
explicitly. Successful reads are cached for the process lifetime, so restart
the local Functions host after changing a prompt.

The eight guided-input resources and `daily_report_coach.txt` retain one
`[SYSTEM]` and one `[USER]` section. Preserve those role markers and all input
variables unless the caller and validation are updated in the same change.

## D2 Advisor source boundary

The D2 task shell and six policy fragments are loaded in the fixed order in
`nodes/d2_coaching_strategy/plan_coaching_strategy.py`. Source IDs, versions,
URLs, fragment ownership, use, and redistribution notes live in
`resources/coaching/d2_source_registry.json`. The BCT catalogue in the same
resource package records its taxonomy source and contains only techniques the
runtime may consider; it contains no scenario-to-answer mapping.

These resources are the production Advisor projection of their source material:
they provide planning definitions and delivery rules. Any future review or
scoring projection must live outside the production chatbot package and may not
be imported by the Advisor runtime. The bundle hash and source IDs in D2 result
provenance identify the exact policy projection used for a strategy call.

## Sensor evidence and KQL boundary

D1 planning uses logical evidence IDs. The active trusted profile binds those
IDs to the deployed `Sensor*` tabular functions. The optional KQL generation
and repair prompts receive only this D1 schema contract and active binding
values. They do not authorize execution.

Every candidate passes `policies/kql_safety.py`, which enforces read-only syntax,
approved resources, exact user scope, bounded time ranges, and result bounds.
An LLM repair returns to `guard_sensor_query`; it cannot route directly to ADX.
Evaluation data, gold answers, backing tables, and raw health payloads must not
enter runtime prompts.

## Modification checklist

1. Edit the `.txt` file owned by the model-calling node.
2. Preserve its task boundary, variables, JSON contract, and role markers.
3. Update the owning caller, this inventory, and tests together if a contract
   changes.
4. Keep deterministic localized application text in `chatbot/i18n/catalog.py`.
5. Run `python local_test.py` and the offline test suites before review.

Prompt text cannot change graph routing safeguards, KQL authorization, Azure
bindings, or storage behavior.
