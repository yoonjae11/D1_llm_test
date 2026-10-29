# Deterministic user-visible text

> Status: **Active runtime package**

## Purpose

This package owns fixed English user-visible text that must remain
deterministic. LLM instructions belong in [`../prompts/`](../prompts/), while
runtime messages, metric labels, and site labels belong here. Legacy `ko` keys
and `is_korean` parameters remain as compatibility inputs and resolve to the
same English values.

## Files at a glance

| File | Public API | Responsibility |
|---|---|---|
| `catalog.py` | `message(...)` | Formats a named deterministic English message. |
| `catalog.py` | `validate_catalog()` | Raises when a deterministic `_MESSAGES` entry lacks a compatibility key or has an empty value. It runs at import time. |
| `catalog.py` | `CAPABILITIES_GENERAL` | Long-form fixed English capability description. |
| `catalog.py` | `METRIC_LABELS` | English labels for curated HealthDataFlat-derived metrics and separately gated optional vendor metrics. |
| `catalog.py` | `TIMEZONE_SITE_LABELS` | English study-site labels used by timezone onboarding. |
| `__init__.py` | Stable re-exports | Preferred import surface for catalog values and helpers. |

Example:

```python
from chatbot.i18n import message

text = message("response.clarification", is_korean=False)
```

Callers should pass interpolation values as keyword arguments. Missing keys are
programming errors and raise `KeyError`; they are not converted into a partial
user message.

## What belongs here

- Fixed UI responses whose wording must be predictable.
- Metric, profile, alert, and timezone labels.
- Deterministic error and availability messages.
- Compatibility locale entries that deliberately share the English value.

Do not put the following here:

- LLM system/user instructions or few-shot examples; use `chatbot/prompts/`.
- Dynamic health conclusions generated from query results.
- Routing, parsing, or KQL rules; use the relevant policy module.
- Secrets, environment-specific text, or provider error payloads.

## Contributor guidance

1. Add matching English values for the `en` and compatibility `ko` entries of
   every `_MESSAGES` key.
2. Keep format placeholders identical across compatibility entries and update
   all callers if a placeholder changes.
3. Keep metric and site identifiers aligned with the canonical policy/config
   identifiers.
4. Update the capability description when the supported data-source inventory
   changes.
5. Keep code comments, documentation, and catalog values in English.

Validate changes from the Function App directory with:

```bash
python local_test.py
python -m unittest discover -s tests
```

Review the output for unchanged placeholders and leaked internal metric or
source identifiers. CI package validation must also pass.
