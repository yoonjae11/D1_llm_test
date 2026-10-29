# Operational verification scripts

> Status: **Operator tools; not deployed**

## Purpose

These scripts perform read-only checks against the Storage, ADX, Telegram, and
optional Azure OpenAI configuration used by the Main Function App. They are
excluded from the Azure deployment package by [`../.funcignore`](../.funcignore)
and should be run from a trusted workstation or controlled operations runner.

Neither script creates or modifies Azure tables, queues, ADX objects, or model
deployments.

## Files at a glance

| File | Role |
|---|---|
| `verify_chatbot_operational_storage.py` | Focused read-only verification of chatbot Storage queues/tables and the required ADX tables and mappings. |
| `verify_sensei_resources.py` | Broader pre/post-deployment verification that adds Phone, conversation-log, Telegram, producer-account, and optional model checks. |
| `README.md` | Operator prerequisites, commands, exit-code meanings, and maintenance rules for both scripts. |

## Quick start and prerequisites

1. Activate the same Python environment used for the Function App and install
   [`../requirements.txt`](../requirements.txt).
2. Run `az login` or use a managed identity with read access to the resources.
3. Make the target settings available through the process environment or an
   ignored `local.settings.json` in the Function App directory.
4. Run commands from the Function App directory so paths and local environment
   loading are unambiguous.

The shared local loader applies this precedence:

```text
existing process environment
  -> local.settings.json for missing values
  -> .env.local for still-missing values
  -> .env for still-missing values
```

Never commit any of those local secret files.

## `verify_chatbot_operational_storage.py`

This is the focused operational-storage verifier. It checks:

- the chatbot conversation-state, user-profile, metric-availability, and STEPS
  trigger-state tables;
- the metric-availability, Smartphone event, STEPS retry, and Withings webhook
  queues from [`../queue_contracts.py`](../queue_contracts.py);
- an optional `ChatbotUserProfile` seed row;
- the baseline/candidate ADX tables and ingestion mappings.

Usage:

```bash
python scripts/verify_chatbot_operational_storage.py
python scripts/verify_chatbot_operational_storage.py --app-user-id <test-user-id>
python scripts/verify_chatbot_operational_storage.py --skip-adx
```

Use `--skip-adx` only when intentionally checking Storage in isolation. A green
Storage-only run does not prove that health queries or alert audit ingestion can
reach ADX.

## `verify_sensei_resources.py`

This is the broader pre/post-deployment verifier. It reuses the focused checks
and additionally verifies or reports:

- Phone curated ADX tables/materialized views;
- the Telegram conversation-log ADX table;
- Telegram bot-token configuration warnings;
- the queue Storage account name, which must match external producers;
- Smartphone proactive-producer settings as an operator reminder;
- an optional Azure OpenAI Responses API smoke request.

Usage:

```bash
python scripts/verify_sensei_resources.py
python scripts/verify_sensei_resources.py --app-user-id <test-user-id>
python scripts/verify_sensei_resources.py --skip-adx
python scripts/verify_sensei_resources.py --openai-smoke
```

`--openai-smoke` performs an external model request and can incur cost. It is
off by default. Run it only against the intended non-production or controlled
deployment.

## Exit codes and interpretation

| Exit code | Meaning |
|---|---|
| `0` | All checks enabled for that invocation passed. Informational warnings may still require operator review. |
| `1` | One or more required resources, mappings, credentials, or checks failed. |
| `2` | The broader script could not import its focused verifier, usually indicating an incomplete checkout or broken script environment. |

The tools deliberately report all discovered errors rather than stopping after
the first one. Do not treat a script result as proof that the Telegram webhook
is registered or that a real end-to-end user query succeeds; perform the
post-deployment smoke tests in the root [README](../README.md) as well.

## Contributor guidance

1. Keep checks read-only and state that property in the module docstring.
2. Import queue names and shared configuration rather than copying integration
   strings.
3. Return nonzero when a required check fails; reserve warnings for conditions
   that are genuinely optional or require human comparison.
4. Do not print tokens, connection strings, OAuth values, raw user data, or
   health records.
5. Add a unit-testable helper for new parsing or comparison logic instead of
   embedding it in `main()`.

After changing a verifier, run its `--help`, the relevant no-network unit tests,
and the full test suite before using it against Azure.
