# Application services

> Status: **Active runtime package**

## Purpose

Services are the application-facing boundaries between graph orchestration and
external providers. They define small protocols, select default adapters, and
provide injectable functions for controlled local validation. Telegram HTTP processing remains in
`functions/telegram_bp.py`; this package does not duplicate transport logic.

## Files at a glance

| Path | Public API | Responsibility |
|---|---|---|
| `sensor_analytics.py` | `CompiledGraph`, `build_initial_state(...)`, `run_sensor_analytics_graph(...)` | Creates the established graph-state shape and invokes a compiled graph through an injectable factory. |
| `adx/ports.py` | `ADXQueryPort` | Minimal protocol for read-only KQL execution. |
| `adx/service.py` | `default_adx_service()` | Lazily constructs `DefaultADXQueryAdapter`. |
| `llm/ports.py` | `LLMPort`, `TextGenerationPort` | Protocols for chat/Responses calls and legacy single-prompt generation. |
| `llm/service.py` | `default_llm_service()`, `complete_chat(...)`, `generate_alert_text(...)` | Selects default LLM adapters while allowing explicit dependency injection. |
| `llm/user_facing.py` | `generate_user_facing(...)` | Uses the Responses API when enabled and falls back to chat completions without changing the response contract. |
| `memory/ports.py` | `OperationalStoragePort`, `RegisteredUserProfilePort` | Protocols for chatbot operational state and the established onboarding profile. |
| `memory/service.py` | `create_operational_storage(...)`, `get_registered_user_profile(...)` | Lazily constructs storage/profile adapters. |
| `__init__.py` and subpackage `__init__.py` files | Public re-exports | Keep common imports stable and dependency-light. |

## Quick usage

New transport or graph code should call a service function or depend on a
service protocol. It should not construct an Azure/OpenAI/ADX SDK client
directly.

### Sensor Analytics boundary

`build_initial_state(...)` constructs the complete graph input expected by the
current topology, including user/thread identifiers, message payload, query
fields, and the configured KQL-repair limit. `run_sensor_analytics_graph(...)`
accepts an optional graph factory so callers can run the same application path
with an alternate graph implementation during controlled local validation.

New transports should prefer this boundary over constructing `AgentState`
directly. The Telegram transport has additional episode-memory integration and
therefore uses compatible lower-level seams where necessary.

## Service and adapter relationship

```text
node or application flow
  -> service function / protocol
  -> default adapter selected lazily
  -> established concrete client or repository
```

The default bindings are:

- `LLMPort` -> `adapters.DefaultLLMAdapter` -> `chatbot.llm_client`
- `ADXQueryPort` -> `adapters.DefaultADXQueryAdapter` -> `chatbot.adx_client`
- `OperationalStoragePort` -> `OperationalTableRepository`
- `RegisteredUserProfilePort` -> `functions.shared.get_registered_user`

## Error and fallback behavior

Services preserve the behavior of their concrete providers. In particular,
`generate_user_facing(...)` attempts the configured Responses API chain and
then uses the supplied chat-completions model if the Responses call fails or
returns no text. Storage fail-soft behavior is selected by the caller and must
not be silently changed in a service factory.

## Contributor guidance

1. Keep protocols minimal and based on operations a current consumer needs.
2. Accept injectable protocol implementations for code that performs I/O.
3. Put SDK construction and legacy environment behavior in adapters, not ports.
4. Put deterministic parsing, KQL, and formatting in `chatbot/policies/`.
5. Do not import Azure Function transport modules from graph nodes. The one
   registered-user compatibility adapter resolves `functions.shared` lazily.
6. Remove an unused service abstraction rather than maintaining speculative
   interfaces.

Run these checks after changing a service contract:

```bash
python local_test.py smoke
python local_test.py ask --user-id "<test-app-user-id>" --message "Show my steps for today"
```

The smoke command verifies imports and graph composition without network calls.
The `ask` command exercises the configured model, ADX, and Storage boundaries;
run it only against protected non-production resources because it may persist
conversation state. Require CI package validation before deployment.
