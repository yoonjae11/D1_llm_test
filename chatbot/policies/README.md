# Deterministic policies

> Status: **Active runtime package**

## Purpose

`chatbot.policies` contains reusable, provider-independent rules. Policies do
not register LangGraph nodes and do not call Azure, OpenAI, ADX, storage, or
function transports directly.

## Files at a glance

| Module | Responsibility | Main consumers |
|---|---|---|
| `semantic_resources.py` | Logical evidence-resource identifiers, trusted data profiles, and the production-to-physical ADX binding. | Planning, deterministic KQL, safety, availability, and evaluation adapters |
| `domain_catalog.py` | Metric definitions, compatibility aliases, semantic-resource mappings, optional vendor fields, and resource priority. | Planning, KQL, availability, and response policies |
| `metric_normalization.py` | Canonical metric names, source-aware metric selection, language detection, and deterministic follow-up suggestions. | Planning, alerting, onboarding, KQL, and responses |
| `availability.py` | Convert metric/source definitions and stored availability into collected/uncollected decisions. | Availability refresh, capabilities, and responses |
| `question_analysis.py` | Deterministic follow-up, time-only, and comparison-question recognition. | Router, planner, and response formatting |
| `time_ranges.py` | Parse and normalize local date/time ranges and produce bounded query windows. | Planner, KQL builder, executor coverage, and responses |
| `plan_sanitization.py` | Validate compatible version-1 plans and fail-closed version-2 computation plans, including follow-up inheritance and resource choice. | Planner and query generator |
| `query_algebra.py` | Define and sanitize the physical-schema-free version-2 computation algebra. | Plan sanitization, tests, and evaluation tooling |
| `query_algebra_kql.py` | Compile supported version-2 entities, predicates, selectors, and aggregates into bounded KQL with evidence metadata. | Deterministic KQL generation |
| `kql_generation.py` | Compile approved plan semantics into deterministic source-aware KQL and bounded same-resource recovery candidates. | Query generator, executor retry construction, and response metadata |
| `kql_safety.py` | Enforce read-only syntax, allowed tables, exact user scope, bounded time, and bounded results; repair known syntax deterministically. | KQL guard and optional model-candidate screening |
| `result_processing.py` | Parse, trim, normalize, and filter ADX result values. | Query executor and response generator |
| `response_format.py` | Deterministic answer polishing, source-aware formatting, analysis footers, and non-redundant follow-up handling. | Response generator |

Follow-up normalization and duplicate suppression are part of
`response_format.py`; there is no separate follow-up policy module.

## KQL boundary

Keep construction, authorization, repair orchestration, and execution separate:

| Layer | Canonical owner | May call ADX? |
|---|---|---|
| Deterministic construction | `policies/kql_generation.py` | No |
| Strategy selection | `nodes/query_generator.py` | No |
| Deterministic authorization | `policies/kql_safety.py` through `nodes/kql_guard.py` | No |
| Bounded model repair | `nodes/query_repair.py`, followed by `nodes/kql_guard.py` | No |
| Query execution | `nodes/query_executor.py` through the ADX service port | Yes |

`kql_generation.py` is a deterministic builder library, not an alternative
graph node. `query_generator.py` orchestrates candidate selection but cannot
authorize execution. `kql_guard.py` is the only graph stage that can emit the
normal `execute` route. `query_repair.py` always returns to the guard.

`query_executor.py` can retry after an ADX error only with candidates rebuilt by
the trusted deterministic construction policy. It does not ask a model to
create an executor-level retry.

## Versioned computation policy

Version-1 plans remain the compatibility path for simple values, series,
latest measurements, comparisons, and contextual follow-ups. Persisted plans
without an explicit version are interpreted through that established contract;
their aggregation behavior is not changed by the version-2 compiler.

Version 2 adds a bounded semantic algebra rather than database syntax. A plan
names an entity grain, typed metric or entity outputs, approved categorical
filters, metric conditions, and an optional ordered selector. It cannot name a
physical table, column, expression, join, or KQL operator.

The current compiler supports `day`, `night`, `sleep_session`, and
`workout_session` entities. Point-sample and hourly questions remain on the
version-1 builders. `group_by` is reserved by the sanitizer but is rejected
fail-closed until deterministic compilation is implemented.

Validation and compilation are fail-closed. The sanitizer allowlists every
entity, metric, aggregation, dimension, operator, unit, selector mode, and
positive selector limit, and it rejects incompatible combinations. Invalid
version-2 content is never stripped and executed as a broader version-1 query.
The deterministic compiler then applies the validated operations in this order:

1. Build bounded normalized rows at the requested entity grain.
2. Apply dimension filters to the applicable evidence streams.
3. Evaluate metric conditions and retain matching entities.
4. Apply first/last chronological or top/bottom metric selection with stable
   tie handling.
5. Aggregate the requested outputs over only the selected entities.

`median` uses the exact middle value (or the mean of the two middle values), not
ADX's approximate percentile aggregate. Daily activity rows are converted from
the allowlisted Health 13.x units into the metric's canonical unit before they
are combined; unknown units are excluded as missing evidence. Typed windows are
bounded to approximately ten years so malformed planner output cannot create an
unbounded multi-relation scan. Calendar anchors are unit-specific and fixed
anchors always represent one day, week, or month; incompatible combinations
fail before KQL compilation instead of silently becoming rolling windows.

