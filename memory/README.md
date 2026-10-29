# Episode-aware conversation memory

> Status: **Source-only research and compatibility reference; not deployed**

## Purpose

This package preserves the earlier episode-aware memory implementation for
research, local simulation, and compatibility analysis. It groups messages into
user- or system-initiated episodes, records turns, and builds bounded context,
but the deployed Function App does not import this package and `.funcignore`
excludes it from the Azure artifact.

The active runtime uses `chatbot/operational_storage.py`,
`chatbot/nodes/hydrate_context.py`, `chatbot/nodes/persist_context.py`, and
`chatbot/episode_service.py`. Those modules store recent turns, active context,
response-chain state, and `/new`/`/resume` episode data in
`ChatbotConversationState`. Do not introduce a production import from this
package without an explicit migration that reconciles the two data models and
their Storage tables.

## Files at a glance

| File | Public API | Responsibility |
|---|---|---|
| `models.py` | `ConversationEpisode`, `ConversationTurn`, `UserConversationState`, `UserLongTermMemory`, `SystemInitiatedContext` | Dataclasses, Azure Table entity conversion, identifiers, timestamps, hashes, and JSON helpers. |
| `store.py` | `ConversationMemoryStore` | Azure Table Storage implementation with lazy table clients and fail-soft behavior by default. |
| `store.py` | `InMemoryConversationMemoryStore` | Behavior-compatible in-memory implementation for controlled research simulation. |
| `context_resolver.py` | `ContextResolver`, `ResolvedContext` | Selects or creates the relevant episode for each incoming user message and loads its memory context. |
| `episode_manager.py` | `build_inbound_turn(...)`, `build_outbound_turn(...)`, `update_episode_after_turn(...)` | Records turn metadata and refreshes episode summaries after chatbot execution. |
| `prompt_builder.py` | `build_memory_context_text(...)`, `memory_context_for_state(...)` | Builds bounded, clearly separated memory text for graph prompts. |
| `summarizer.py` | `should_summarize(...)`, `summarize_episode(...)` | Creates a deterministic structured summary without inventing numeric facts. |
| `config.py` | `MULTITURN_*` and table-name settings | Reads memory limits, retention, feature flags, and table names. |
| `__init__.py` | Package-level public API | Re-exports the resolver and both store implementations. |

Preferred imports are:

```python
from memory import ContextResolver, ConversationMemoryStore
```

Use `memory.models` and `memory.episode_manager` directly for model or turn
construction APIs that are not package-level exports.

## Source-only data model and default tables

| Model | Default table | Key purpose |
|---|---|---|
| `ConversationEpisode` | `ConversationEpisode` | Topic/status/summary and latest query/evidence metadata for one conversation episode. |
| `ConversationTurn` | `ConversationTurn` | Ordered inbound/outbound messages and bounded route/query-result metadata for an episode. |
| `UserConversationState` | `UserConversationState` | Active, recent, user-initiated, system-initiated, and alert episode pointers for a user. |
| `UserLongTermMemory` | `UserLongTermMemory` | Explicit durable user facts with source, confidence, and optional expiry. |
| `SystemInitiatedContext` | `SystemInitiatedContext` | Trigger message, evidence, status, and expiry for a proactive or scheduled conversation. |

These tables are not required by the current Azure deployment. They are created
only when a local/research caller explicitly constructs
`ConversationMemoryStore`. If a study uses persistent rather than in-memory
storage, treat the table names and entity key layouts as study-data contracts;
a rename requires a data migration, not only an environment change.

## Legacy research lifecycle

```text
incoming registered-user message
  -> ConversationMemoryStore loads user state, recent episodes, and system context
  -> ContextResolver selects one of:
       continue_system_initiated_episode
       resume_paused_episode
       continue_active_episode
       create_new_episode
  -> recent turns, related summaries, and long-term memory are loaded
  -> ResolvedContext supplies an episode-specific graph thread id
  -> inbound and outbound turns are persisted
  -> episode metadata and deterministic summary are updated when required
```

System-initiated context is eligible only within its configured reply window.
Explicit new-topic messages can prevent an alert context from capturing an
unrelated turn. Resolution is rule-based; it must remain deterministic and
inspectable without an LLM.

## Local and research execution

When explicitly invoked, `ConversationMemoryStore` uses `AzureWebJobsStorage`
unless a connection string is injected. It creates table clients lazily through
the shared storage-resource helper. With the default `fail_soft=True`, a
temporary storage outage records `last_error` and returns empty/default state.
This behavior applies to the source-only harness; production Telegram behavior
uses the active chatbot operational repository instead.

Use `fail_soft=False` in controlled research operations that must surface a
storage failure. Do not convert authentication, privacy, or data-integrity
errors into silent success at a higher layer.

## Privacy and retention

Conversation turns and summaries can contain sensitive user text or derived
health context. Any research environment that persists this model must define
appropriate access, retention, deletion, and audit policies. Do not add raw
provider payloads, credentials, or full KQL text to memory records. Existing
hashes and bounded summaries are deliberate privacy and size controls.

## Contributor guidance

1. Keep Azure entity serialization in `models.py` and persistence operations in
   `store.py`.
2. Keep episode selection deterministic in `context_resolver.py`.
3. Keep prompt assembly bounded and sectioned in `prompt_builder.py`; do not
   insert instructions that override graph/policy safety.
4. Make the Azure and in-memory stores behaviorally consistent when extending
   the store API.
5. Preserve the default fail-soft behavior unless the caller explicitly opts
   into strict failures.
6. Add a migration plan for entity-key, table-name, or serialized-field changes.
7. Keep the package source-only unless the active runtime and deployment
   documentation are deliberately migrated to this model.

## Validation

This legacy package has no published automated validation contract and is not
loaded by the canonical local smoke command. Validate the deployed episode
implementation instead:

```bash
python local_test.py smoke
python local_test.py system-preview
```

To exercise the active system-initiated episode and reply path against protected
non-production Storage, run:

```bash
python local_test.py system-reply --user-id "<test-app-user-id>" --chat-id "<test-chat-id>" --alert-text "Your steps were lower than usual." --message "Why was that?" --confirm-write
```

The reply command deliberately writes operational conversation state. It does
not validate this source-only model. Changes to this package require a separate
research validation plan or an explicit migration into the active runtime.
