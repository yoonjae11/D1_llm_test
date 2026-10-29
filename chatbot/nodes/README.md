# Domain node ownership

The top-level graph contains six orchestration stages. Actual domain subgraphs
and node implementations belong to the packages below.

| Package | Ownership | Model calls |
|---|---|---|
| `common/` | Prepare context, aggregate results, generate final response, persist trace/context and shared support | Final response as needed; existing context behavior retained |
| `planning/` | One combined domain/intent plan, deterministic greeting fast path and DAG validation | One planner call except deterministic greetings |
| `orchestration/` | `DOMAIN_RUNNERS`, dependency passing, bounded parallel scheduling | None |
| `d1_sensor_data/` | Plan, generate, guard, repair, execute, produce sensor evidence | Established planner/generation/repair policy only |
| `d2_coaching_strategy/` | Prepare, plan Advisor strategy, validate, wrap result | At most one strategy call |
| `d3_health_knowledge/` | Approved-corpus retrieval, evidence validation, safety and `run_d3` | None in the initial deterministic implementation |
| `d4_multiturn_context/` | Current-session candidate resolution, provenance validation and `run_d4` | None in the initial deterministic implementation |
| `operational/` | Onboarding, capabilities, alert/daily-summary configuration responders | Established behavior |

## Domain interfaces

Each `workflow.py` exports `build_dN_graph()` and `run_dN(state)`.
Every runner returns `DomainRunOutput` with a common `DomainExecutionResult`
and explicit `state_updates`. D1 returns its existing query state fields;
D2-D4 return typed domain results without parent state updates. Domains do not return final
messages. Common aggregation supplies results and status to the final response.

The executor passes only declared results through `dependency_results`.
Required non-success dependencies block a runner; explicitly optional ones
remain visible to it with their real statuses. Independent same-stage calls
receive isolated snapshots and request contextvars. Conflicting updates fail
closed. A plan has at most three stages, with one or two preferred.

## Physical layout

The `nodes/` root contains only this guide and the package marker. Executable
nodes live in the package that owns their behavior:

```text
nodes/
|-- common/
|   |-- prepare_context.py
|   |-- aggregate_domain_results.py
|   |-- generate_final_response.py
|   |-- persist_trace_and_context.py
|   `-- support.py
|-- planning/
|   |-- plan_domain_execution.py
|   `-- validate_domain_execution_plan.py
|-- orchestration/
|   `-- execute_domains.py
|-- d1_sensor_data/
|   |-- workflow.py
|   |-- plan_sensor_query.py
|   |-- generate_sensor_query.py
|   |-- guard_sensor_query.py
|   |-- repair_sensor_query.py
|   |-- execute_sensor_query.py
|   `-- produce_sensor_result.py
|-- d2_coaching_strategy/
|   |-- workflow.py
|   |-- prepare_coaching_context.py
|   |-- plan_coaching_strategy.py
|   |-- validate_coaching_plan.py
|   `-- produce_coaching_result.py
|-- d3_health_knowledge/
|   |-- workflow.py
|   `-- pipeline.py
|-- d4_multiturn_context/
|   |-- workflow.py
|   `-- pipeline.py
`-- operational/
    |-- onboarding_responder.py
    |-- capabilities_responder.py
    `-- alert_config_responder.py
```

Import every node from its canonical file. The top-level six-node graph and its
linear edges belong in `graph/workflow.py`; each domain's nodes, edges, and
conditional transitions belong in that domain's `workflow.py`. Keep helpers in
the consuming node file unless they are genuinely shared across owners; shared
node support belongs in `common/`.

Each model-calling node loads the correspondingly owned prompt resource:

- planning: `prompts/planning/plan_domain_execution.txt`;
- D1: `prompts/d1_sensor_data/{plan_sensor_query,generate_sensor_query,repair_sensor_query}.txt`
  plus the shared-in-D1 `adx_schema_context.txt`;
- D2: the task shell at `prompts/d2_coaching_strategy/plan_coaching_strategy.txt`
  plus the fixed-order source fragments under `advisor_policy/`;
- common response: `prompts/common/generate_final_response/`;
- operational responders: the matching files in `prompts/operational/`; and
- proactive threshold/report wording: `prompts/proactive/threshold_monitor/`.

Deterministic nodes do not get placeholder prompts merely to mirror a Python
filename. Reusable policies and service ports stay in their existing packages.

## System-initiated ownership

`chatbot.proactive.threshold_monitor`, `chatbot.proactive.system_steps_alert` and
`chatbot.episode_service` still own initial alerts, scheduled reports and
proactive episode state outside LangGraph. User replies return through common
context preparation and use the same domain orchestration as ordinary messages.
