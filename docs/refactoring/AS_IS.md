# AS-IS: historical chatbot structure

This document records the structure before the canonical flat-node cleanup. It
is historical context, not current import or deployment guidance.

## Historical runtime shape

The original application assembled one reactive sensor-analysis graph with the
following functional stages:

```text
context hydration
  -> intent routing
  -> planning
  -> KQL generation
  -> KQL validation
  -> ADX execution
  -> response generation
  -> context persistence
```

Capabilities, timezone onboarding, alert configuration, chat, unsupported
requests, clarification, and rejected KQL were conditional branches of the same
conversation flow.

System-initiated threshold alerts, daily reports, and System STEPS alerts were
already produced by background/function code outside the reactive graph. The
conversation store allowed a later user reply to continue through the normal
chatbot flow.

## Problems in the historical layout

The runtime behavior was valid, but ownership was difficult to understand:

- graph construction, route functions, and public graph imports existed across
  multiple modules;
- active node implementations were moved into nested responsibility packages
  while flat aliases and wrapper functions remained beside them;
- deterministic KQL construction, the graph query node, safety checks, repair
  helpers, and compatibility filenames all contained similar `kql` or `query`
  names;
- context hydration and persistence shared one implementation file even though
  they were separately registered nodes;
- helper modules existed mainly to preserve tests and monkeypatch paths rather
  than serve production runtime consumers;
- response follow-up handling was separated from the response-format policy
  despite having only one production consumer;
- architecture documentation described both obsolete and current paths as if
  both were canonical.

At the start of the cleanup, `chatbot/nodes` contained 44 tracked Python files.
Twenty-three were aliases or wrapper modules rather than independent production
implementations.

## Historical responsibility mixing

| Area | Mixed responsibilities |
|---|---|
| Graph modules | Public import compatibility, state, routes, node registration, edges, and compilation |
| Node tree | Active nodes, nested package exports, flat aliases, policy aliases, model helpers, and diagnostics |
| Query area | Strategy selection, deterministic KQL construction, generic model fallback, repair, safety, retry selection, and ADX execution |
| Response area | Intent-specific response routing, model calls, deterministic formatting, result parsing, availability, and follow-up de-duplication |
| Architecture docs | Historical paths, intermediate grouped paths, and current paths without a single source of truth |

## Behavior that had to survive cleanup

- Ordinary user-initiated Telegram messages.
- Replies to threshold alerts, daily reports, and System STEPS alerts.
- Strict timezone gating and guided onboarding.
- Capability, profile, timezone, and collected-data questions.
- Natural-language alert and scheduled-summary configuration.
- Deterministic Withings/Phone KQL and source priority.
- Read-only, user-scoped, bounded KQL enforcement.
- The bounded `guard -> repair -> guard` cycle.
- ADX result normalization and deterministic response fast paths.
- Conversation/episode state and the `ChatbotResponse` contract.

The target was therefore a structural simplification, not a replacement of the
working chatbot behavior.
