# Instruction, Onboarding, and Prompt Modification Guide

This guide identifies the authoritative files for onboarding questions, prompt resources, capability answers, and deterministic user-facing text. It is intended for researchers who need to change language behavior without accidentally changing Storage contracts, graph routing, or Azure Function trigger behavior.

## 1. Keep the behavior boundaries clear

The application has three different kinds of language assets:

| Kind | Source of truth | Appropriate changes |
|---|---|---|
| Deterministic onboarding flow | `functions/interview.py` | Question text, state transitions, answer validation, alert setup, and daily-summary setup. |
| Model instructions and examples | `chatbot/prompts/` | Intent classification, planning, KQL generation/repair, response generation, alert extraction, coaching, and ambiguous-input NLU. |
| Deterministic English replies | `chatbot/i18n/catalog.py` | Literal application replies and general capability text. |

Do not move deterministic state transitions into a prompt. A model may interpret an answer, but `functions/interview.py` remains responsible for deciding what is stored and which state follows. Conversely, do not duplicate a long model instruction inside Python when it belongs in a prompt resource.

## 2. Current onboarding state machine

The canonical onboarding implementation is `functions/interview.py`. Telegram commands enter it through `functions/telegram_bp.py`:

- `/start` starts or resumes incomplete onboarding and does not erase completed settings.
- `/update_info` starts the interview from the first state and overwrites fields as new answers are accepted.
- `/cancel` stops an incomplete interview while retaining answers already stored.
- `split_message_bubbles()` lets one transition return a confirmation and the next question as separate Telegram messages.

### Why the source contains 16 state keys and 15 handlers

`INTERVIEW_STEPS` contains 16 persisted state keys. The first state, `name`, is the only simple field handled by the generic validation/store/advance path. The remaining 15 states are handled explicitly by functions in the `special` dispatcher inside `process_interview_answer()`.

This distinction matters when editing the flow: the list is not a flat list of 16 mandatory questions. Confirmation states are conditional, and the alert states repeat once for each selected metric.

| Order | State key | Handler type | Responsibility |
|---:|---|---|---|
| 1 | `name` | Generic field path | Validates and stores `Users.Name`. |
| 2 | `age` | `_process_age_step` | Parses age; exact input stores immediately and inferred input requests confirmation. |
| 3 | `age_confirm` | `_process_age_confirm_step` | Conditionally confirms an inferred age. |
| 4 | `gender` | `_process_gender_step` | Resolves canonical gender values and may request confirmation. |
| 5 | `gender_confirm` | `_process_gender_confirm_step` | Conditionally confirms an inferred gender. |
| 6 | `timezone` | `_process_timezone_step` | Accepts a numbered site, city/region text, or an IANA timezone. |
| 7 | `timezone_confirm` | `_process_timezone_confirm_step` | Conditionally confirms an inferred timezone. |
| 8 | `alert_optin` | `_process_alert_optin_step` | Asks whether to configure threshold alerts; skipped when no supported vital is available. |
| 9 | `alert_select` | `_process_alert_select_step` | Selects one or more available metrics. |
| 10 | `alert_mode` | `_process_alert_mode_step` | Selects a cited automatic range or manual entry; metrics without a reference range go directly to manual entry. |
| 11 | `alert_lower` | `_process_alert_lower_step` | Collects the optional lower threshold. |
| 12 | `alert_upper` | `_process_alert_upper_step` | Collects the optional upper threshold. |
| 13 | `alert_duration` | `_process_alert_duration_step` | Collects the sustained-duration requirement. |
| 14 | `alert_bound_confirm` | `_process_alert_bound_confirm_step` | Conditionally confirms an inferred bound or duration. |
| 15 | `summary_optin` | `_process_summary_optin_step` | Asks whether to enable the daily summary. |
| 16 | `summary_time` | `_process_summary_time_step` | Parses local delivery time, rounds to a five-minute timer boundary, saves it, and completes onboarding. |

The normal high-level path is:

```text
name -> age -> [age_confirm] -> gender -> [gender_confirm]
     -> timezone -> [timezone_confirm]
     -> alert_optin
        -> [alert_select -> alert_mode/manual bounds/duration
            -> repeat for each selected metric]
     -> summary_optin -> [summary_time] -> done
```

Important conditional behavior in `_enter_step()` must remain intact:

- No currently available supported vital skips the alert block.
- Declining alerts jumps to summary opt-in.
- A metric without a medical reference skips automatic mode and uses manual entry.
- Completing one metric advances to the next metric in `OnboardingAlertQueue`.
- Exhausting the metric queue advances to summary opt-in.
- Declining the summary completes onboarding immediately.
- Resuming at a confirmation state safely re-asks its source question if scratch data is unavailable.

## 3. Editing onboarding text

### Simple field question

The `name` question is stored in `STEP_QUESTIONS`. `_question_for_step()` is the common entry point for simple questions and the dynamic timezone menu.

