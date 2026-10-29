# Chatbot refactoring record

> Status: **Historical canonical flat-node migration record**

This directory preserves the earlier migration snapshot. For current D1-D4,
prompt and proactive ownership, use the
[current DAG/domain layout report](../DAG_DOMAIN_LAYOUT_REFACTOR_REPORT_KR.md),
[application architecture](../../ARCHITECTURE.md),
[node ownership guide](../../chatbot/nodes/README.md), and
[prompt ownership guide](../../chatbot/prompts/README.md).

## Purpose

This directory explains how the Main Azure Function App chatbot was simplified
without discarding its Telegram, ADX, Withings, smartphone, alerting, storage,
or conversation-memory behavior.

## Files at a glance

| File | Role |
|---|---|
| [`AS_IS.md`](AS_IS.md) | Historical duplication and mixed ownership that motivated the cleanup; it is not current operator guidance. |
| [`TO_BE.md`](TO_BE.md) | Historical flat-node target, dependency direction, and safety invariants. |
| [`MIGRATION_MAP.md`](MIGRATION_MAP.md) | Auditable mapping of each move, merge, removal, and preserved production owner. |
| `README.md` | Reading guidance, migration status vocabulary, and invariants shared by the three records. |

## Reading order

Read `AS_IS.md` for the original problem, `TO_BE.md` for the resulting design,
and `MIGRATION_MAP.md` when a contributor needs to locate a former path.

For operational configuration and deployment, use [`../../README.md`](../../README.md).
For model-resource behavior, use the dedicated prompt documentation in
`chatbot/prompts/README.md`.

## Status vocabulary

| Status | Meaning |
|---|---|
| `MOVED` | An active implementation now has one canonical path. |
| `MERGED` | Node-private or single-consumer behavior was absorbed by its owning module. |
| `REMOVED` | A duplicate, alias, wrapper, or unused runtime contract was deleted. |
| `PRESERVED` | A production owner already had a clear responsibility and remained in place. |

No obsolete import path is retained as a second canonical implementation.

## Earlier flat-node target invariants (superseded)

The following list describes the target at this historical checkpoint. It is
retained to explain the migration sequence and is not a current implementation
contract.

- `chatbot/graph/workflow.py` is the only topology implementation.
- The graph registers exactly 12 nodes, with one flat Python implementation file
  per node.
- `chatbot/nodes/support.py` is shared support and is not a graph node.
- Deterministic KQL construction remains separate from candidate selection,
  authorization, model repair, and ADX execution.
- The only model-repair route is
  `kql_guard -> query_repair -> kql_guard`; it is bounded and has no direct ADX
  edge.
- Initial threshold, daily-report, and System STEPS notifications remain owned
  by their background/system modules outside LangGraph.
- A reply to a system notification re-enters the same 12-node graph through
  `hydrate_context`.
- Azure route names, environment-variable names, ADX source behavior, storage
  entities, and `ChatbotResponse` remain stable.

## Keeping this historical record accurate

Do not rewrite `TO_BE.md` as a current design. Record later canonical changes in
the current architecture, package guides, and DAG/domain report. Fix this folder
only when its description of the historical checkpoint is internally wrong.

Architecture validation should confirm:

```text
graph/workflow.py registers exactly 12 flat node imports
every responder reaches persist_context
query_repair returns only to kql_guard
no production source imports a removed grouped/alias node path
all referenced documentation paths exist
```

Syntax compilation and a local end-to-end chatbot smoke test remain the minimum
runtime checks after structural changes.
