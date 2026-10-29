# Provider adapters

> Status: **Active runtime package**

## Purpose

This package supplies concrete implementations of the provider protocols in
[`../services/`](../services/). Adapters preserve the established LLM, ADX,
registered-user, and operational-storage behavior while allowing graph nodes
and services to depend on small injectable interfaces.

Imports of concrete provider modules are intentionally lazy. This keeps
dependency-light imports usable during local validation and preserves existing
operator-facing provider behavior.

## Files at a glance

| File | Public API | Responsibility |
|---|---|---|
| `defaults.py` | `DefaultLLMAdapter` | Delegates chat completions and Responses API calls to `chatbot.llm_client`. |
| `defaults.py` | `DefaultADXQueryAdapter` | Delegates read-only KQL execution to `chatbot.adx_client.adx_manager`. |
| `defaults.py` | `LegacyAlertNarrationAdapter` | Preserves the established single-prompt `ChatOpenAI` path used to naturalize threshold alerts. |
| `defaults.py` | `LegacyRegisteredUserProfileAdapter` | Reads the established `Users` table through `functions.shared.get_registered_user`. |
| `defaults.py` | `create_operational_storage_adapter(...)` | Constructs `OperationalTableRepository` with optional connection-string injection and fail-soft behavior. |
| `__init__.py` | Lazy package exports | Exposes the adapter API without eagerly importing provider SDKs. |

## Runtime relationship

- `DefaultLLMAdapter` implements `services.llm.ports.LLMPort`.
- `DefaultADXQueryAdapter` implements `services.adx.ports.ADXQueryPort`.
- `LegacyAlertNarrationAdapter` implements the small
  `services.llm.ports.TextGenerationPort` seam.
- `LegacyRegisteredUserProfileAdapter` implements
  `services.memory.ports.RegisteredUserProfilePort`.
- `create_operational_storage_adapter` returns an implementation of
  `services.memory.ports.OperationalStoragePort`.

The `Legacy` prefix describes a compatibility-preserving provider path, not an
unused implementation. Do not duplicate provider configuration in an adapter;
environment parsing remains in the established concrete client or repository.

## Contributor guidance

1. Add an adapter only when an application service has a real runtime consumer.
2. Keep SDK-specific types out of service protocols where a dependency-light
   type is sufficient.
3. Resolve concrete modules inside methods or factories when import timing is a
   compatibility seam.
4. Preserve method signatures and error behavior expected by the corresponding
   protocol.
5. Do not place prompts, graph state transitions, or deterministic domain rules
   in this package.

Validate adapter changes from the Function App directory with:

```bash
python local_test.py smoke
python local_test.py ask --user-id "<test-app-user-id>" --message "Show my steps for today"
```

The smoke command validates imports and graph composition without network
calls. Run `ask` only with protected non-production settings; it exercises the
real ADX, model, and Storage adapters and may update conversation state. The CI
package-validation gate must pass before deployment.
