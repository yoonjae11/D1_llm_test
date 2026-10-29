# D1 runtime prompts

The text resources here are the single source of truth for sensor planning,
KQL generation, daily-to-flat recheck, repair and their ADX schema context.
Canonical D1 nodes load these paths directly; local tests use the same paths.
There is no dataset-specific prompt or routing branch: the same text serves
PHIA daily records, Custom Logger interval uploads and other producers.
Evaluation-only rules (for example the PHIA D1 runner's forced D1 route and
completed-day clock) live in `run_phia_all.py`, not here.

## How the prompts are written

The model does the reasoning. Each prompt states the task, how to work it out,
and only a few illustrations of the method; code adds no query rules and no
code-directed rewrites (evidence checks are recorded in the trace as
diagnostics only). Deterministic code is limited to safety (read-only, the
authenticated user's scope) and plumbing.

Illustrations are synthetic: an invented user (`u7`), 2024 dates and
activities absent from the benchmark (rowing, pilates).
`tests/test_d1_prompt_examples.py` checks that every planner example passes
plan validation, every KQL illustration passes the shared guard, and no
example question appears in the PHIA workbook.

| Resource | Role |
|---|---|
| `plan_sensor_query.txt` | Question -> typed JSON plan. Describes the plan interface the validator accepts (`time_range` shapes, `computation` members, metric vocabulary) and gives 3 examples. |
| `generate_sensor_query.txt` | Plan + original question -> one KQL query. A short method (understand, decide the kind of record and read the observed metadata, keep the period, deduplicate, build the population, reduce, empty sets, result columns), KQL language reminders and 2 illustrations. |
| `adx_schema_context.txt` | Three parts: (1) the schema - what one row of each Sensor function means; (2) what can be recorded - the kinds of Types (accumulating amounts, readings, sleep, workouts, profile values, events/logs), the shapes one producer or another uses for them, and how the metadata counts tell the shapes apart; (3) therefore how to query - where to read each kind, re-sent copies, dates, records that cross a boundary, units. Written for any producer. |
| `repair_sensor_query.txt` | Compiler/guard diagnostic -> minimal fix; common Kusto errors and their fixes. |
| `review_measurement_scope.txt` | Optional second-opinion plan review; off unless `SENSEI_D1_SCOPE_REVIEW=1`. |
| `recheck_daily_with_flat.txt` | Bounded daily-to-flat recheck (unchanged). |

## Shared conventions (all producers)

- "past/last N days" = N completed days before the reference date; "today" and
  "yesterday" are single days; "last week" = previous Sunday-Saturday week.
- Sleep is labelled by the night it starts (noon-to-noon) unless the question
  is explicitly about the wake date.
- A statistic of no observations is null; a count of nothing is 0; recorded
  zero is kept.
