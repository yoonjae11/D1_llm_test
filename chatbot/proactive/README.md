# Proactive alerts and reports

This package physically owns existing system-initiated behavior outside the
reactive D1-D4 graph. Function blueprints import these modules directly.

| Module | Responsibilities |
|---|---|
| `threshold_monitor.py` | Sustained threshold detection, cooldowns, daily sensor summaries, optional alert/report language and notification handoff |
| `system_steps_alert.py` | Daily STEPS baseline/candidate evaluation, quiet hours, retries, idempotent state and notification handoff |

These two modules are the only runtime import paths; no root-level compatibility
facades remain. Their two model resources live in
`prompts/proactive/threshold_monitor/`. Deterministic STEPS messages have no
model prompt.

User replies re-enter common context preparation through the existing episode
service. That service spans reactive and proactive conversation state and stays
shared. Existing reusable policies, data clients and service ports also stay
in place. Function binding names and trigger settings are unchanged.

`.funcignore` anchors the obsolete top-level experimental `/proactive/` exclusion;
it must not exclude this nested runtime package or `chatbot/prompts/proactive/`.
The offline ownership and proactive tests mock ADX, model, storage and delivery.