```python
STEP_QUESTIONS = {
    "name": "What's your name? (used for personalised replies)\n[e.g. Alex]",
    "age": "How old are you?\n[e.g. 30]",
    "gender": "Your gender? (male / female / other / prefer not to say)\n[e.g. male]",
}
```

Although `age` and `gender` have entries here, their answers are processed by explicit handlers because inferred values may require confirmation.

### Dynamic onboarding messages

| Message or question | Function in `functions/interview.py` |
|---|---|
| Getting-started completion text | `_completion_message()` |
| Saved-field confirmation | `_field_confirmation()` |
| Age confirmation | `_age_confirm_question()` |
| Gender confirmation | `_gender_confirm_question()` |
| Timezone menu | `chatbot/timezone_onboarding.py` -> `build_menu_message()` |
| Timezone confirmation | `_tz_confirm_question()` |
| Alert opt-in | `_alert_optin_question()` |
| Metric selection | `_alert_select_question()` |
| Automatic/manual mode | `_alert_mode_question()` |
| Lower/upper/duration input | `_alert_lower_question()`, `_alert_upper_question()`, `_alert_duration_question()` |
| Inferred bound confirmation | `_bound_confirm_question()` |
| Daily-summary opt-in/time | `_summary_optin_question()`, `_summary_time_question()` |
| Initial greeting | `begin_interview()` |
| Profile-update header | `begin_interview_update()` |
| Cancellation reply | `cancel_interview()` |

Keep one decision per question. Preserve `MESSAGE_BREAK` when a confirmation and the next question should be sent as separate Telegram bubbles.

### Changing validation or accepted answers

Input interpretation is deliberately layered:

1. Exact deterministic parsing
2. Fuzzy or number-word parsing
3. A small LLM extraction prompt for genuinely ambiguous text
4. Explicit yes/no confirmation before an inferred value is persisted

The orchestration and persistence rules are in `functions/interview.py`.
Reusable parsing is in `chatbot/nlu.py` and `chatbot/alert_thresholds.py`.
Fallback model instructions for ambiguous guided input are in
`chatbot/prompts/operational/guided_input/`.

When changing accepted input, update the parser and its documented validation cases. Do not bypass confirmation merely because a model returned a syntactically valid value.

## 4. Adding, removing, or reordering an onboarding state

Changing only `INTERVIEW_STEPS` is not sufficient for a state with dynamic content or side effects.

For a simple persisted field, review all of the following:

1. Add the state key to `INTERVIEW_STEPS` at the intended position.
2. Add its question to `STEP_QUESTIONS`.
3. Add its `Users` table property to `STEP_FIELD_MAP`.
4. Add validation to `_validate_answer()`.
5. Verify that advancing into the next state renders the correct question.
6. Update `begin_interview_update()` if the field should appear in the current-profile summary.
7. Exercise state transitions, invalid answers, resume behavior, and persistence
   with the local smoke tool and a protected non-production account.

For a dynamic or side-effectful state, also:

1. Add the key to `_SPECIAL_STEPS`.
2. Implement a `_process_<state>_step()` handler.
3. Register the handler in the `special` dispatcher in `process_interview_answer()`.
4. Add entry rendering and conditional jumps to `_enter_step()`.
5. Define and clear any scratch fields used across turns.
6. Verify `/start`, `/update_info`, `/cancel`, and resume behavior.
7. Document any new Table property or operational resource.

Before removing or reordering a state, account for users whose `Users.InterviewStep` already contains that key. A production migration or compatibility redirect may be required; otherwise a deployed user can resume into an invalid state.

## 5. Prompt resource architecture

All model task text is packaged in UTF-8 `.txt` files below
`chatbot/prompts/`. Typed output contracts live in `chatbot/schemas/`, and each
owning node assembles its prompt text, schema, and allowlisted runtime context.
The detailed file-by-file inventory is maintained in
[chatbot/prompts/README.md](chatbot/prompts/README.md).

| Directory | Runtime responsibility | Main consumers |
|---|---|---|
| `planning/` | Combined intent and domain execution planning. | `chatbot/nodes/planning/plan_domain_execution.py` |
| `d1_sensor_data/` | Sensor plan, KQL generation/repair and their ADX schema context. | `chatbot/nodes/d1_sensor_data/` |
| `d2_coaching_strategy/` | Advisor strategy-planning task text. | `chatbot/nodes/d2_coaching_strategy/plan_coaching_strategy.py` |
| `common/generate_final_response/` | Final chat, sensor, coaching and unsupported answers. | `chatbot/nodes/common/generate_final_response.py` |
| `operational/` | Capabilities, alert configuration and guided-input fallback tasks. | Operational nodes, `chatbot/nlu.py`, `chatbot/timezone_onboarding.py` |
| `proactive/threshold_monitor/` | Optional threshold-alert rewriting and daily-report narrative. | `chatbot/proactive/threshold_monitor.py` |
| No D3/D4 prompt directory | D3/D4 make no model call; their explicit placeholders are documented with the node workflows. | `chatbot/nodes/d3_health_knowledge/`, `chatbot/nodes/d4_multiturn_context/` |

### Prompt loader contract