Cross-resource matching is owned by this compiler and uses canonical entity
identity, such as a local day. The model expresses only the dependency's
meaning. The resulting KQL still requires the normal guard's user-scope,
read-only, bounded-time, and bounded-result approval before execution.
Typed KQL never enters model-authored generation or repair, and an ADX failure
cannot replace it with a legacy query that omits its computation semantics.
Long-form `aggregation: none` results are capped independently for each declared
output, so one metric cannot consume the configured result allowance and hide
another metric.

The current `health.samples` binding supplies compiler-normalized WORKOUT
session evidence for supported duration, steps, distance, and calorie metrics.
A future or evaluation profile that requires other workout-session fields must
provide a first-class normalized workout-session adapter. Do not extend the
planner with a dataset's physical field names.

## Semantic resource policy

The planner selects a logical evidence shape. It does not choose an ADX table or
stored function. The active, code-registered data profile performs that binding:

| Logical resource | Meaning | Shared production/evaluation binding |
|---|---|---|
| `activity.daily` | Deduplicated daily cumulative activity | `SensorActivityDaily(...)` |
| `activity.hourly` | Deduplicated hourly cumulative activity | `SensorActivityHourly(...)` |
| `activity.interval` | Interval evidence and freshness diagnostics | `SensorActivityInterval(...)` |
| `health.samples` | Deduplicated sampled and state measurements | `SensorHealthSamples(...)` |
| `sleep.nightly` | Source-resolved nightly sleep and stages | `SensorSleepNights(...)` |
| `workout.sessions` | Deduplicated workout sessions | `SensorWorkoutSessions(...)` |

The binding is intentionally code-registered and identifier-validated. Do not
accept arbitrary table names from a model, request, or environment variable.
Both databases provide the same six functions with the same canonical column,
unit, local-day, deduplication, and provenance contract. Selecting production
or evaluation requires only `ADX_DATABASE`; it does not install a PHIA-specific
profile or change prompts and metric routing.

`Phone*` remains a legacy production-schema prefix, not a provider-selection
rule. Activity views use the canonical native-source filters defined by ADX and
exclude mirrored records that could double count. Sleep uses its own
source-agnostic session and stage resolution.

Optional direct `Withings*` tables remain available only for explicitly
requested vendor-only fields or comparisons when deployment and user coverage
are known. They are not a fallback for a missing curated value. A missing
activity aggregate also must not be reconstructed by summing raw
`HealthDataFlat` rows; that path can reintroduce overlapping mirrored records.

### ADX-compatible evaluation datasets

Adapt a source schema inside ADX so it exposes the six canonical `Sensor*`
functions, then point the unchanged application at that database. Test-only
profiles still accept simple trusted identifiers for isolated unit tests, but
runtime profiles never accept KQL fragments, request-supplied names, or secrets.

A profile changes identifiers, not metric meaning. Before a historical-dataset
evaluation, its adapter must also normalize canonical units, entity keys,
deduplication, and percentage denominators. For example, production `DeepPct`
means `DeepMin / AsleepMin * 100`; a benchmark field using time in bed as its
denominator is not interchangeable and requires an explicit adapter decision.
Relative benchmark phrases must likewise be resolved against the dataset's
declared reference date and supplied to the plan as an absolute date range;
otherwise the production compiler intentionally anchors them to the user's
current local date.

### External benchmark readiness

The version-2 algebra covers the general scalar operations used by sensor QA,
but the production profile must not be described as full PHIA compatibility.
An honest end-to-end PHIA evaluation still needs a canonical adapter for workout
heart rate, active-zone minutes, elevation, and speed; explicit benchmark output
units and reference dates; a fixed sleep-day attribution policy; and separate
metric names for stage share of asleep time versus stage share of the complete
session. The current production stage percentages use asleep time as the
denominator. A 4,000-question claim also requires an executable fixture and
answer-comparison harness, not prompt inspection alone.

## System-owned deterministic queries

Not every KQL string belongs to the conversational personal-data builder.
Single-purpose system workflows keep their private deterministic queries with
their owners:

- metric-availability refresh queries in `chatbot.availability_refresh`;
- threshold and daily-report queries in `chatbot.proactive.threshold_monitor`;
- System STEPS MAD queries in `chatbot.proactive.system_steps_alert`.

These workflows do not make `query_generator` or `query_executor` reusable
proactive graph nodes. Their initial notifications run outside LangGraph.

## Dependency rules

1. Policies may import other pure policies but not `chatbot.nodes`.
2. Policies must not read environment variables when configuration can be
   passed explicitly by a node or service.
3. Provider SDK objects and credentials never belong in a policy.
4. Prefer a small public policy function over importing another module's private
   implementation details.
5. Keep user-scope and read-only checks centralized in `kql_safety.py`.
6. When merging a single-consumer policy, preserve behavior and move it to the
   closest cohesive policy rather than into graph topology.
