# Transport and domain schemas

> Status: **Active runtime package**

## Purpose

This package owns the transport-neutral response and typed D1-D4 orchestration
contracts. Existing sensor query plans and KQL validation remain in their
canonical policy modules. The transport response fields remain unchanged.

## Files at a glance

| File | Public API | Responsibility |
|---|---|---|
| `tool_result.py` | `ChatbotResponse` | Carries the final text, route, intent, execution status, referenced KQL tables, ADX row count, and diagnostic payload from the chatbot entry point to a transport. |
| `domain_execution_plan.py` | `DomainExecutionPlan` | Intent, selected domains, typed requirements, generic dependencies, bounded stages and early-exit conditions; concrete-planning request semantics. |
| `domain_execution_result.py` | `DomainExecutionResult`, `DomainRunOutput` | Common selected/status/result/missing-information/confidence/provenance wrapper plus explicit parent state updates; statuses include `blocked_dependency`. |
| `d2_coaching_strategy_plan.py` | `D2CoachingStrategyPlan`, `MIDelivery`, `SDTSupport` | Typed Advisor strategy, objectives, COM-B, catalogue selections and delivery constraints; no evaluator scores. |
| `__init__.py` | `ChatbotResponse` | Provides the preferred package-level import. |

Use the package-level import:

```python
from chatbot.schemas import ChatbotResponse
```

## Boundary rules

- `ChatbotResponse` is a compatibility contract shared by Telegram and local
  tooling. Coordinate field changes with every producer and transport
  consumer.
- Keep provider SDK objects, Azure Function request or response objects,
  LangGraph construction, and business rules outside this package.
- Keep `payload` diagnostic and serializable. Do not place secrets, raw access
  tokens, provider clients, or unbounded health records in it.
- A field rename or type change requires a deliberate transport migration;
  adding a default does not make an incompatible change safe automatically.

## Changing the contract

1. Identify every `ChatbotResponse` constructor and consumer.
2. Update `chatbot/entrypoint.py`, transport serialization, and public
   documentation together.
3. Run the no-network package smoke check:

   ```bash
   python local_test.py smoke
   ```

4. With protected non-production settings, exercise the real response path:

   ```bash
   python local_test.py ask --user-id "<test-app-user-id>" --message "Show my steps for today"
   ```

5. Inspect the emitted JSON fields and require the CI package-validation gate
   to pass before deployment. The live command may query ADX and update
   operational conversation state.
