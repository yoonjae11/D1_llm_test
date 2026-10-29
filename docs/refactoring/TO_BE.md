# TO-BE: canonical Main Function App chatbot

This document records the earlier flat-node target for the deployable chatbot.
It is a historical snapshot. Current domain and prompt ownership is documented
in the [current DAG/domain layout report](../DAG_DOMAIN_LAYOUT_REFACTOR_REPORT_KR.md),
[application architecture](../../ARCHITECTURE.md),
[node ownership guide](../../chatbot/nodes/README.md), and
[prompt ownership guide](../../chatbot/prompts/README.md).

## Canonical runtime structure

```text
chatbot/
|-- entrypoint.py
|-- graph/
|   |-- __init__.py
|   |-- state.py
|   `-- workflow.py
|-- nodes/
|   |-- __init__.py
|   |-- support.py
|   |-- hydrate_context.py
|   |-- intent_router.py
|   |-- plan_builder.py
|   |-- query_generator.py
|   |-- kql_guard.py
|   |-- query_repair.py
|   |-- query_executor.py
|   |-- response_generator.py
|   |-- capabilities_responder.py
|   |-- onboarding_responder.py
|   |-- alert_config_responder.py
|   `-- persist_context.py
|-- policies/
|-- prompts/
|-- services/
|-- adapters/
|-- i18n/
|-- schemas/
|   `-- tool_result.py
|-- availability_refresh.py
|-- episode_service.py
|-- system_steps_alert.py
`-- threshold_monitor.py
```

`graph/workflow.py` is the only topology implementation. Each registered node
has one flat implementation file with the same name. `nodes/support.py` is
shared support, not a thirteenth node.

## Dependency direction

```text
Azure Functions and Telegram transport
  -> chatbot.entrypoint
  -> chatbot.services.sensor_analytics
  -> chatbot.graph
  -> chatbot.graph.workflow
  -> chatbot.nodes.*
  -> chatbot.policies + chatbot.prompts + chatbot.services
  -> chatbot.adapters
  -> established provider clients
```

- Graph state and topology do not construct provider clients.
- Nodes coordinate one state transition and delegate reusable deterministic
  behavior.
- Policies are pure and provider-independent.
- Services and adapters isolate LLM, ADX, and storage access.
- `schemas/tool_result.py` owns the transport-neutral response contract.

## Exact 12-node topology

```text
hydrate_context
  -> intent_router
       |-- data + timezone ready
       |     -> plan_builder
       |     -> query_generator
       |     -> kql_guard
       |          |-- execute -> query_executor -> response_generator
       |          |-- repair  -> query_repair -> kql_guard
       |          `-- blocked -> response_generator
       |-- data + timezone missing -> onboarding_responder
       |-- capabilities            -> capabilities_responder
       |-- alert_config            -> alert_config_responder
       `-- chat / unsupported / clarification -> response_generator
  -> persist_context
  -> END
```

The node list is fixed and auditable:

1. `hydrate_context`
2. `intent_router`
3. `plan_builder`
4. `query_generator`
5. `kql_guard`
6. `query_repair`
7. `query_executor`
8. `response_generator`
9. `capabilities_responder`
10. `onboarding_responder`
11. `alert_config_responder`
12. `persist_context`

## KQL separation

```text
plan_builder
  -> query_generator
       -> policies.kql_generation (deterministic builder)
       -> optional model candidate when configured
  -> kql_guard
       -> policies.kql_safety (authorization)
       -> query_repair -> kql_guard (bounded model repair)
  -> query_executor (ADX I/O)
```

Construction is not authorization. `policies/kql_generation.py` builds trusted
deterministic queries but is not a graph node. `query_generator.py` selects a
candidate strategy. `kql_guard.py` and `policies/kql_safety.py` decide whether a
candidate may execute. `query_repair.py` never calls ADX. `query_executor.py`
does not ask a model to generate a retry.

## User-initiated and system-initiated ownership

Ordinary messages enter the 12-node graph through `chatbot.entrypoint`.

Initial proactive messages have different owners:

- threshold alerts and daily reports: `chatbot.proactive.threshold_monitor`;
- System STEPS alerts: `chatbot.proactive.system_steps_alert`;
- episode suspension and follow-up context: `chatbot.episode_service`.

Those initial notifications do not traverse LangGraph. A user's reply invokes
the same conversation entrypoint as an ordinary message. `hydrate_context`
restores the proactive episode, after which routing, planning, KQL, response,
and persistence reuse the canonical graph.

## Structural invariants

- No duplicate wrapper or alias module for a registered node.
- No nested node implementation packages.
- No second graph builder, route module, or graph-state export path.
- No direct repair-to-executor edge.
- No provider calls from deterministic policies.
- No deletion of a background or configuration-gated production path based only
  on graph-import reachability.
- Prompt resources follow actual model tasks; deterministic nodes do not receive
  placeholder prompts merely to match filenames.
- Azure route names, environment settings, ADX source semantics, storage state,
  and `ChatbotResponse` remain behaviorally compatible.