`chatbot/prompts/loader.py` provides four APIs:

- `load_prompt(path)` returns raw UTF-8 text.
- `load_chat_prompt(path)` returns the existing single-user-message `ChatPromptTemplate` behavior.
- `split_role_prompt(text)` separates one `[SYSTEM]`/`[USER]` task resource.
- `load_role_prompt(path)` loads and separates a role-bundled task resource.

Prompt paths must be relative `.txt` paths below `chatbot/prompts/`. Absolute
paths, path traversal, missing files, empty resources, and malformed role
sections fail explicitly.

`{question}`, `{history}`, and similar braces are runtime input variables. Do
not rename them without updating the corresponding caller, prompt inventory,
and package validation. Do not change `load_chat_prompt()` to a system-message
template as a cleanup; that would change the current model interaction contract.

Loaded resources are cached. Restart `func start` or redeploy after changing prompt text.

### Language convention

Runtime prompt instructions and few-shot examples are written in English.
The runtime produces English responses. The `{answer_language}` value is set
to English, and deterministic application messages belong in
`chatbot/i18n/catalog.py`. Legacy locale arguments remain accepted at module
boundaries, but they resolve to the same English text.

## 6. Changing routing and capability behavior

The combined intent and domain-execution prompt is
`chatbot/prompts/planning/plan_domain_execution.txt`.

The planner must continue returning one supported intent: `data`, `chat`,
`unsupported`, `clarification`, `capabilities`, or `alert_config`.
Only exact greetings and thanks have deterministic fast paths in
`chatbot/nodes/planning/plan_domain_execution.py`. Contextual replies are
classified with the available history first; after a `data` decision, an
explicitly anaphoric or period-only turn may reuse its prior sensor plan. Never
use message length alone as a routing rule.

Capability questions use
`chatbot/prompts/operational/capabilities_responder.txt`.

It selects `timezone`, `profile`, `inventory`, or `general`. The inventory is built from the user's refreshed metric-availability cache, so it must not be replaced with a static list.

The general capability text is `CAPABILITIES_GENERAL` in `chatbot/i18n/catalog.py`. Keep it aligned with:

- `chatbot/policies/domain_catalog.py` for canonical metrics and data sources;
- `chatbot/prompts/d1_sensor_data/adx_schema_context.txt` for the schema visible to query generation and repair; and
- `chatbot/policies/kql_safety.py` for tables the executor is permitted to query.

The capability node reads the selected language directly from
`CAPABILITIES_GENERAL`; do not create a second editable copy in the node.

## 7. Changing data-question prompts safely

A data question crosses several prompt and deterministic-policy boundaries:

```text
intent routing
  -> structured data plan
  -> deterministic KQL when supported, otherwise optional fallback generation
  -> deterministic KQL guard
  -> bounded repair and guard retry
  -> ADX execution
  -> grounded user-facing response
```

When adding a new metric or source, changing only a prompt is insufficient. Review the domain catalog, metric aliases, availability policy, plan sanitization, KQL generation, KQL safety allowlist, schema context, response formatting, and validation evidence. The guard must remain authoritative even when the generation or repair prompt requests a query.

Keep response prompts explicit about missing data: no records are not the same as a zero measurement. Preserve source separation when both Withings and smartphone measurements are shown.

Keep prompt examples representative rather than exhaustive. Use a small set of
boundary examples to show decisions that prose alone may not make clear, and
put wording variants in regression tests or evaluation datasets. Safety,
authorization, user scoping, table allowlists, and deterministic metadata must
remain enforced in Python rather than delegated to prompt wording.

## 8. Validation and review checklist

Run the no-network smoke check after editing onboarding, prompts, deterministic
messages, graph wiring, or transport contracts:

```bash
python local_test.py smoke
```

The smoke command imports the Azure Function app, compiles the canonical graph,
checks its node inventory, and loads every packaged prompt. The repository CI
package-validation step must also pass before deployment.

When a change depends on live ADX, Storage, or model behavior, use a protected
non-production identity and exercise the real user path explicitly:

```bash
python local_test.py ask --user-id "<test-app-user-id>" --message "Show my steps for today"
python local_test.py system-preview
```

The `ask` command uses configured external services and may update conversation
state. Never run it with production credentials or a production participant
unless that operation has been separately approved.

For an onboarding change, manually verify with a non-production Telegram user:

1. Start a new or incomplete profile with `/start`.
2. Exercise exact and ambiguous age, gender, and timezone answers.
3. Resume from an interrupted confirmation state.
4. Exercise alert opt-out, automatic threshold, manual threshold, and multiple-metric repetition.
5. Exercise daily-summary opt-out and time parsing.
6. Run `/update_info` and confirm existing settings are not erased before replacement.
7. Run `/cancel` and confirm accepted partial answers remain.

For a prompt change, record the prompt files changed, the expected input/output
contract, representative English cases, and the validation evidence.
Prompt quality evaluation does not replace graph compilation, safety review,
Storage verification, or a protected end-to-end check when external behavior
changed.
