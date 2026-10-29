# Conversation graph

The public `build_graph(checkpointer=None)` API retains six stages and the
existing Telegram/transport-neutral response contract. Initial alerts and
daily summaries continue outside this graph.

```text
START -> prepare_context -> plan_domain_execution -> execute_domains
      -> aggregate_domain_results -> generate_final_response
      -> persist_trace_and_context -> END
```

Context preparation reuses profile, timezone, availability, episode and
conversation hydration and clears request-local query/domain artifacts.
The planner combines intent and domain selection in one typed JSON model call;
exact greetings and thanks are deterministic. The validator rejects malformed
plans before any runner is called. Operational responders remain reachable
inside common final response generation.

## Scheduling contract

`selected_domains`, `domain_requirements`, `dependencies`, `execution_stages`
and `early_exit_conditions` represent D1-D4 without a pair-specific flag.
The scheduler supports independent parallel domains and dependency DAGs such
as `[D1 || D3] -> D2` or `D4 -> D1 -> D2`, with a hard maximum of three stages.
Cycles, self-dependencies, unselected references, inconsistent stage order,
duplicate execution positions and malformed plans fail closed.

All four registry runners return `DomainRunOutput(result, state_updates)`.
Declared results are passed in `dependency_results`. Required dependencies
must be `success`; otherwise the runner is not called and its wrapper is
`blocked_dependency`. `domain_requirements[domain].optional_dependencies`
permits continuation while preserving failures and missing evidence.
Isolated state copies and propagated contextvars support parallel calls.
Shared orchestration/message updates and concurrent state conflicts are rejected.

## Actual subgraphs

```text
D1: plan_sensor_query -> generate_sensor_query -> guard_sensor_query
                                                    | execute -> execute_sensor_query
                                                    | repair  -> repair_sensor_query
                                                    |               -> guard_sensor_query
    execute_sensor_query | compiler error -> repair_sensor_query
                         | no evidence, and every selected Type/label is absent
                         |   from the observed inventory (once per question)
                         |   -> request_evidence_feedback -> generate_sensor_query
    invalid/empty/blocked paths and execution -> produce_sensor_result -> END

D2: prepare_coaching_context -> plan_coaching_strategy
    -> validate_coaching_plan -> produce_coaching_result -> END

D3: prepare_health_knowledge_request -> classify_knowledge_and_safety_need
    -> formulate_retrieval_query -> retrieve_authoritative_evidence
    -> validate_and_rank_evidence -> apply_scope_and_safety_policy
    -> produce_health_knowledge_result -> END

D4: prepare_context_candidates -> resolve_context_reference
    -> validate_context_resolution -> produce_context_result -> END
```

D1 implementation physically lives in `nodes/d1_sensor_data/`. Existing typed
plans, KQL policies, guard, bounded repair, result parsing and provider services
are reused. Invalid plans and unsafe KQL stop before ADX. A query with more
than one result statement is sent to repair, because only the first result
table is read. D2 has exactly one
strategy model step; preparation, policy validation and result wrapping are
Python operations. D2 produces Advisor strategy only, without scoring or writing
the final answer. D3 retrieves only hash-validated snapshots from approved
sources and abstains when none match; its safety escalation outranks coaching.
D4 resolves only the current session and retains source-turn IDs. Both initial
implementations are deterministic and make no model call.

Aggregation supplies successful results plus honest statuses to the common
final response. Existing sensor error/no-data and onboarding explanations retain
priority; missing required domains are identified without inventing evidence.
`persist_trace_and_context` retains existing conversation storage and records
plan, stages, statuses, dependency provenance, D2 strategy, D3 evidence IDs and
D4 source-turn resolution. Full D1 rows are
not copied into durable active context. Old strategy/trace fields are excluded
from the next turn's memory prompt.

The `chatbot.nodes` root contains no executable node modules. D1 transition
functions are colocated with its edges in `nodes/d1_sensor_data/workflow.py`.
The deterministic greeting fast path and route mapping are colocated with the
combined planner in `nodes/planning/plan_domain_execution.py`. Production,
evaluation, and local tooling import these canonical owners directly.

## Offline checks

Run from the repository root with the prepared Python environment:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s azure_function_main_app/tests -v
.\.venv\Scripts\python.exe -m unittest evaluation.test_offline evaluation.test_diagnostics
.\.venv\Scripts\python.exe azure_function_main_app/local_test.py smoke
```

Tests mock model, ADX, Telegram and storage boundaries. They establish local
structure/compatibility, not live model coaching quality or integration behavior.
