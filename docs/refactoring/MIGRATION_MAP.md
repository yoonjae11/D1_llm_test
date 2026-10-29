# Canonical migration map

> Historical flat-node migration snapshot. Later domain, prompt and proactive
> moves are superseded by the
> [current DAG/domain layout report](../DAG_DOMAIN_LAYOUT_REFACTOR_REPORT_KR.md),
> [application architecture](../../ARCHITECTURE.md),
> [node ownership guide](../../chatbot/nodes/README.md), and
> [prompt ownership guide](../../chatbot/prompts/README.md).

This table records completed structural changes. Paths in the **former path or
shape** column are historical references only; they are not supported runtime
imports.

Statuses:

- `MOVED`: the implementation moved to the one canonical path.
- `MERGED`: single-consumer or node-private behavior was absorbed by its owner.
- `REMOVED`: a duplicate, alias, wrapper, or unused contract was deleted.
- `PRESERVED`: the path already owned real production behavior and remains.

| Former path or shape | Canonical path | Status | Runtime behavior |
|---|---|---|---|
| Multiple graph builder/route entry modules | `chatbot/graph/workflow.py` | MOVED + MERGED | Same 12 nodes, route decisions, bounded repair cycle, and compile signature |
| Separate graph-state re-export | `chatbot/graph/state.py` | REMOVED | State contract remains canonical in `graph/state.py` |
| Context hydration and persistence in one grouped module | `chatbot/nodes/hydrate_context.py`, `chatbot/nodes/persist_context.py` | MOVED | Same storage, episode, availability, and response-chain behavior |
| Grouped routing implementations plus flat aliases | `chatbot/nodes/intent_router.py`, `capabilities_responder.py`, `onboarding_responder.py` | MOVED + REMOVED | Same routing and responder responsibilities |
| Grouped planning implementation plus flat aliases/wrappers | `chatbot/nodes/plan_builder.py` | MOVED + REMOVED | Same plan and follow-up state contract |
| Grouped query nodes plus flat aliases | `chatbot/nodes/query_generator.py`, `kql_guard.py`, `query_repair.py`, `query_executor.py` | MOVED + REMOVED | Same four graph stages and guard-repair loop |
| Optional query-generation helper module | `chatbot/nodes/query_generator.py` | MERGED | Configuration-gated generic KQL behavior remains with its node |
| Optional query-repair helper module | `chatbot/nodes/query_repair.py` | MERGED | Model repair still returns to the deterministic guard |
| Executor retry-candidate helper module | `chatbot/nodes/query_executor.py` | MERGED | Retry order remains deterministic and de-duplicated |
| Grouped response implementation plus flat alias/wrapper | `chatbot/nodes/response_generator.py` | MOVED + REMOVED | Same response branches and user-facing contract |
| Grouped alert-config implementation plus flat alias | `chatbot/nodes/alert_config_responder.py` | MOVED + REMOVED | Same extraction and storage operations |
| Node-layer aliases for constants, KQL builders, planning, time, results, and formatting | Corresponding modules in `chatbot/policies/` | REMOVED | Production nodes already use the policy implementations |
| Separate follow-up de-duplication policy | `chatbot/policies/response_format.py` | MERGED | Same deterministic normalization and duplicate suppression |
| Compatibility-only shared support helpers | `chatbot/nodes/support.py` reduced to production consumers | REMOVED | Active LLM, history, parsing, logging, and diagnostics remain |
| Implicit route/plan TypedDict documentation without runtime consumers | `chatbot/graph/state.py` and runtime dictionaries | REMOVED | No runtime coercion was removed |
| Transport response dataclass | `chatbot/schemas/tool_result.py` | PRESERVED | `ChatbotResponse` fields and defaults remain stable |
| Node-owned user prompts and example fragments | `chatbot/prompts/user_initiated/<owner>/` | MOVED + MERGED | Examples are embedded with the owning model task |
| ADX schema prompt resource | `chatbot/prompts/shared/adx_schema_context.txt` | MOVED | Same query-generation and repair context |
| Threshold/report prompts | `chatbot/prompts/system_initiated/threshold_monitor/` | MOVED | Same system notification model tasks |
| Guided input NLU prompts | `chatbot/prompts/user_initiated/guided_input_nlu/` | MOVED | Same fallback parsing tasks |
| Literal deterministic EN/KO messages | `chatbot/i18n/catalog.py` | PRESERVED | Localized deterministic output remains centralized |
| LLM provider access | `chatbot/services/llm/` and provider adapter | PRESERVED | Existing Azure/OpenAI configuration remains authoritative |
| ADX provider access | `chatbot/services/adx/` and provider adapter | PRESERVED | Existing managed-identity and ADX configuration remains authoritative |
| Operational conversation/profile storage | `chatbot/services/memory/` and `chatbot/operational_storage.py` | PRESERVED | Existing entities and storage settings remain authoritative |
| Threshold and daily-report production | `chatbot/proactive/threshold_monitor.py` | MOVED | Initial system notifications remain outside LangGraph |
| System STEPS production | `chatbot/proactive/system_steps_alert.py` | MOVED | Initial deterministic notification remains outside LangGraph |
| Proactive episode state | `chatbot/episode_service.py` | PRESERVED | User replies re-enter the canonical graph with restored context |
| Azure Functions deployment root | `function_app.py`, `functions/`, `host.json`, `requirements.txt` | PRESERVED | Function discovery and deployment layout remain unchanged |

## Removed node compatibility surface

The cleanup intentionally removed duplicate flat/grouped aliases, policy aliases
under `chatbot.nodes`, old graph import paths, and wrapper functions such as the
historical data-planner, KQL-generator, and grounded-response names. In-repository
production consumers now import only the canonical flat node modules.

## Safety result

The final query route remains:

```text
query_generator
  -> kql_guard
       |-- execute -> query_executor
       |-- repair  -> query_repair -> kql_guard
       `-- blocked -> response_generator
```

Deterministic construction remains in `policies/kql_generation.py`; authorization
remains in `policies/kql_safety.py`. Model repair cannot route directly to ADX.
