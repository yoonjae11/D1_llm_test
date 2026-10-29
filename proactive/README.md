# Legacy proactive context helper

> Status: **Source-only compatibility helper; not deployed**

## Purpose

This package contains the historical helper that creates a system-initiated
conversation episode before a proactive message is sent. It is retained as a
source-level compatibility reference, but it is excluded from the Azure
Function deployment by [`../.funcignore`](../.funcignore).

There is currently no production or in-repository runtime consumer of this
package. The deployed threshold and STEPS flows use
`chatbot.episode_service` and `chatbot.operational_storage`; they do not import
this helper or the source-only top-level `memory` package.

## Files at a glance

| File | Public API | Responsibility |
|---|---|---|
| `context_api.py` | `create_system_initiated_episode(...)` | Pauses an active episode, creates a system-owned episode and `SystemInitiatedContext`, appends the opening system turn, and updates the user's episode pointers. |
| `__init__.py` | Package marker | Does not register an Azure Function or perform runtime initialization. |

The helper returns `(episode, system_context)`. The caller remains responsible
for delivering the Telegram message; the helper only records operational
memory.

## Why it is not packaged

The current production notification paths already use the canonical chatbot
episode service. Packaging this duplicate source path would create two possible
implementations for the same lifecycle. `.funcignore` therefore excludes the
entire `proactive/` directory.

Do not add a new Azure Function, queue consumer, or runtime import here. New
proactive behavior belongs in:

- `chatbot/episode_service.py` for conversation interruption/resume state;
- `chatbot/proactive/system_steps_alert.py` for proactive STEPS policy and delivery;
- `chatbot/proactive/threshold_monitor.py` for threshold and daily-summary delivery;
- `functions/system_initiated_steps_bp.py` or another registered blueprint for
  Azure trigger handling.

The top-level `memory/` package remains available for source-only research and
simulation, but it is also excluded from deployment and must not become a
second production state implementation.

## Compatibility and removal

If no external checkout imports `proactive.context_api`, this package can be
removed in a deliberate compatibility cleanup together with its documentation
references. Do not silently repurpose it as a second production implementation.

Validate the canonical deployed system-initiated flow from the Function App
directory with:

```bash
python local_test.py smoke
python local_test.py system-preview
```

For a protected non-production end-to-end reply check, use
`python local_test.py system-reply ... --confirm-write`. That command exercises
`chatbot.episode_service` and the canonical LangGraph; it does not make this
legacy helper part of the runtime contract. The explicit confirmation is
required because the command writes operational conversation state.
