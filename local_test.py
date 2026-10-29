"""Single local validation and live-chat utility for the Sensei main app.

The default ``smoke`` command is offline and read-only. ``ask`` calls the real
chatbot entrypoint and may use Azure OpenAI, ADX, and Table Storage. The
``system-reply`` command additionally writes a synthetic proactive episode to
the configured operational storage, so it requires explicit confirmation.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from runtime_environment import load_local_environment


APP_ROOT = Path(__file__).resolve().parent
EXPECTED_GRAPH_NODES = {
    "prepare_context",
    "plan_domain_execution",
    "execute_domains",
    "aggregate_domain_results",
    "generate_final_response",
    "persist_trace_and_context",
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate Sensei locally or invoke its real chatbot entrypoint."
    )
    subcommands = parser.add_subparsers(dest="command")

    subcommands.add_parser(
        "smoke",
        help="Validate imports, Azure Function registration, graph topology, prompts, and KQL guard offline.",
    )

    ask = subcommands.add_parser(
        "ask",
        help="LIVE: send one user-initiated message through the production graph.",
    )
    _add_message_arguments(ask)
    ask.add_argument(
        "--reply-to-text",
        default="",
        help="Optional earlier assistant message quoted by the user.",
    )

    preview = subcommands.add_parser(
        "system-preview",
        help="Render a synthetic system STEPS alert without sending or storing it.",
    )
    preview.add_argument("--target-date", default=dt.date.today().isoformat())
    preview.add_argument("--current", type=float, default=2500)
    preview.add_argument("--median", type=float, default=7000)

    reply = subcommands.add_parser(
        "system-reply",
        help="LIVE/WRITE: seed a synthetic system episode, then process the user's reply.",
    )
    _add_message_arguments(reply)
    reply.add_argument("--alert-text", required=True)
    reply.add_argument(
        "--confirm-write",
        action="store_true",
        help="Required acknowledgement that configured Table Storage will be modified.",
    )
    return parser


def _add_message_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--user-id", required=True, help="Sensei AppUserId.")
    parser.add_argument("--chat-id", default="", help="Telegram chat id; defaults to the user thread.")
    parser.add_argument("--message", required=True, help="User message to process.")


def _load_environment() -> None:
    load_local_environment(APP_ROOT)


def _smoke() -> int:
    import function_app
    from chatbot.availability_refresh import (
        _merge_rows,
        _source_policy,
        build_metric_availability_kql,
    )
    from chatbot.graph import build_graph
    from chatbot.nodes.d1_sensor_data.workflow import guard_decision, query_generation_decision
    from chatbot.nodes.d1_sensor_data.execute_sensor_query import _build_retry_candidates, _coverage_context
    from chatbot.nodes.d1_sensor_data.repair_sensor_query import repair_sensor_query
    from chatbot.policies.availability import metric_availability_keys
    from chatbot.policies.domain_catalog import METRIC_DEFINITIONS
    from chatbot.policies.kql_generation import (
        build_deterministic_query,
        metric_result_alias,
    )
    from chatbot.policies.kql_safety import inspect_query_safety
    from chatbot.policies.semantic_resources import (
        ACTIVITY_DAILY,
        ACTIVITY_HOURLY,
        ACTIVITY_INTERVAL,
        CONVERSATION_HISTORY,
        HEALTH_SAMPLES,
        SENSEI_ADX_V1,
        SLEEP_NIGHTLY,
        WORKOUT_SESSIONS,
        WITHINGS_ACTIVITY,
        WITHINGS_INTRADAY,
        WITHINGS_NIGHT_EVENTS,
        WITHINGS_RESOURCE_IDS,
        WITHINGS_SLEEP,
        WITHINGS_SLEEP_DETAIL,
        WITHINGS_WORKOUTS,
        physical_name,
        prompt_resource_values,
        tabular_call,
        use_data_profile,
        with_overrides,
    )
    from chatbot.policies.plan_sanitization import (
        _DEFAULT_METRICS_BY_CASE,
        sanitize_plan,
    )
    from chatbot.policies.query_algebra import sanitize_computation
    from chatbot.policies.query_algebra_kql import (
        compile_computation_query,
        validate_computation_support,
    )
    from chatbot.policies.time_ranges import (
        EvaluationTimeContext,
        _parse_time_range_from_question,
        compute_window_bounds,
        use_evaluation_time,
    )
    from chatbot.policies.result_processing import trim_records_for_plan
    from chatbot.policies.question_analysis import (
        _asks_for_aggregate,
        _is_contextual_followup,
        _is_time_only_followup,
    )
    from chatbot.policies.response_format import (
        _build_analysis_footer,
        _build_source_aware_monitoring_answer,
    )
    from chatbot.nodes.planning.plan_domain_execution import _fast_intent
    from chatbot.prompts import load_chat_prompt, load_prompt, split_role_prompt
    from chatbot.proactive.threshold_monitor import _daily_sleep_query, _data_lines

    functions = function_app.app.get_functions()
    if not functions:
        raise AssertionError("No Azure Functions were registered.")

    graph_nodes = set(build_graph().get_graph().nodes)
    missing_nodes = EXPECTED_GRAPH_NODES - graph_nodes
    if missing_nodes:
        raise AssertionError(f"Missing graph nodes: {sorted(missing_nodes)}")
    from chatbot.nodes.d1_sensor_data.workflow import build_d1_graph
    expected_d1 = {"plan_sensor_query", "generate_sensor_query", "guard_sensor_query", "repair_sensor_query",
                   "execute_sensor_query", "produce_sensor_result"}
    if not expected_d1.issubset(build_d1_graph().get_graph().nodes):
        raise AssertionError("D1 subgraph lost an existing query/guard/repair step")

    # Availability discovery must use the same canonical Sensor contract as
    # query generation. Direct vendor tables remain explicit opt-in sources.
    optional_withings_env = "CHATBOT_ENABLE_OPTIONAL_WITHINGS_TABLES"
    previous_optional_withings = os.environ.get(optional_withings_env)
    try:
        os.environ[optional_withings_env] = "false"
        default_availability_kql = build_metric_availability_kql(
            "local-smoke-user"
        )
        if "SensorHealthSamples(" not in default_availability_kql:
            raise AssertionError(
                "Default metric availability no longer calls SensorHealthSamples."
            )
        for backing_name in ("HealthDataFlat", "PhoneCumulativeDailyMV"):
            if backing_name in default_availability_kql:
                raise AssertionError(
                    f"Availability query exposes backing storage: {backing_name}."
                )
        if "WithingsActivity" in default_availability_kql:
            raise AssertionError(
                "Default metric availability unexpectedly scans direct Withings tables."
            )
        empty_availability = _merge_rows("local-smoke-user", [])
        expected_missing_tables = {
            physical_name(resource_id) for resource_id in WITHINGS_RESOURCE_IDS
        }
        if {
            item.preferred_source_table for item in empty_availability
        } != expected_missing_tables:
            raise AssertionError(
                "Empty availability refresh did not emit active-profile optional "
                "source markers."
            )

        os.environ[optional_withings_env] = "true"
        optional_availability_kql = build_metric_availability_kql(
            "local-smoke-user"
        )
        for optional_table in ("WithingsActivity", "WithingsSleep"):
            if optional_table not in optional_availability_kql:
                raise AssertionError(
                    "Explicit optional-source availability omitted "
                    f"{optional_table}."
                )
    finally:
        if previous_optional_withings is None:
            os.environ.pop(optional_withings_env, None)
        else:
            os.environ[optional_withings_env] = previous_optional_withings

    daily_sleep_kql = _daily_sleep_query("local-smoke-user", "Asia/Seoul")
    if "HasAsleepDuration" not in daily_sleep_kql:
        raise AssertionError(
            "The daily report query lost the actual-asleep evidence flag."
        )
    stage_less_lines = _data_lines(
        {},
        {
            "AsleepMin": None,
            "AsleepHm": "",
            "TimeInBedHm": "7h 50m",
            "AwakeMin": 0,
            "SleepEfficiencyPct": None,
            "HasAsleepDuration": False,
            "Onset": "2026-09-02 23:20:00",
            "Wake": "2026-09-03 07:10:00",
        },
        {},
    )
    stage_less_text = "\n".join(stage_less_lines)
    if (
        "actual asleep duration unavailable" not in stage_less_text
        or "awake ~0 min" in stage_less_text
        or "efficiency" in stage_less_text
    ):
        raise AssertionError(
            f"Stage-less daily sleep output is misleading: {stage_less_text!r}"
        )

    source_policy_cases = (
        (
            "Android platform aggregate",
            ("STEPS", "googleHealthConnect", "android", ""),
            True,
        ),
        (
            "Health Connect phone aggregate",
            (
                "STEPS",
                "googleHealthConnect",
                "com.android.healthconnect.phone.pixel",
                "",
            ),
            True,
        ),
        (
            "Google Fit mirrored activity",
            (
                "STEPS",
                "googleHealthConnect",
                "com.google.android.apps.fitness",
                "",
            ),
            False,
        ),
        (
            "Withings mirrored activity",
            (
                "STEPS",
                "googleHealthConnect",
                "com.withings.wiscale2",
                "",
            ),
            False,
        ),
        (
            "Renamed iPhone native activity",
            (
                "STEPS",
                "appleHealth",
                "JH",
                "com.apple.health.device-id",
            ),
            True,
        ),
        (
            "Apple Watch mirrored locomotion",
            (
                "STEPS",
                "appleHealth",
                "Personal Apple Watch",
                "com.apple.health.device-id",
            ),
            False,
        ),
        (
            "Apple Watch native energy",
            (
                "ACTIVE_ENERGY_BURNED",
                "appleHealth",
                "Personal Apple Watch",
                "com.apple.health.device-id",
            ),
            True,
        ),
    )
    for label, arguments, expected in source_policy_cases:
        actual = _source_policy(*arguments)
        if actual is not expected:
            raise AssertionError(
                f"Activity source policy mismatch for {label}: {actual}"
            )

    if metric_availability_keys("steps") != {"STEPS"}:
        raise AssertionError("Generic steps lost its unified STEPS availability key.")
    if metric_availability_keys("phone_steps") != {"STEPS"}:
        raise AssertionError(
            "The compatibility phone_steps alias no longer shares STEPS availability."
        )
    total_sleep_keys = metric_availability_keys("total_sleep_time")
    if "SLEEP_SESSION" in total_sleep_keys or not {
        "SLEEP_ASLEEP",
        "SLEEP_LIGHT",
        "SLEEP_DEEP",
        "SLEEP_REM",
    }.issubset(total_sleep_keys):
        raise AssertionError(
            "Total-sleep availability no longer distinguishes asleep-stage "
            "evidence from a session window."
        )
    if metric_availability_keys("phone_sleep_duration") != total_sleep_keys:
        raise AssertionError(
            "The phone sleep-duration compatibility alias no longer shares the "
            "actual-asleep availability contract."
        )
    wake_keys = metric_availability_keys("sleep_end_local")
    if not {"SLEEP_SESSION", "SLEEP_IN_BED"}.issubset(wake_keys):
        raise AssertionError(
            "Wake-time availability lost session/in-bed timing evidence."
        )
    if "SLEEP_AWAKE" not in metric_availability_keys("awake_duration"):
        raise AssertionError("Awake-duration availability lost SLEEP_AWAKE evidence.")
    if metric_availability_keys("sleep_score") != {
        "SOURCE_TABLE:WITHINGSSLEEP"
    }:
        raise AssertionError(
            "Vendor-only sleep_score borrowed an unrelated unified availability key."
        )
    if any(
        "sleep_score" in metrics for metrics in _DEFAULT_METRICS_BY_CASE.values()
    ):
        raise AssertionError(
            "A generic planning default restored optional vendor-only sleep_score."
        )

    prompt_root = APP_ROOT / "chatbot" / "prompts"
    prompt_files = sorted(prompt_root.rglob("*.txt"))
    if not prompt_files:
        raise AssertionError("No prompt resources were found.")
    role_prompt_count = 0
    for path in prompt_files:
        resource = path.relative_to(prompt_root).as_posix()
        text = load_prompt(resource)
        if not text.strip():
            raise AssertionError(f"Empty prompt resource: {resource}")
        if re.search(r"[\uac00-\ud7a3]", text):
            raise AssertionError(
                f"Runtime prompt must be written in English; Hangul found in {resource}."
            )
        if "@@" in text:
            raise AssertionError(f"Unresolved prompt fragment marker: {resource}")
        if "[SYSTEM]" in text or "[USER]" in text:
            split_role_prompt(text, resource=resource)
            role_prompt_count += 1

    # General-health chat must answer the question directly and must not imply
    # that this app supports a nutrition-log connection that does not exist.
    chat_resource = "common/generate_final_response/chat.txt"
    chat_contract = " ".join(load_prompt(chat_resource).lower().split())
    required_chat_rules = (
        "lead with the direct answer",
        "2-4 short sentences",
        "about 80 words",
        "do not mention data access, connection, collection, or synchronization",
        "nutrition, diet, meal, or food logs",
        "that capability is not implemented",
    )
    missing_chat_rules = [rule for rule in required_chat_rules if rule not in chat_contract]
    if missing_chat_rules:
        raise AssertionError(
            f"General-health chat prompt is missing concise/capability rules: {missing_chat_rules}"
        )
    obsolete_chat_rule = (
        "if data has not been connected or collected, you must clearly state "
        "that and offer general guidance only"
    )
    if obsolete_chat_rule in chat_contract:
        raise AssertionError(
            "General-health chat still requires an unsolicited data-connection preamble."
        )

    # Prompt design regression: every active task must compile with its balanced
    # few-shot set, while the task prompts remain reviewable rather than growing
    # back into an exhaustive rule dump.
    reviewable_prompt_limits = {
        "planning/plan_domain_execution.txt": 70,
        "d1_sensor_data/plan_sensor_query.txt": 240,
        "d1_sensor_data/generate_sensor_query.txt": 130,
        "d1_sensor_data/repair_sensor_query.txt": 70,
        "d2_coaching_strategy/plan_coaching_strategy.txt": 70,
        "common/generate_final_response/sensor_data.txt": 260,
        "common/generate_final_response/chat.txt": 80,
        "common/generate_final_response/coaching.txt": 50,
        "common/generate_final_response/unsupported_metric.txt": 40,
        "operational/alert_config_responder.txt": 50,
        "operational/capabilities_responder.txt": 50,
        "proactive/threshold_monitor/rewrite_threshold_alert.txt": 10,
        "proactive/threshold_monitor/daily_report_coach.txt": 20,
    }
    for resource, max_lines in reviewable_prompt_limits.items():
        prompt_text = load_prompt(resource)
        load_chat_prompt(resource)
        line_count = len(prompt_text.splitlines())
        if line_count > max_lines:
            raise AssertionError(
                f"Prompt {resource} grew to {line_count} lines (limit {max_lines})."
            )

    # Prompt resources are Python ``str.format`` templates. Exact input-variable
    # contracts catch an easy few-shot editing mistake: adding literal JSON with
    # single braces silently creates an unintended template variable and fails
    # only when the production node invokes the prompt.
    prompt_input_contracts = {
        "d1_sensor_data/adx_schema_context.txt": {"user_id"},
        "planning/plan_domain_execution.txt": set(),
        "d1_sensor_data/plan_sensor_query.txt": {
            "today_date",
            "today_weekday",
            "available_metrics",
            "user_available_metrics",
            "history",
            "memory_context",
            "last_assistant_message",
            "last_plan",
            "question",
        },
        "d1_sensor_data/generate_sensor_query.txt": {
            "schema",
            "user_id",
            "question",
            "now",
            "plan_json",
        }
        | {
            "activity_daily_resource",
            "health_samples_resource",
            "sleep_nightly_resource",
            "workout_sessions_resource",
        },
        "d1_sensor_data/repair_sensor_query.txt": {
            "schema",
            "user_id",
            "question",
            "plan_json",
            "kql",
            "error_hint",
        } | {"activity_daily_resource"},
        "d2_coaching_strategy/plan_coaching_strategy.txt": set(),
        "common/generate_final_response/sensor_data.txt": {
            "answer_language",
            "today_date",
            "question",
            "previous_assistant",
            "memory_context",
            "case_type",
            "metrics",
            "computation",
            "time_range",
            "coverage_context",
            "assumptions",
            "data_json",
        },
        "common/generate_final_response/chat.txt": {
            "answer_language",
            "history",
            "memory_context",
            "question",
        },
        "common/generate_final_response/coaching.txt": {
            "answer_language",
            "coaching_strategy",
            "coverage_context",
            "data_json",
            "history",
            "memory_context",
            "question",
        },
        "common/generate_final_response/unsupported_metric.txt": {
            "answer_language",
            "question",
        },
        "operational/alert_config_responder.txt": {"history", "pending_spec", "question"},
        "operational/capabilities_responder.txt": {"question"},
        "proactive/threshold_monitor/rewrite_threshold_alert.txt": {"deterministic", "facts"},
        "proactive/threshold_monitor/daily_report_coach.txt": {"data_lines", "part_of_day"},
    }
    for resource, expected_variables in prompt_input_contracts.items():
        actual_variables = set(load_chat_prompt(resource).input_variables)
        if actual_variables != expected_variables:
            raise AssertionError(
                f"Prompt {resource} input variables changed: "
                f"expected {sorted(expected_variables)}, got {sorted(actual_variables)}. "
                "Check that literal JSON braces in few-shot examples are doubled."
            )

    # The D1 sensor schema deliberately spells the deployed canonical function
    # names literally. Keep those names synchronized with the active profile so
    # neither a prompt edit nor a binding edit can silently create two contracts.
    schema_context = load_prompt("d1_sensor_data/adx_schema_context.txt")
    expected_sensor_functions = {
        "activity_daily_resource": "SensorActivityDaily",
        "activity_hourly_resource": "SensorActivityHourly",
        "activity_interval_resource": "SensorActivityInterval",
        "health_samples_resource": "SensorHealthSamples",
        "sleep_nightly_resource": "SensorSleepNights",
        "workout_sessions_resource": "SensorWorkoutSessions",
    }
    rendered_resource_values = prompt_resource_values()
    for placeholder, function_name in expected_sensor_functions.items():
        if rendered_resource_values.get(placeholder) != function_name:
            raise AssertionError(
                "The active Sensor binding no longer matches the literal prompt "
                f"contract: {placeholder} -> {rendered_resource_values.get(placeholder)!r}, "
                f"expected {function_name!r}."
            )
        expected_call = (
            f"{function_name}(TargetUserId, StartLocal, EndLocal)"
        )
        if expected_call not in schema_context:
            raise AssertionError(
                "The D1 ADX schema prompt is missing canonical call "
                f"{expected_call!r}."
            )
        if "{" + placeholder + "}" in schema_context:
            raise AssertionError(
                "The D1 ADX schema prompt still contains resource placeholder "
                f"{placeholder!r}."
            )

    def prompt_section(resource: str, start: str, end: str = "") -> str:
        text = load_prompt(resource).replace("\r\n", "\n")
        if start not in text:
            raise AssertionError(f"Prompt {resource} is missing section {start!r}.")
        section = text.split(start, 1)[1]
        if end:
            if end not in section:
                raise AssertionError(
                    f"Prompt {resource} section {start!r} is missing terminator {end!r}."
                )
            section = section.split(end, 1)[0]
        return section.strip()

    def numbered_examples(section: str) -> list[str]:
        starts = list(re.finditer(r"(?m)^\d+\.\s", section))
        return [
            section[match.start() : starts[index + 1].start()].strip()
            if index + 1 < len(starts)
            else section[match.start() :].strip()
            for index, match in enumerate(starts)
        ]

    def assert_example_count(
        resource: str,
        label: str,
        examples: list[str],
        *,
        minimum: int,
        maximum: int,
    ) -> None:
        count = len(examples)
        if not minimum <= count <= maximum:
            raise AssertionError(
                f"Prompt {resource} has {count} {label} examples; "
                f"expected {minimum}-{maximum}."
            )

    def assert_example_case(
        resource: str,
        examples: list[str],
        cue: str,
        required_tokens: tuple[str, ...],
    ) -> str:
        cue_folded = " ".join(cue.casefold().split())
        matches = [
            block
            for block in examples
            if cue_folded in " ".join(block.casefold().split())
        ]
        if len(matches) != 1:
            raise AssertionError(
                f"Prompt {resource} must contain exactly one few-shot case for {cue!r}; "
                f"found {len(matches)}."
            )
        block = " ".join(matches[0].casefold().split())
        missing = [
            token
            for token in required_tokens
            if " ".join(token.casefold().split()) not in block
        ]
        if missing:
            raise AssertionError(
                f"Prompt {resource} few-shot {cue!r} is missing expected tokens: {missing}"
            )
        return matches[0]

    # Domain planning owns routing and dependency policy. Its prompt is plain
    # policy text; the typed schema and conversation context are separate messages.
    planner_resource = "planning/plan_domain_execution.txt"
    planner_contract = " ".join(load_prompt(planner_resource).casefold().split())
    for token in (
        "preserve these intents:",
        "d1 is sensor evidence & query reasoning",
        "d2 is coaching strategy & dialogue delivery",
        "d3 is health knowledge & safety",
        "d4 is multi-turn context",
        "selected_domains",
        "domain_requirements",
        "optional_dependencies",
        "dependencies",
        "execution_stages",
        "dependencies are required",
        "never exceed three",
        "return only json conforming to the supplied schema",
    ):
        if token not in planner_contract:
            raise AssertionError(
                f"Prompt {planner_resource} is missing domain-planning policy: {token!r}"
            )

    # Few-shot examples should cover the semantic boundaries that caused the
    # observed D1 regressions. The behavioral sanitizer/KQL assertions below
    # independently verify that Python implements the same contracts.
    plan_resource = "d1_sensor_data/plan_sensor_query.txt"
    plan_prompt_text = load_prompt(plan_resource)
    for physical_resource_name in (
        "PhoneCumulativeDailyMV",
        "PhoneCumulativeHourlyMV",
        "PhoneCumulativeIntervalDedupMV",
        "PhoneSleepNights",
        "HealthDataFlat",
    ):
        if physical_resource_name in plan_prompt_text:
            raise AssertionError(
                "The semantic planner prompt contains a physical ADX resource: "
                f"{physical_resource_name}"
            )
    exact_plan_examples = numbered_examples(
        prompt_section(plan_resource, "Representative JSON examples")
    )
    assert_example_count(
        plan_resource,
        "semantic JSON",
        exact_plan_examples,
        minimum=9,
        maximum=14,
    )
    required_plan_cases = {
        'After that answer, "What time?"': (
            '"metrics":["heart_rate"]',
            '"latest_sample":true',
            '"answer_focus":"measurement_time"',
        ),
        '"What time did I wake up today?"': (
            '"is_followup":false',
            '"metrics":["sleep_end_local"]',
            '"resources_priority":["sleep.nightly"]',
        ),
        '"How many treadmill sessions did I have in the last 7 days?"': (
            '"resources_priority":["workout.sessions"]',
            '"entity":"workout_session"',
            '"aggregation":"count"',
        ),
        '"Total duration of my first four Interval Workout sessions?"': (
            '"mode":"all_available"',
            '"selector":{{"mode":"first","n":4}',
        ),
        '"Sum Run steps on days when sleep was at most 220 minutes in 31 days."': (
            '"resources_priority":["sleep.nightly","workout.sessions"]',
            '"metric":"total_sleep_time","operator":"lte","value":220',
        ),
        '"Step count on the day I slept longest in the past 7 days."': (
            '"metrics":["total_sleep_time","steps"]',
            '"selector":{{"mode":"top","n":1,"by_metric":"total_sleep_time"}',
        ),
        '"What was the median awake percentage over the last 14 nights?"': (
            '"metrics":["awake_percent"]',
            '"aggregation":"median"',
        ),
        '"What was my total active-zone time?"': (
            '"mode":"all_available"',
            '"metrics":["active_zone_minutes"]',
        ),
        '"Show average HR, elevation, zone minutes, and speed for my last two runs."': (
            '"workout_average_heart_rate"',
            '"workout_elevation_gain"',
            '"workout_active_zone_minutes"',
            '"workout_speed"',
        ),
        '"How were my steps over the last 7 days?"': (
            '"value":7,"unit":"day","anchor":"rolling"',
            '"entity":"day"',
            '"metric":"steps","aggregation":"none"',
        ),
        '"Show my steps for last week."': (
            '"value":1,"unit":"week","anchor":"last_week"',
            '"metric":"steps","aggregation":"none"',
        ),
        '"Tell me my average steps over the last 7 days."': (
            '"value":7,"unit":"day","anchor":"rolling"',
            '"metric":"steps","aggregation":"mean"',
        ),
    }
    for cue, tokens in required_plan_cases.items():
        assert_example_case(plan_resource, exact_plan_examples, cue, tokens)

    chat_resource = "common/generate_final_response/chat.txt"
    chat_examples = numbered_examples(
        prompt_section(chat_resource, "Few-shot examples", "Recent history:")
    )
    assert_example_count(
        chat_resource, "general-chat", chat_examples, minimum=3, maximum=6
    )
    milk_example = assert_example_case(
        chat_resource,
        chat_examples,
        "Question: Milk or Coke: which is healthier?",
        ("protein and calcium", "regular coke", "water"),
    )
    milk_answer = milk_example.split("Answer:", 1)[-1]
    if len(re.findall(r"\b[\w'-]+\b", milk_answer)) > 90:
        raise AssertionError("The Milk-vs-Coke few-shot answer is no longer concise.")
    for unwanted in (
        "don't have",
        "do not have",
        "nutrition log",
        "diet log",
        "not connected",
        "general guidance only",
    ):
        if unwanted in milk_answer.casefold():
            raise AssertionError(
                f"The general-health few-shot restored an unsolicited capability preamble: {unwanted!r}"
            )

    unsupported_resource = "common/generate_final_response/unsupported_metric.txt"
    unsupported_examples = numbered_examples(
        prompt_section(unsupported_resource, "Few-shot examples")
    )
    assert_example_count(
        unsupported_resource,
        "unsupported-metric",
        unsupported_examples,
        minimum=1,
        maximum=4,
    )
    food_example = assert_example_case(
        unsupported_resource,
        unsupported_examples,
        "Question: How many calories did I eat today?",
        ("food-intake calories are not collected", "calories burned"),
    )
    for unsupported_offer in ("connect", "enable", "upload", "sync"):
        if unsupported_offer in food_example.casefold():
            raise AssertionError(
                "The unsupported nutrition few-shot suggests a nonexistent integration: "
                f"{unsupported_offer!r}"
            )

    data_resource = "common/generate_final_response/sensor_data.txt"
    data_prompt = load_prompt(data_resource)
    compact_data_prompt = " ".join(data_prompt.split())
    for dependent_lookup_token in (
        "Dependent lookup:",
        "lead with the requested result",
        "selector metric and value",
        "never substitute another date",
        "among the recorded days or nights",
    ):
        if dependent_lookup_token not in compact_data_prompt:
            raise AssertionError(
                "The data-answer prompt lost its dependent-lookup contract: "
                f"{dependent_lookup_token!r}"
            )
    data_examples = numbered_examples(
        prompt_section(data_resource, "Representative examples")
    )
    assert_example_count(
        data_resource, "data-answer", data_examples, minimum=12, maximum=20
    )
    assert_example_case(
        data_resource,
        data_examples,
        "Question: What is my current heart rate?",
        ("recorded_at_local", "ingestion time", "recorded at 06:41 am"),
    )
    assert_example_case(
        data_resource,
        data_examples,
        "Question: What was my Withings sleep score last night?",
        ("sleepscore = 82", "withings sleep score was 82"),
    )
    assert_example_case(
        data_resource,
        data_examples,
        "Question: What about yesterday?",
        ("previous answer", "yesterday's wake-up value", "07:29 am"),
    )
    assert_example_case(
        data_resource,
        data_examples,
        "Question: How much deep sleep did I get yesterday?",
        ("1h08m", "september 1"),
    )
    assert_example_case(
        data_resource,
        data_examples,
        "Question: How many steps did I take this morning, and which hours contributed?",
        ("06:00 am-12:00 pm", "2,249 steps", "08:00 am", "09:00 am"),
    )
    assert_example_case(
        data_resource,
        data_examples,
        "Question: Is my HRV lower than usual today?",
        ("rmssd", "personal baseline", "25%"),
    )
    assert_example_case(
        data_resource,
        data_examples,
        "Question: Why might I feel so tired today?",
        ("deep sleep", "efficiency", "cannot establish"),
    )
    assert_example_case(
        data_resource,
        data_examples,
        "Question: How should I manage today after sleeping poorly last night?",
        ("lighter recovery day", "gentle movement", "earlier wind-down"),
    )
    assert_example_case(
        data_resource,
        data_examples,
        "16. Insufficient evidence for personalization",
        ("do not show a clear recovery deficit", "physical fatigue", "mental fog"),
    )
    dependent_lookup_example = assert_example_case(
        data_resource,
        data_examples,
        "17. Cross-metric dependent lookup",
        ("daily period/metric rows", "august 29 steps row"),
    )
    dependent_lookup_answer = " ".join(
        dependent_lookup_example.split("Answer:", 1)[-1].split()
    )
    for answer_token in ("6h34m", "august 29", "8,679 steps"):
        if answer_token not in dependent_lookup_answer.casefold():
            raise AssertionError(
                "The cross-metric answer omitted required supporting evidence: "
                f"{answer_token!r}"
            )
    missing_lookup_example = assert_example_case(
        data_resource,
        data_examples,
        "18. Dependent value missing on the selected date",
        ("other dates have", "no step row for august 29"),
    )
    missing_lookup_answer = " ".join(
        missing_lookup_example.split("Answer:", 1)[-1].split()
    )
    for answer_token in ("6h34m", "august 29", "no step record"):
        if answer_token not in missing_lookup_answer.casefold():
            raise AssertionError(
                "The missing dependent-value answer lost its evidence or gap: "
                f"{answer_token!r}"
            )
    assert_example_case(
        data_resource,
        data_examples,
        "19. Session timing without actual-asleep evidence",
        (
            "asleepmin=null",
            "hasasleepduration=false",
            "in-bed window of 7h50m",
            "does not contain enough sleep-stage evidence",
        ),
    )

    query_resource = "d1_sensor_data/generate_sensor_query.txt"
    query_examples = prompt_section(
        query_resource, "Representative semantic patterns"
    )
    query_example_count = len(
        re.findall(r"(?m)^let TargetUserId=", query_examples)
    )
    if not 4 <= query_example_count <= 6:
        raise AssertionError(
            f"Prompt {query_resource} has {query_example_count} KQL examples; "
            "expected 4-6."
        )
    for query_token in (
        "{activity_daily_resource}(TargetUserId, StartLocal, EndLocal)",
        "{health_samples_resource}(TargetUserId, StartLocal, EndLocal)",
        "{sleep_nightly_resource}(TargetUserId, StartLocal, EndLocal)",
        "{workout_sessions_resource}(TargetUserId, StartLocal, EndLocal)",
        'Type == "RESTING_HEART_RATE"',
        'Metric="awake_percent"',
        'Aggregation="none"',
        'ActivityCode == "INTERVAL_WORKOUT"',
        "join kind=inner RunSteps on Period",
        'ResultStatus=iff(EvidenceRows > 0, "ok", "no_metric_evidence")',
    ):
        if query_token not in query_examples:
            raise AssertionError(
                f"Prompt {query_resource} is missing representative KQL token: "
                f"{query_token}"
            )
    for disallowed_prompt_source in (
        "HealthDataFlat",
        "PhoneCumulativeDailyMV",
        "PhoneSleepNights",
        "PHIASummary",
        "PHIAExercise",
    ):
        if disallowed_prompt_source in query_examples:
            raise AssertionError(
                "A query-generator example exposes backing storage: "
                f"{disallowed_prompt_source}"
            )

    data_answer_contract = " ".join(
        load_prompt("common/generate_final_response/sensor_data.txt")
        .lower()
        .split()
    )
    for obsolete_rule in (
        "always include the general recommended value",
        "required structure: paragraph",
        "list the day-by-day values",
        "data shows you are physically more relaxed",
    ):
        if obsolete_rule in data_answer_contract:
            raise AssertionError(
                f"Data-answer prompt restored an over-prescriptive rule: {obsolete_rule}"
            )

    # Only the disputed temporal defaults are deterministic: completed rolling
    # days exclude today, while calendar weeks run Sunday through Saturday.
    policy_clock = EvaluationTimeContext(
        summary_reference_date=dt.date(2026, 9, 22),
        summary_min_date=dt.date(2025, 1, 1),
        workout_reference_date=dt.date(2026, 9, 22),
        workout_min_date=dt.date(2025, 1, 1),
    )
    policy_time_cases = (
        ("Show my steps for the last 7 days", "rolling", dt.date(2026, 9, 15), dt.date(2026, 9, 21)),
        ("Show my sleep for the last 7 nights", "rolling", dt.date(2026, 9, 15), dt.date(2026, 9, 21)),
        ("지난 7일간 걸음 수", "rolling", dt.date(2026, 9, 15), dt.date(2026, 9, 21)),
        ("Show my steps for the last week", "last_week", dt.date(2026, 9, 13), dt.date(2026, 9, 19)),
        ("지난 주 걸음 수", "last_week", dt.date(2026, 9, 13), dt.date(2026, 9, 19)),
        ("Show my steps for the last 2 weeks", "last_week", dt.date(2026, 9, 6), dt.date(2026, 9, 19)),
        ("Show my steps for the past month", "last_month", dt.date(2026, 8, 1), dt.date(2026, 8, 31)),
    )
    with use_evaluation_time(policy_clock):
        for question_text, expected_anchor, expected_start, expected_end in policy_time_cases:
            parsed_range, _, has_explicit_time = _parse_time_range_from_question(
                question_text
            )
            if (
                not has_explicit_time
                or not isinstance(parsed_range, dict)
                or parsed_range.get("anchor") != expected_anchor
                or compute_window_bounds(parsed_range, "Asia/Seoul")
                != (expected_start, expected_end)
            ):
                raise AssertionError(
                    "Research time-window policy drifted for "
                    f"{question_text!r}: {parsed_range}"
                )

        # A model-produced conflicting range cannot redefine these few policy
        # phrases; all other temporal interpretation remains model-owned.
        enforced_last_week = sanitize_plan(
            {
                "schema_version": 1,
                "case_type": "monitoring",
                "metrics": ["steps"],
                "time_range": {
                    "mode": "relative",
                    "value": 7,
                    "unit": "day",
                    "anchor": "rolling",
                },
            },
            "Show my steps for the last week",
            {},
        )
        enforced_last_days = sanitize_plan(
            {
                "schema_version": 1,
                "case_type": "monitoring",
                "metrics": ["steps"],
                "time_range": {
                    "mode": "relative",
                    "value": 1,
                    "unit": "day",
                    "anchor": "today",
                },
            },
            "Show my steps for the last 7 days",
            {},
        )
        if enforced_last_week.get("time_range", {}).get("anchor") != "last_week":
            raise AssertionError("The planner overrode the fixed last-week policy.")
        if enforced_last_days.get("time_range") != {
            "mode": "relative",
            "value": 7,
            "unit": "day",
            "anchor": "rolling",
        }:
            raise AssertionError("The planner overrode the fixed last-N-days policy.")

    # A previous data plan must not make every short sentence a data follow-up.
    for new_topic in ("Who are you?", "Is milk healthy?", "Delete all alerts", "Weight today?"):
        if _is_contextual_followup(new_topic):
            raise AssertionError(f"Short topic change was marked contextual: {new_topic}")
        if _fast_intent(new_topic) is not None:
            raise AssertionError(f"Short topic change was hard-routed: {new_topic}")
    for contextual in ("Yes please", "Compare them", "What time?"):
        if not _is_contextual_followup(contextual):
            raise AssertionError(f"True contextual reply was not recognized: {contextual}")
    if not _is_time_only_followup("What about yesterday?"):
        raise AssertionError("A period-only follow-up was not recognized.")
    if _is_time_only_followup("Weight today?"):
        raise AssertionError("A short metric topic change was treated as period-only.")

    # Latest sampled measurements are a different contract from period
    # aggregates. A short follow-up must preserve the heart-rate metric and the
    # latest-sample intent, then change only the answer focus to recorded time.
    latest_question = "What is my current heart rate?"
    latest_plan = sanitize_plan(
        {
            "case_type": "monitoring",
            "metrics": ["heart_rate"],
            "time_range": {
                "mode": "relative",
                "value": 1,
                "unit": "day",
                "anchor": "today",
            },
        },
        latest_question,
        {},
    )
    latest_plan["timezone_iana"] = "Asia/Seoul"
    latest_metrics = latest_plan.get("metrics", [])
    if len(latest_metrics) != 1 or latest_metrics[0] not in {
        "heart_rate",
        "phone_heart_rate",
    }:
        raise AssertionError(
            f"Current heart-rate planning was not narrowed to one metric: {latest_metrics}"
        )
    if latest_plan.get("latest_sample") is not True:
        raise AssertionError("Current heart-rate planning did not retain latest_sample=true.")
    if latest_plan.get("answer_focus") != "value_and_time":
        raise AssertionError(
            "Current heart-rate planning must focus on both value and recorded time."
        )

    # Reproduce the original failure mode: if the planner returns no usable
    # JSON, the sanitizer must not fall back to the broad 14-day sleep/activity
    # pack for this high-confidence question.
    fallback_latest_plan = sanitize_plan({}, latest_question, {})
    if fallback_latest_plan.get("metrics") != ["heart_rate"]:
        raise AssertionError(
            "Planner fallback broadened a current heart-rate question: "
            f"{fallback_latest_plan.get('metrics')}"
        )
    if fallback_latest_plan.get("latest_sample") is not True:
        raise AssertionError(
            "Planner fallback lost latest_sample for current heart rate."
        )
    fallback_range = fallback_latest_plan.get("time_range", {})
    if fallback_range.get("anchor") != "today":
        raise AssertionError(
            "Planner fallback may present an older rolling-window sample as current: "
            f"{fallback_range}"
        )

    stale_current_plan = sanitize_plan(
        {
            "case_type": "monitoring",
            "metrics": ["heart_rate"],
            "time_range": {
                "mode": "relative",
                "value": 14,
                "unit": "day",
                "anchor": "rolling",
            },
        },
        latest_question,
        {},
    )
    if stale_current_plan.get("time_range", {}).get("anchor") != "today":
        raise AssertionError(
            "A planner-provided rolling default may present an older sample as current."
        )

    for now_question in ("What is my heart rate now?", "Show my current heart rate."):
        now_plan = sanitize_plan({}, now_question, {})
        if now_plan.get("metrics") != ["heart_rate"] or not now_plan.get("latest_sample"):
            raise AssertionError(
                f"Standalone now wording lost latest heart-rate semantics: {now_plan}"
            )
        if now_plan.get("time_range", {}).get("anchor") != "today":
            raise AssertionError(
                f"Standalone now wording did not use today's window: {now_plan}"
            )

    heart_metric = str(latest_metrics[0])
    heart_alias = metric_result_alias(
        METRIC_DEFINITIONS.get(heart_metric, {}).get("summary", "")
    )
    if not heart_alias:
        raise AssertionError(f"No result alias is defined for {heart_metric}.")
    recorded_alias = f"{heart_alias}_recorded_at_local"
    ingestion_alias = f"{heart_alias}_latest_ingestion_utc"

    def assert_latest_heart_query(query: str, *, label: str) -> None:
        if not query:
            raise AssertionError(f"{label} produced no deterministic KQL.")
        lowered = query.lower()
        if "sensorhealthsamples(" not in lowered:
            raise AssertionError(
                f"{label} did not use the canonical SensorHealthSamples contract."
            )
        if "withings" in lowered:
            raise AssertionError(
                f"{label} unexpectedly queried or referenced direct Withings data."
            )
        if "take 1" not in lowered:
            raise AssertionError(f"{label} did not select exactly the latest sample.")
        if "avg(" in lowered or "avgif(" in lowered:
            raise AssertionError(f"{label} collapsed a latest-sample request to an average.")
        if recorded_alias.lower() not in lowered:
            raise AssertionError(
                f"{label} did not project measurement timestamp alias {recorded_alias}."
            )
        if ingestion_alias.lower() not in lowered:
            raise AssertionError(
                f"{label} did not keep ingestion timestamp alias {ingestion_alias}."
            )
        decision = inspect_query_safety(query, "local-smoke-user")
        if not decision["is_safe"]:
            raise AssertionError(f"{label} generated unsafe KQL: {decision}")

    latest_query = build_deterministic_query(
        "local-smoke-user", latest_question, latest_plan
    )
    assert_latest_heart_query(latest_query, label="Current heart rate")

    fallback_latest_plan["timezone_iana"] = "Asia/Seoul"
    fallback_latest_query = build_deterministic_query(
        "local-smoke-user", latest_question, fallback_latest_plan
    )
    assert_latest_heart_query(
        fallback_latest_query,
        label="Current heart-rate planner fallback",
    )

    # A complete wake-up question starts a new topic even when the previous
    # turn asked for the latest heart-rate measurement. The sanitizer must not
    # treat every phrase containing "what time" as an HR timestamp follow-up.
    wake_question = "What time do I wake up today?"
    wake_candidate = {
        "case_type": "monitoring",
        "is_followup": False,
        "time_range": {
            "mode": "relative",
            "value": 1,
            "unit": "day",
            "anchor": "today",
        },
        "metrics": ["sleep_end_local"],
        "granularity": "intraday",
        "latest_sample": False,
        "answer_focus": "value",
        "comparison_requested": False,
        "needs_followup": False,
        "followup_question": "",
    }
    wake_plan = sanitize_plan(wake_candidate, wake_question, latest_plan)
    if wake_plan.get("metrics") != ["sleep_end_local"]:
        raise AssertionError(
            "A complete wake-up question inherited the previous heart-rate metric: "
            f"{wake_plan}"
        )
    if wake_plan.get("time_range", {}).get("anchor") != "today":
        raise AssertionError(f"Today's wake-up question lost its date: {wake_plan}")
    if wake_plan.get("latest_sample") is not False:
        raise AssertionError(
            f"A wake-up time became a latest sensor sample: {wake_plan}"
        )
    if wake_plan.get("answer_focus") != "value":
        raise AssertionError(f"A wake-up question inherited HR timestamp focus: {wake_plan}")
    wake_plan["timezone_iana"] = "Asia/Seoul"
    wake_query = build_deterministic_query(
        "local-smoke-user",
        wake_question,
        wake_plan,
    )
    if not all(
        token in wake_query
        for token in ("SensorSleepNights", "sleep_end_local", "WakeLocal")
    ):
        raise AssertionError(f"A wake-up plan did not generate sleep KQL: {wake_query}")
    if "Withings" in wake_query or "HealthDataFlat" in wake_query:
        raise AssertionError(
            f"A wake-up plan bypassed the canonical SensorSleepNights function: {wake_query}"
        )
    wake_query_decision = inspect_query_safety(wake_query, "local-smoke-user")
    if not wake_query_decision["is_safe"]:
        raise AssertionError(f"Wake-up KQL failed the safety guard: {wake_query_decision}")

    yesterday_wake_plan = sanitize_plan({}, "what about yesterday?", wake_plan)
    if yesterday_wake_plan.get("metrics") != ["sleep_end_local"]:
        raise AssertionError(
            "The yesterday follow-up did not retain the wake-up metric: "
            f"{yesterday_wake_plan}"
        )
    if yesterday_wake_plan.get("time_range", {}).get("anchor") != "yesterday":
        raise AssertionError(
            f"The wake-up follow-up did not switch to yesterday: {yesterday_wake_plan}"
        )
    if yesterday_wake_plan.get("latest_sample") is not False:
        raise AssertionError(
            f"The yesterday wake-up follow-up became a latest sample: {yesterday_wake_plan}"
        )
    if yesterday_wake_plan.get("answer_focus") != "value":
        raise AssertionError(
            f"The yesterday wake-up follow-up inherited HR timestamp focus: {yesterday_wake_plan}"
        )

    average_question = "What is my average heart rate today?"
    average_plan = sanitize_plan(
        {},
        average_question,
        {},
    )
    average_plan["timezone_iana"] = "Asia/Seoul"
    if average_plan.get("latest_sample"):
        raise AssertionError("An explicit average heart-rate request became a latest sample.")
    if average_plan.get("answer_focus") != "value":
        raise AssertionError("An average heart-rate request must focus on the aggregate value.")
    average_query = build_deterministic_query(
        "local-smoke-user", average_question, average_plan
    )
    if not average_query or not any(
        aggregate in average_query.lower() for aggregate in ("avg(", "avgif(")
    ):
        raise AssertionError("An explicit average heart-rate request lost its average KQL.")

    average_time_followup = sanitize_plan({}, "What time?", average_plan)
    if average_time_followup.get("latest_sample"):
        raise AssertionError(
            "A time-only follow-up silently changed a period average into a latest sample."
        )
    if not average_time_followup.get("needs_followup"):
        raise AssertionError(
            "A time-only follow-up to an average must explain that the aggregate has no one timestamp."
        )

    if _asks_for_aggregate("What does my current heart rate mean?"):
        raise AssertionError("The verb 'mean' was misclassified as an average request.")
    if _asks_for_aggregate("My heart rate feels strange"):
        raise AssertionError("A substring inside 'strange' was misclassified as range.")

    multi_heart_plan = sanitize_plan(
        {
            "case_type": "monitoring",
            "metrics": ["heart_rate", "phone_resting_heart_rate"],
            "time_range": {
                "mode": "relative",
                "value": 1,
                "unit": "day",
                "anchor": "today",
            },
        },
        "What are my current heart rate and resting heart rate?",
        {},
    )
    if multi_heart_plan.get("metrics") != ["heart_rate", "phone_resting_heart_rate"]:
        raise AssertionError(
            "Current-heart fallback narrowing overwrote valid multi-metric planning: "
            f"{multi_heart_plan.get('metrics')}"
        )

    prior_steps_plan = sanitize_plan(
        {
            "case_type": "monitoring",
            "metrics": ["phone_steps"],
            "time_range": {
                "mode": "relative",
                "value": 1,
                "unit": "day",
                "anchor": "yesterday",
            },
        },
        "Show yesterday's steps",
        {},
    )
    short_topic_change = sanitize_plan(
        {},
        "current heart rate?",
        prior_steps_plan,
    )
    if short_topic_change.get("metrics") != ["heart_rate"]:
        raise AssertionError(
            "A short explicit heart-rate topic change inherited the previous metric: "
            f"{short_topic_change.get('metrics')}"
        )
    if short_topic_change.get("time_range", {}).get("anchor") != "today":
        raise AssertionError(
            "A current heart-rate topic change inherited the previous date."
        )

    max_heart_plan = sanitize_plan(
        {
            "case_type": "monitoring",
            "metrics": ["heart_rate_max"],
            "time_range": {
                "mode": "relative",
                "value": 1,
                "unit": "day",
                "anchor": "today",
            },
        },
        "What is my current max heart rate today?",
        {},
    )
    if max_heart_plan.get("metrics") != ["heart_rate_max"] or max_heart_plan.get("latest_sample"):
        raise AssertionError(
            f"Maximum heart rate was rewritten as a latest generic sample: {max_heart_plan}"
        )

    comparison_plan = sanitize_plan(
        {
            "case_type": "monitoring",
            "metrics": ["heart_rate"],
            "time_range": {
                "mode": "relative",
                "value": 1,
                "unit": "day",
                "anchor": "today",
            },
            "comparison_requested": True,
        },
        "Is my current heart rate higher than yesterday?",
        {},
    )
    if comparison_plan.get("latest_sample"):
        raise AssertionError("A comparison retained an incompatible latest-sample contract.")

    average_alias_plan = sanitize_plan(
        {
            "case_type": "monitoring",
            "metrics": ["hr_average"],
            "time_range": {
                "mode": "relative",
                "value": 1,
                "unit": "day",
                "anchor": "today",
            },
        },
        "What was my average heart rate today?",
        {},
    )
    if average_alias_plan.get("metrics") != ["heart_rate"]:
        raise AssertionError(
            "The generic average-heart-rate alias was routed to a vendor sleep metric: "
            f"{average_alias_plan.get('metrics')}"
        )

    provider_comparison_plan = sanitize_plan(
        {
            "case_type": "monitoring",
            "metrics": ["steps"],
            "time_range": {
                "mode": "relative",
                "value": 1,
                "unit": "day",
                "anchor": "today",
            },
        },
        "Compare my Withings and smartphone steps today.",
        {},
    )
    if provider_comparison_plan.get("metrics") != ["steps"]:
        raise AssertionError(
            "An unsupported provider comparison fabricated a second metric series: "
            f"{provider_comparison_plan.get('metrics')}"
        )
    if not provider_comparison_plan.get("needs_followup"):
        raise AssertionError(
            "An unsupported provider comparison did not request a truthful clarification."
        )

    phone_comparison_question = (
        "Is my current smartphone resting heart rate higher than yesterday?"
    )
    phone_comparison_plan = sanitize_plan(
        {
            "case_type": "monitoring",
            "metrics": ["phone_resting_heart_rate"],
            "time_range": {
                "mode": "relative",
                "value": 1,
                "unit": "day",
                "anchor": "today",
            },
            "comparison_requested": True,
        },
        phone_comparison_question,
        {},
    )
    phone_comparison_query = build_deterministic_query(
        "local-smoke-user",
        phone_comparison_question,
        phone_comparison_plan,
    )
    if not any(
        token in phone_comparison_query.lower()
        for token in ("avg(", "avgif(")
    ):
        raise AssertionError(
            "A current-vs-prior Phone comparison was rewritten as one latest sample."
        )
    if "_recorded_at_local" in phone_comparison_query.lower():
        raise AssertionError(
            "A comparison query emitted the latest-sample timestamp contract."
        )

    blood_pressure_question = "What are my current systolic and diastolic blood pressure?"
    blood_pressure_plan = sanitize_plan(
        {
            "case_type": "monitoring",
            "metrics": [
                "phone_blood_pressure_systolic",
                "phone_blood_pressure_diastolic",
            ],
            "time_range": {
                "mode": "relative",
                "value": 1,
                "unit": "day",
                "anchor": "today",
            },
        },
        blood_pressure_question,
        {},
    )
    blood_pressure_query = build_deterministic_query(
        "local-smoke-user",
        blood_pressure_question,
        blood_pressure_plan,
    )
    if "summarize arg_max(RecordedAtLocal, *) by Type" not in blood_pressure_query:
        raise AssertionError("Multi-sampled latest query did not retain one correctly labelled row per Type.")
    if "phone_bp_sys=todouble(ValueNumber)" in blood_pressure_query:
        raise AssertionError("A multi-Type sampled value was projected under the first metric alias.")
    blood_pressure_decision = inspect_query_safety(
        blood_pressure_query,
        "local-smoke-user",
    )
    if not blood_pressure_decision["is_safe"]:
        raise AssertionError(
            f"Multi-sampled latest KQL failed the guard: {blood_pressure_decision}"
        )

    latest_row = {
        heart_alias: 58.21,
        f"{heart_alias}_source": "smartphone",
        recorded_alias: "2026-09-02T11:42:00",
        ingestion_alias: "2099-12-31T23:59:00Z",
    }

    def assert_measurement_time_answer(answer: str | None, *, label: str) -> None:
        if not answer:
            raise AssertionError(f"{label} produced no deterministic monitoring answer.")
        lowered = answer.lower()
        if "11:42" not in answer:
            raise AssertionError(f"{label} omitted the recorded measurement time: {answer}")
        if "heart rate" not in lowered and "heart-rate" not in lowered:
            raise AssertionError(f"{label} lost the inherited heart-rate metric: {answer}")
        if "average" in lowered:
            raise AssertionError(f"{label} described a latest measurement as an average: {answer}")
        if any(token in answer for token in ("2099", "2100", "11:59", "08:59")):
            raise AssertionError(f"{label} exposed ingestion time as measurement time: {answer}")

    initial_answer = _build_source_aware_monitoring_answer(
        latest_plan, [latest_row], question=latest_question
    )
    assert_measurement_time_answer(initial_answer, label="Current heart-rate answer")
    if "58.21" not in str(initial_answer):
        raise AssertionError(
            f"Current heart-rate answer omitted the latest value: {initial_answer}"
        )

    for followup_question in ("What time", "please say time"):
        followup_plan = sanitize_plan({}, followup_question, latest_plan)
        followup_plan["timezone_iana"] = "Asia/Seoul"
        if followup_plan.get("metrics") != latest_metrics:
            raise AssertionError(
                f"{followup_question!r} lost the heart-rate metric: "
                f"{followup_plan.get('metrics')}"
            )
        if followup_plan.get("latest_sample") is not True:
            raise AssertionError(
                f"{followup_question!r} lost the latest-sample intent."
            )
        if followup_plan.get("answer_focus") != "measurement_time":
            raise AssertionError(
                f"{followup_question!r} did not focus the answer on measurement time."
            )
        followup_query = build_deterministic_query(
            "local-smoke-user", followup_question, followup_plan
        )
        assert_latest_heart_query(
            followup_query, label=f"Heart-rate follow-up {followup_question!r}"
        )
        followup_answer = _build_source_aware_monitoring_answer(
            followup_plan, [latest_row], question=followup_question
        )
        assert_measurement_time_answer(
            followup_answer, label=f"Heart-rate follow-up {followup_question!r}"
        )

    simple_steps_plan = sanitize_plan(
        {
            "case_type": "monitoring",
            "metrics": ["steps"],
            "time_range": {
                "mode": "relative",
                "value": 1,
                "unit": "day",
                "anchor": "today",
            },
        },
        "How many steps did I walk today?",
        {},
    )
    simple_steps_answer = _build_source_aware_monitoring_answer(
        simple_steps_plan,
        [{"steps": 8420, "steps_source": "curated"}],
        question="How many steps did I walk today?",
    )
    if not simple_steps_answer or "8,420 steps" not in simple_steps_answer:
        raise AssertionError(
            f"Simple monitoring answer lost its value/unit: {simple_steps_answer}"
        )
    if "I retrieved" in simple_steps_answer or "from Withings" in simple_steps_answer:
        raise AssertionError(
            f"Simple monitoring answer restored retrieval narration: {simple_steps_answer}"
        )
    if _build_analysis_footer(
        simple_steps_plan,
        question="How many steps did I walk today?",
    ):
        raise AssertionError("A simple one-day fact received an analysis footer.")

    direct_withings_tables = (
        "WithingsActivity",
        "WithingsSleep",
        "WithingsIntraday",
        "WithingsSleepDetailMinute",
        "WithingsWorkouts",
        "WithingsSleepNightEventSegment",
    )

    def assert_curated_route(
        query: str | None,
        *,
        label: str,
        required_table: str,
        forbidden_core_tables: tuple[str, ...] = (),
    ) -> str:
        if not query:
            raise AssertionError(f"{label} produced no deterministic KQL.")
        if required_table not in query:
            raise AssertionError(
                f"{label} did not use required resource {required_table}: {query}"
            )
        unexpected = [
            table
            for table in (*direct_withings_tables, *forbidden_core_tables)
            if table in query
        ]
        if unexpected:
            raise AssertionError(
                f"{label} used unexpected resources {unexpected}: {query}"
            )
        decision = inspect_query_safety(query, "local-smoke-user")
        if not decision["is_safe"]:
            raise AssertionError(f"{label} generated unsafe KQL: {decision}")
        return query

    # Generic metrics resolve by data shape, not vendor name: daily cumulative
    # activity uses the deduplicated daily MV, while finer requests use its
    # hourly or interval layer. Direct Withings tables are never an implicit
    # fallback for these ordinary questions.
    simple_steps_plan["timezone_iana"] = "Asia/Seoul"
    assert_curated_route(
        build_deterministic_query(
            "local-smoke-user",
            "How many steps did I walk today?",
            simple_steps_plan,
        ),
        label="Generic daily steps",
        required_table="SensorActivityDaily(",
        forbidden_core_tables=(
            "PhoneCumulativeHourlyMV",
            "PhoneCumulativeIntervalDedupMV",
            "HealthDataFlat",
        ),
    )

    for activity_label, activity_question, activity_granularity, required_table in (
        (
            "Generic hourly steps",
            "Show my hourly steps today.",
            "hourly",
            "SensorActivityHourly(",
        ),
        (
            "Generic interval steps",
            "Show the individual step intervals recorded today.",
            "interval",
            "SensorActivityInterval(",
        ),
    ):
        activity_plan = sanitize_plan(
            {
                "case_type": "trend",
                "metrics": ["steps"],
                "time_range": {
                    "mode": "relative",
                    "value": 1,
                    "unit": "day",
                    "anchor": "today",
                },
                "granularity": activity_granularity,
                "latest_sample": False,
            },
            activity_question,
            {},
        )
        activity_plan["timezone_iana"] = "Asia/Seoul"
        assert_curated_route(
            build_deterministic_query(
                "local-smoke-user", activity_question, activity_plan
            ),
            label=activity_label,
            required_table=required_table,
            forbidden_core_tables=tuple(
                table
                for table in (
                    "PhoneCumulativeDailyMV",
                    "PhoneCumulativeHourlyMV",
                    "PhoneCumulativeIntervalDedupMV",
                    "HealthDataFlat",
                )
                if table not in required_table
            ),
        )

    cross_metric_question = (
        "What was my step count on the day I slept the longest this past 7 days?"
    )
    cross_metric_plan = sanitize_plan(
        {
            "case_type": "monitoring",
            "metrics": ["total_sleep_time", "steps"],
            "time_range": {
                "mode": "relative",
                "value": 7,
                "unit": "day",
                "anchor": "rolling",
            },
            "granularity": "daily",
            "latest_sample": False,
            "answer_focus": "value",
            "comparison_requested": False,
        },
        cross_metric_question,
        {},
    )
    if cross_metric_plan.get("metrics") != ["total_sleep_time", "steps"]:
        raise AssertionError(
            "The longest-sleep-day step lookup lost one of its required metrics: "
            f"{cross_metric_plan.get('metrics')}"
        )
    cross_metric_plan["timezone_iana"] = "Asia/Seoul"
    cross_metric_query = build_deterministic_query(
        "local-smoke-user", cross_metric_question, cross_metric_plan
    )
    def assert_multi_metric_evidence_query(
        query: str | None,
        *,
        label: str,
        metrics: tuple[str, ...],
        source_tables: tuple[str, ...],
    ) -> str:
        if not query:
            raise AssertionError(f"{label} produced no deterministic KQL.")
        required_tokens = [
            *(f"Metric='{metric}'" for metric in metrics),
            *(f"SourceTable='{table}'" for table in source_tables),
            "project Period, Metric, Value, ValueText, Source, SourceTable, EvidenceRows",
            "order by Period asc, Metric asc",
        ]
        missing = [token for token in required_tokens if token not in query]
        if missing:
            raise AssertionError(
                f"{label} lost requested evidence fields or series: {missing}"
            )
        unexpected = [table for table in direct_withings_tables if table in query]
        if unexpected:
            raise AssertionError(
                f"{label} unexpectedly used direct Withings tables: {unexpected}"
            )
        decision = inspect_query_safety(query, "local-smoke-user")
        if not decision["is_safe"]:
            raise AssertionError(f"{label} generated unsafe KQL: {decision}")
        return query

    assert_multi_metric_evidence_query(
        cross_metric_query,
        label="Longest-sleep-day step lookup",
        metrics=("total_sleep_time", "steps"),
        source_tables=("SensorSleepNights", "SensorActivityDaily"),
    )

    for mixed_label, mixed_metrics, mixed_tables in (
        (
            "Sleep and heart-rate comparison",
            ("total_sleep_time", "heart_rate"),
            ("SensorSleepNights", "SensorHealthSamples"),
        ),
        (
            "Sleep, steps, and heart-rate comparison",
            ("total_sleep_time", "steps", "heart_rate"),
            ("SensorSleepNights", "SensorActivityDaily", "SensorHealthSamples"),
        ),
    ):
        mixed_question = f"Compare {', '.join(mixed_metrics)} over the past 7 days."
        mixed_plan = sanitize_plan(
            {
                "case_type": "trend",
                "metrics": list(mixed_metrics),
                "time_range": {
                    "mode": "relative",
                    "value": 7,
                    "unit": "day",
                    "anchor": "rolling",
                },
                "granularity": "daily",
                "latest_sample": False,
                "comparison_requested": True,
            },
            mixed_question,
            {},
        )
        if mixed_plan.get("metrics") != list(mixed_metrics):
            raise AssertionError(
                f"{mixed_label} planning lost a requested metric: {mixed_plan}"
            )
        mixed_plan["timezone_iana"] = "Asia/Seoul"
        assert_multi_metric_evidence_query(
            build_deterministic_query(
                "local-smoke-user", mixed_question, mixed_plan
            ),
            label=mixed_label,
            metrics=mixed_metrics,
            source_tables=mixed_tables,
        )

    for unsupported_case_type in (
        "monitoring",
        "trend",
        "factor_analysis",
        "prescription",
    ):
        unsupported_plan = sanitize_plan(
            {
                "case_type": unsupported_case_type,
                "metrics": ["serum_cortisol"],
                "time_range": {
                    "mode": "relative",
                    "value": 1,
                    "unit": "day",
                    "anchor": "today",
                },
            },
            "What is my serum cortisol today?",
            {},
        )
        unsupported_plan["timezone_iana"] = "Asia/Seoul"
        if unsupported_plan.get("metrics") or unsupported_plan.get(
            "unsupported_metrics"
        ) != ["serum_cortisol"]:
            raise AssertionError(
                "An unsupported metric was not preserved as a bounded diagnostic: "
                f"{unsupported_plan}"
            )
        unsupported_query = build_deterministic_query(
            "local-smoke-user",
            "What is my serum cortisol today?",
            unsupported_plan,
        )
        for required_token in (
            "SensorHealthSamples-diagnostic",
            "RequestedMetrics",
            "serum_cortisol",
            "unsupported_or_unavailable_metric",
        ):
            if required_token not in str(unsupported_query or ""):
                raise AssertionError(
                    f"{unsupported_case_type} unsupported-metric query lost its "
                    f"diagnostic contract: {required_token!r}"
                )
        if "HEART_RATE" in str(unsupported_query or ""):
            raise AssertionError(
                f"{unsupported_case_type} substituted unrelated heart-rate data."
            )
        unsupported_decision = inspect_query_safety(
            str(unsupported_query or ""), "local-smoke-user"
        )
        if not unsupported_decision["is_safe"]:
            raise AssertionError(
                f"{unsupported_case_type} diagnostic KQL was rejected: "
                f"{unsupported_decision}"
            )

    # Vendor-only fields remain available solely behind explicit deployment
    # opt-in. The default path must not touch those legacy direct tables.
    optional_withings_env = "CHATBOT_ENABLE_OPTIONAL_WITHINGS_TABLES"
    previous_optional_withings = os.environ.get(optional_withings_env)
    vendor_only_plan = {
        "case_type": "monitoring",
        "metrics": ["sleep_score"],
        "time_range": {
            "mode": "relative",
            "value": 7,
            "unit": "day",
            "anchor": "rolling",
        },
        "timezone_iana": "Asia/Seoul",
    }
    try:
        os.environ[optional_withings_env] = "false"
        vendor_disabled_query = build_deterministic_query(
            "local-smoke-user", "Show my Withings sleep score.", vendor_only_plan
        )
        if vendor_disabled_query and any(
            table in vendor_disabled_query for table in direct_withings_tables
        ):
            raise AssertionError(
                "A vendor-only query bypassed the optional-Withings feature flag."
            )

        for mixed_case_type in ("trend", "factor_analysis", "prescription"):
            mixed_vendor_plan = {
                "case_type": mixed_case_type,
                "metrics": ["total_sleep_time", "sleep_score"],
                "time_range": {
                    "mode": "relative",
                    "value": 7,
                    "unit": "day",
                    "anchor": "rolling",
                },
                "timezone_iana": "Asia/Seoul",
            }
            mixed_vendor_query = build_deterministic_query(
                "local-smoke-user",
                "Show my sleep duration and Withings sleep score for the past week.",
                mixed_vendor_plan,
            )
            for required_token in (
                "SensorSleepNights",
                "Metric='total_sleep_time'",
                "Metric='sleep_score'",
                "optional-direct-vendor-disabled",
                "optional_direct_withings_disabled",
            ):
                if required_token not in str(mixed_vendor_query or ""):
                    raise AssertionError(
                        f"A {mixed_case_type} core/vendor query silently dropped "
                        f"unavailable evidence: {required_token!r}"
                    )
            if "WithingsSleep" in str(mixed_vendor_query or ""):
                raise AssertionError(
                    f"A disabled {mixed_case_type} vendor query still referenced "
                    "WithingsSleep."
                )
            mixed_vendor_decision = inspect_query_safety(
                str(mixed_vendor_query or ""), "local-smoke-user"
            )
            if not mixed_vendor_decision["is_safe"]:
                raise AssertionError(
                    f"{mixed_case_type} core/vendor diagnostic KQL failed the guard: "
                    f"{mixed_vendor_decision}"
                )

        os.environ[optional_withings_env] = "true"
        vendor_enabled_query = build_deterministic_query(
            "local-smoke-user", "Show my Withings sleep score.", vendor_only_plan
        )
        if not vendor_enabled_query or not all(
            token in vendor_enabled_query
            for token in ("WithingsSleep", "sleep_score")
        ):
            raise AssertionError(
                "The explicit opt-in path lost its vendor-only sleep-score query."
            )
        vendor_decision = inspect_query_safety(
            vendor_enabled_query,
            "local-smoke-user",
            allow_legacy_sources=True,
        )
        if not vendor_decision["is_safe"]:
            raise AssertionError(
                f"Explicit vendor-only KQL was rejected: {vendor_decision}"
            )
        for mixed_case_type in ("trend", "factor_analysis", "prescription"):
            mixed_vendor_plan = {
                "case_type": mixed_case_type,
                "metrics": ["total_sleep_time", "sleep_score"],
                "time_range": {
                    "mode": "relative",
                    "value": 7,
                    "unit": "day",
                    "anchor": "rolling",
                },
                "timezone_iana": "Asia/Seoul",
            }
            mixed_vendor_query = build_deterministic_query(
                "local-smoke-user",
                "Show my sleep duration and Withings sleep score for the past week.",
                mixed_vendor_plan,
            )
            for required_token in (
                "SensorSleepNights",
                "WithingsSleep",
                "Metric='total_sleep_time'",
                "Metric='sleep_score'",
            ):
                if required_token not in str(mixed_vendor_query or ""):
                    raise AssertionError(
                        f"An enabled {mixed_case_type} core/vendor query dropped "
                        f"requested evidence: {required_token!r}"
                    )
            mixed_vendor_decision = inspect_query_safety(
                str(mixed_vendor_query or ""),
                "local-smoke-user",
                allow_legacy_sources=True,
            )
            if not mixed_vendor_decision["is_safe"]:
                raise AssertionError(
                    f"Enabled {mixed_case_type} core/vendor KQL failed the guard: "
                    f"{mixed_vendor_decision}"
                )
    finally:
        if previous_optional_withings is None:
            os.environ.pop(optional_withings_env, None)
        else:
            os.environ[optional_withings_env] = previous_optional_withings

    question = "Show my smartphone steps for today"
    plan = sanitize_plan(
        {
            "case_type": "monitoring",
            "metrics": ["phone_steps"],
            "time_range": {
                "mode": "relative",
                "value": 1,
                "unit": "day",
                "anchor": "today",
            },
            "timezone_iana": "Asia/Seoul",
        },
        question,
        {},
    )
    safe_query = build_deterministic_query("local-smoke-user", question, plan)
    if not safe_query:
        raise AssertionError("The deterministic personal-data query builder returned no KQL.")
    safe_decision = inspect_query_safety(safe_query, "local-smoke-user")
    if not safe_decision["is_safe"]:
        raise AssertionError(f"Known-safe KQL was rejected: {safe_decision}")

    # Schema-v1 remains a production compatibility path. Straightforward
    # workout metrics must use the same bounded canonical function as schema-v2
    # instead of falling through to a diagnostic or legacy payload parser.
    for workout_metric, result_alias, aggregate_token in (
        ("workout_count", "workout_count", "count()"),
        ("workout_duration", "workout_duration_min", "sum(DurationMin)"),
        ("workout_distance", "workout_distance", "sum(Distance)"),
        ("workout_calories", "workout_calories", "sum(CaloriesKcal)"),
        (
            "workout_average_heart_rate",
            "workout_average_heart_rate",
            "avg(AverageHeartRateBpm)",
        ),
        ("workout_elevation_gain", "workout_elevation_gain", "sum(ElevationGain)"),
        (
            "workout_active_zone_minutes",
            "workout_active_zone_minutes",
            "sum(ActiveZoneMinutes)",
        ),
        ("workout_speed", "workout_speed", "avg(Speed)"),
    ):
        workout_plan = sanitize_plan(
            {
                "schema_version": 1,
                "case_type": "monitoring",
                "metrics": [workout_metric],
                "time_range": {
                    "mode": "relative",
                    "value": 7,
                    "unit": "day",
                    "anchor": "rolling",
                },
            },
            f"Show my {workout_metric} for the past 7 days.",
            {},
        )
        workout_plan["timezone_iana"] = "Asia/Seoul"
        workout_query = build_deterministic_query(
            "local-smoke-user",
            f"Show my {workout_metric} for the past 7 days.",
            workout_plan,
        )
        for token in (
            "SensorWorkoutSessions(",
            aggregate_token,
            result_alias,
            'SourceTable="SensorWorkoutSessions"',
        ):
            if token not in str(workout_query or ""):
                raise AssertionError(
                    f"Schema-v1 {workout_metric} lost {token!r}: {workout_query}"
                )
        workout_decision = inspect_query_safety(
            str(workout_query or ""), "local-smoke-user"
        )
        if not workout_decision["is_safe"]:
            raise AssertionError(
                f"Schema-v1 {workout_metric} generated unsafe KQL: {workout_decision}"
            )

    # The planner owns logical evidence selection; one trusted data profile owns
    # every physical ADX name. Keep the production mapping and representative
    # generated KQL stable while proving that a complete evaluation profile can
    # replace those names without changing metric routing or weakening safety.
    expected_production_bindings = {
        ACTIVITY_DAILY: "PhoneCumulativeDailyMV",
        ACTIVITY_HOURLY: "PhoneCumulativeHourlyMV",
        ACTIVITY_INTERVAL: "PhoneCumulativeIntervalDedupMV",
        SLEEP_NIGHTLY: "PhoneSleepNights",
        HEALTH_SAMPLES: "HealthDataFlat",
        CONVERSATION_HISTORY: "TelegramConversationLog",
        WITHINGS_ACTIVITY: "WithingsActivity",
        WITHINGS_SLEEP: "WithingsSleep",
        WITHINGS_INTRADAY: "WithingsIntraday",
        WITHINGS_SLEEP_DETAIL: "WithingsSleepDetailMinute",
        WITHINGS_WORKOUTS: "WithingsWorkouts",
        WITHINGS_NIGHT_EVENTS: "WithingsSleepNightEventSegment",
    }
    for resource_id, expected_name in expected_production_bindings.items():
        if SENSEI_ADX_V1.physical_name(resource_id) != expected_name:
            raise AssertionError(
                f"Production binding drifted for {resource_id}: "
                f"{SENSEI_ADX_V1.physical_name(resource_id)!r}"
            )

    legacy_plan = sanitize_plan(
        {
            "case_type": "monitoring",
            "metrics": ["steps"],
            "tables_priority": ["PhoneCumulativeDailyMV"],
            "time_range": {
                "mode": "relative",
                "value": 1,
                "unit": "day",
                "anchor": "today",
            },
        },
        "How many steps today?",
        {},
    )
    if legacy_plan.get("resources_priority") != [ACTIVITY_DAILY]:
        raise AssertionError(
            "A persisted physical tables_priority hint no longer migrates to "
            "the logical activity.daily resource."
        )
    explicit_v1_plan = sanitize_plan(
        {
            "schema_version": 1,
            "case_type": "monitoring",
            "metrics": ["steps"],
            "resources_priority": [ACTIVITY_DAILY],
            "time_range": {
                "mode": "relative",
                "value": 1,
                "unit": "day",
                "anchor": "today",
            },
        },
        "How many steps today?",
        {},
    )
    if (
        explicit_v1_plan.get("schema_version") != 1
        or explicit_v1_plan.get("execution_ready") is False
        or explicit_v1_plan.get("validation_errors")
    ):
        raise AssertionError(
            f"An explicit version-1 compatibility plan failed closed: {explicit_v1_plan}"
        )
    if build_deterministic_query(
        "local-smoke-user", "How many steps today?", explicit_v1_plan
    ) != build_deterministic_query(
        "local-smoke-user", "How many steps today?", legacy_plan
    ):
        raise AssertionError("Explicit schema_version 1 changed legacy KQL semantics.")

    for profile_metric in (
        "phone_birth_date",
        "phone_gender",
        "phone_blood_type",
    ):
        profile_query = build_deterministic_query(
            "local-smoke-user",
            f"Show {profile_metric}",
            {
                "case_type": "monitoring",
                "metrics": [profile_metric],
                "time_range": {
                    "mode": "relative",
                    "value": 1,
                    "unit": "day",
                    "anchor": "today",
                },
                "granularity": "daily",
                "timezone_iana": "Asia/Seoul",
            },
        )
        profile_decision = inspect_query_safety(
            str(profile_query or ""), "local-smoke-user"
        )
        if not profile_decision["is_safe"]:
            raise AssertionError(
                f"Profile metric {profile_metric} failed KQL safety: "
                f"{profile_decision}"
            )

    def _query_hash(kql: str, *, ignore_sleep_evidence_flag: bool = False) -> str:
        normalized = re.sub(r"\s+", " ", str(kql or "")).strip()
        if ignore_sleep_evidence_flag:
            # The explicit evidence flag and null-aware source labels are the
            # intentional sleep-contract additions in this revision. Removing
            # them must recover the pre-refactor KQL exactly.
            normalized = normalized.replace(
                ' | extend HasAsleepDuration=coalesce(column_ifexists("HasAsleepDuration", bool(null)), isnotnull(AsleepMin) and AsleepMin > 0)',
                "",
            )
            normalized = normalized.replace(", HasAsleepDuration", "")
            normalized = normalized.replace(
                "sleep_duration_hr=iif(HasAsleepDuration,round(AsleepMin / 60.0, 2),real(null))",
                "sleep_duration_hr=round(AsleepMin / 60.0, 2)",
            )
            normalized = normalized.replace(
                'sleep_duration_hr_source=iif(HasAsleepDuration,"curated","none")',
                'sleep_duration_hr_source="curated"',
            )
            normalized = normalized.replace(
                "sleep_efficiency=iif(HasAsleepDuration,SleepEfficiencyPct,real(null))",
                "sleep_efficiency=SleepEfficiencyPct",
            )
            normalized = normalized.replace(
                'sleep_efficiency_source=iif(HasAsleepDuration and isnotnull(SleepEfficiencyPct),"curated","none")',
                'sleep_efficiency_source="curated"',
            )
            normalized = normalized.replace(
                "phone_sleep_duration_hr=iif(HasAsleepDuration,round(AsleepMin / 60.0, 2),real(null))",
                "phone_sleep_duration_hr=round(AsleepMin / 60.0, 2)",
            )
            normalized = normalized.replace(
                'phone_sleep_duration_hr_source=iif(HasAsleepDuration,"smartphone","none")',
                'phone_sleep_duration_hr_source="smartphone"',
            )
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    production_golden_cases = (
        (
            "steps_today",
            build_deterministic_query(
                "golden-user",
                "How many steps today?",
                {
                    "case_type": "monitoring",
                    "metrics": ["steps"],
                    "time_range": {"mode": "relative", "value": 1, "unit": "day", "anchor": "today"},
                    "granularity": "daily",
                    "timezone_iana": "Asia/Seoul",
                    "latest_sample": False,
                    "comparison_requested": False,
                },
            ),
            "ae7c05b8a825229cb0cc9ba67c12066921e53bb45ffd5a85613355d13a93ac31",
            False,
        ),
        (
            "heart_latest",
            build_deterministic_query(
                "golden-user",
                "What is my current heart rate?",
                {
                    "case_type": "monitoring",
                    "metrics": ["heart_rate"],
                    "time_range": {"mode": "relative", "value": 1, "unit": "day", "anchor": "today"},
                    "granularity": "intraday",
                    "timezone_iana": "Asia/Seoul",
                    "latest_sample": True,
                    "answer_focus": "value_and_time",
                    "comparison_requested": False,
                },
            ),
            "742f271186adad18f2f672211b3e3f91af7131bda2be7fe66b528c9d77e6a060",
            False,
        ),
        (
            "sleep_and_steps",
            build_deterministic_query(
                "golden-user",
                "What were my steps on the longest sleep day?",
                {
                    "case_type": "monitoring",
                    "metrics": ["total_sleep_time", "steps"],
                    "time_range": {"mode": "relative", "value": 7, "unit": "day", "anchor": "rolling"},
                    "granularity": "daily",
                    "timezone_iana": "Asia/Seoul",
                    "latest_sample": False,
                    "comparison_requested": False,
                },
            ),
            "232163bf5bb00947aa507f4661d38fe6d81cf2ec00e8dccdf53f57d9768ad01b",
            True,
        ),
    )
    for case_name, generated, expected_hash, ignore_sleep_flag in production_golden_cases:
        if case_name == "sleep_and_steps":
            required_null_aware_sources = (
                'HasAsleepDuration=coalesce(column_ifexists("HasAsleepDuration", bool(null)), isnotnull(AsleepMin) and AsleepMin > 0)',
                'sleep_duration_hr_source=iif(HasAsleepDuration,"curated","none")',
                'sleep_efficiency_source=iif(HasAsleepDuration and isnotnull(SleepEfficiencyPct),"curated","none")',
                'phone_sleep_duration_hr_source=iif(HasAsleepDuration,"smartphone","none")',
            )
            for source_expression in required_null_aware_sources:
                if source_expression not in str(generated or ""):
                    raise AssertionError(
                        "Sleep KQL lost its null-aware evidence source: "
                        f"{source_expression}"
                    )
        actual_hash = _query_hash(
            str(generated or ""),
            ignore_sleep_evidence_flag=ignore_sleep_flag,
        )
        if actual_hash != expected_hash:
            raise AssertionError(
                f"Production KQL changed unexpectedly for {case_name}: {actual_hash}"
            )

    # Schema v2 describes the calculation rather than leaking ADX syntax into
    # the planner.  These examples exercise the operations needed by PHIA-like
    # sensor questions: aggregation, entity counts, dimension filters, ordered
    # session selection, cross-source conditions, and metric-based selection.
    typed_case_specs = (
        (
            "steps_daily_series",
            "Show my steps for the last 7 days.",
            7,
            {
                "entity": "day",
                "outputs": [{"metric": "steps", "aggregation": "none"}],
                "filters": [],
                "conditions": [],
            },
            (
                'Metric="steps"',
                'Aggregation="none"',
                "sort by EntityTime asc, EntityKey asc",
                "take 500",
                'SourceTable="SensorActivityDaily"',
            ),
        ),
        (
            "deep_sleep_percent_sum",
            "What is the sum of my deep sleep percent over the past 14 days?",
            14,
            {
                "entity": "night",
                "outputs": [
                    {"metric": "deep_sleep_percent", "aggregation": "sum"}
                ],
                "filters": [],
                "conditions": [],
            },
            (
                'MetricValue0=todouble(DeepPct)',
                'Metric="deep_sleep_percent"',
                'Aggregation="sum"',
                "sum(TargetValue)",
                'SourceTable="SensorSleepNights"',
            ),
        ),
        (
            "treadmill_session_count",
            "How many treadmill workouts did I do in the past 7 days?",
            7,
            {
                "entity": "workout_session",
                "outputs": [
                    {"entity": "workout_session", "aggregation": "count"}
                ],
                "filters": [
                    {
                        "dimension": "activity_name",
                        "operator": "eq",
                        "value": "Treadmill",
                    }
                ],
                "conditions": [],
            },
            (
                'Metric="workout_session"',
                'Aggregation="count"',
                'ActivityCode in ("TREADMILL", "RUNNING_TREADMILL", "WALKING_TREADMILL")',
                "summarize arg_max(IngestionTime, *) by EntityKey",
                "RawValue=todouble(count())",
                'SourceTable="SensorWorkoutSessions"',
            ),
        ),
        (
            "distinct_run_days",
            "On how many distinct days did I run in the past 30 days?",
            30,
            {
                "entity": "day",
                "outputs": [{"entity": "day", "aggregation": "count_distinct"}],
                "filters": [
                    {
                        "dimension": "activity_name",
                        "operator": "eq",
                        "value": "Run",
                    }
                ],
                "conditions": [],
            },
            (
                'Metric="day"',
                'Aggregation="count_distinct"',
                'ActivityCode in ("RUN", "RUNNING", "OUTDOOR_RUNNING")',
                "ActivityEligibleDays",
                "count_distinct(EntityKey)",
                'SourceTable="SensorWorkoutSessions"',
            ),
        ),
        (
            "first_four_interval_duration_sum",
            "What was the total duration of my first 4 interval workouts?",
            30,
            {
                "entity": "workout_session",
                "outputs": [
                    {"metric": "workout_duration", "aggregation": "sum"}
                ],
                "filters": [
                    {
                        "dimension": "activity_name",
                        "operator": "eq",
                        "value": "Interval Workout",
                    }
                ],
                "conditions": [],
                "selector": {"mode": "first", "n": 4},
            },
            (
                'Metric="workout_duration"',
                'Aggregation="sum"',
                'ActivityCode in ("INTERVAL_WORKOUT", "HIGH_INTENSITY_INTERVAL_TRAINING", "HIIT")',
                "WorkoutDurationMin=todouble(DurationMin)",
                "sort by EntityTime asc, EntityKey asc",
                "take 4",
                'SelectorMode="first"',
                "SelectorN=tolong(4)",
                "sum(TargetValue)",
            ),
        ),
        (
            "run_steps_on_short_sleep_days",
            "What is the sum of Run steps on days when I slept 220 minutes or less?",
            30,
            {
                "entity": "day",
                "outputs": [
                    {"metric": "workout_steps", "aggregation": "sum"}
                ],
                "filters": [
                    {
                        "dimension": "activity_name",
                        "operator": "eq",
                        "value": "Run",
                    }
                ],
                "conditions": [
                    {
                        "metric": "total_sleep_time",
                        "operator": "lte",
                        "value": 220,
                        "unit": "minute",
                    }
                ],
            },
            (
                'Metric="workout_steps"',
                'Aggregation="sum"',
                'ActivityCode in ("RUN", "RUNNING", "OUTDOOR_RUNNING")',
                "MetricValue1 <= 220",
                '"metric", "total_sleep_time", "operator", "lte"',
                'SourceTable="SensorSleepNights+SensorWorkoutSessions"',
                "sum(TargetValue)",
            ),
        ),
        (
            "steps_on_longest_sleep_day",
            "What was my step count on the day I slept the longest this past 7 days?",
            7,
            {
                "entity": "day",
                "outputs": [{"metric": "steps", "aggregation": "none"}],
                "filters": [],
                "conditions": [],
                "selector": {
                    "mode": "top",
                    "n": 1,
                    "by_metric": "total_sleep_time",
                },
            },
            (
                'Metric="steps"',
                'Aggregation="none"',
                "sort by MetricValue1 desc nulls last, EntityTime asc, EntityKey asc",
                "take 1",
                '"selector_metric", "total_sleep_time"',
                'SelectorByMetric="total_sleep_time"',
                'SourceTable="SensorSleepNights+SensorActivityDaily"',
            ),
        ),
    )
    typed_plans: dict[str, dict[str, Any]] = {}
    typed_queries: dict[str, str] = {}
    for case_name, typed_question, days, raw_computation, required_tokens in typed_case_specs:
        canonical, validation_errors = sanitize_computation(raw_computation)
        if canonical is None or validation_errors:
            raise AssertionError(
                f"Valid typed computation was rejected for {case_name}: "
                f"{validation_errors}"
            )
        canonical_again, repeated_errors = sanitize_computation(canonical)
        if canonical_again != canonical or repeated_errors:
            raise AssertionError(
                f"Typed computation sanitization is not idempotent for {case_name}."
            )
        support_errors = validate_computation_support(canonical)
        if support_errors:
            raise AssertionError(
                f"Typed compiler unexpectedly rejected {case_name}: {support_errors}"
            )

        typed_plan = sanitize_plan(
            {
                "schema_version": 2,
                "case_type": "monitoring",
                "time_range": {
                    "mode": "relative",
                    "value": days,
                    "unit": "day",
                    "anchor": "rolling",
                },
                "computation": raw_computation,
            },
            typed_question,
            {},
        )
        typed_plan["timezone_iana"] = "Asia/Seoul"
        if typed_plan.get("execution_ready") is not True:
            raise AssertionError(
                f"Valid typed plan was not execution-ready for {case_name}: "
                f"{typed_plan.get('validation_errors')}"
            )
        if typed_plan.get("computation") != canonical:
            raise AssertionError(
                f"Plan sanitization changed typed semantics for {case_name}."
            )

        typed_query = compile_computation_query("local-smoke-user", typed_plan)
        if not typed_query:
            raise AssertionError(f"Typed compiler returned no KQL for {case_name}.")
        missing_tokens = [token for token in required_tokens if token not in typed_query]
        if missing_tokens:
            raise AssertionError(
                f"Typed KQL for {case_name} lost required semantics: {missing_tokens}"
            )
        for evidence_field in (
            "BaseCoverageRows",
            "MatchedRows",
            "SelectedRows",
            "EvidenceRows",
            "ConditionEvidence",
            "SelectionEvidence",
        ):
            if evidence_field not in typed_query:
                raise AssertionError(
                    f"Typed KQL for {case_name} omitted {evidence_field}."
                )
        typed_decision = inspect_query_safety(typed_query, "local-smoke-user")
        if not typed_decision["is_safe"]:
            raise AssertionError(
                f"Typed KQL failed the safety guard for {case_name}: {typed_decision}"
            )
        routed_typed_query = build_deterministic_query(
            "local-smoke-user", typed_question, typed_plan
        )
        if routed_typed_query != typed_query:
            raise AssertionError(
                f"Schema-v2 routing did not use the typed compiler for {case_name}."
            )
        typed_plans[case_name] = typed_plan
        typed_queries[case_name] = typed_query

    aggregate_tokens = {
        "mean": "avg(TargetValue)",
        "min": "min(TargetValue)",
        "max": "max(TargetValue)",
        "median": "SortedTargetValues=array_sort_asc(TargetValues)",
        "stddev_sample": "stdev(TargetValue)",
        "stddev_population": "stdevp(TargetValue)",
    }
    for aggregation, aggregate_token in aggregate_tokens.items():
        aggregate_body = {
            "entity": "night",
            "outputs": [
                {"metric": "deep_sleep_percent", "aggregation": aggregation}
            ],
            "filters": [],
            "conditions": [],
        }
        aggregate_plan = sanitize_plan(
            {
                "schema_version": 2,
                "case_type": "monitoring",
                "time_range": {
                    "mode": "relative",
                    "value": 14,
                    "unit": "day",
                    "anchor": "rolling",
                },
                "computation": aggregate_body,
            },
            f"Calculate the {aggregation} deep sleep percent.",
            {},
        )
        aggregate_query = build_deterministic_query(
            "local-smoke-user", "Aggregate deep sleep percent.", aggregate_plan
        )
        if (
            aggregate_plan.get("execution_ready") is not True
            or not aggregate_query
            or aggregate_token not in aggregate_query
            or not inspect_query_safety(
                aggregate_query, "local-smoke-user"
            )["is_safe"]
        ):
            raise AssertionError(
                f"Typed {aggregation} aggregation was not compiled safely."
            )

    median_query = build_deterministic_query(
        "local-smoke-user",
        "Calculate the median deep sleep percent.",
        {
            **typed_plans["deep_sleep_percent_sum"],
            "computation": {
                "entity": "night",
                "outputs": [
                    {"metric": "deep_sleep_percent", "aggregation": "median"}
                ],
                "filters": [],
                "conditions": [],
            },
        },
    )
    for exact_median_token in (
        "TargetValues=make_list(TargetValue, 1048576)",
        "EvidenceRows % 2 == 1",
        "SortedTargetValues[MedianUpperIndex - 1]",
        "SortedTargetValues[MedianUpperIndex]",
    ):
        if exact_median_token not in str(median_query or ""):
            raise AssertionError(
                f"Exact median compilation lost {exact_median_token!r}."
            )
    if "percentile(" in str(median_query or "").lower():
        raise AssertionError("Typed median regressed to ADX's approximate percentile.")

    # PhoneCumulativeDailyMV keeps Unit in its grain. Typed activity relations
    # must normalize every accepted Health 13.x unit before combining rows and
    # must discard incompatible/unknown units rather than relabeling raw values.
    steps_unit_query = typed_queries["steps_on_longest_sleep_day"]
    for unit_token in (
        'ActivityUnit=toupper(trim(@"\\s+", tostring(Unit)))',
        'ActivityUnit in ("COUNT")',
        "CanonicalActivityValue=case(",
        "where isnotnull(CanonicalActivityValue)",
    ):
        if unit_token not in steps_unit_query:
            raise AssertionError(
                f"Daily activity unit normalization lost {unit_token!r}."
            )

    activity_unit_cases = {
        "distance": (
            'ActivityUnit in ("METER", "CENTIMETER", "INCH", "FOOT", "YARD", "MILE")',
            'ActivityUnit == "MILE", ActivityValue * 1609.344',
        ),
        "active": (
            'ActivityUnit in ("SECOND", "MILLISECOND", "MINUTE", "HOUR", "DAY")',
            'ActivityUnit == "HOUR", ActivityValue * 60.0',
        ),
        "calories": (
            'ActivityUnit in ("JOULE", "KILOCALORIE", "LARGE_CALORIE", "SMALL_CALORIE")',
            'ActivityUnit == "JOULE", ActivityValue / 4184.0',
        ),
    }
    for metric_name, required_unit_tokens in activity_unit_cases.items():
        unit_plan = {
            "schema_version": 2,
            "time_range": {
                "mode": "relative",
                "value": 7,
                "unit": "day",
                "anchor": "rolling",
            },
            "computation": {
                "entity": "day",
                "outputs": [{"metric": metric_name, "aggregation": "sum"}],
                "filters": [],
                "conditions": [],
            },
        }
        unit_query = compile_computation_query("local-smoke-user", unit_plan) or ""
        if not unit_query or any(
            token not in unit_query for token in required_unit_tokens
        ):
            raise AssertionError(
                f"Canonical daily-unit conversion is incomplete for {metric_name}."
            )

    workout_unit_plan = {
        "schema_version": 2,
        "time_range": {
            "mode": "relative",
            "value": 30,
            "unit": "day",
            "anchor": "rolling",
        },
        "computation": {
            "entity": "day",
            "outputs": [{"metric": "workout_distance", "aggregation": "sum"}],
            "filters": [],
            "conditions": [],
        },
    }
    workout_unit_query = compile_computation_query(
        "local-smoke-user", workout_unit_plan
    ) or ""
    # The adapter declares its unit and the shared Python compiler performs a
    # closed, backend-neutral conversion. Unknown units become null rather than
    # being silently relabeled as metres.
    for workout_unit_token in (
        "SensorWorkoutSessions(TargetUserId, StartLocal, EndLocal)",
        'toupper(tostring(DistanceUnit)) in ("METER", "METRE")',
        'toupper(tostring(DistanceUnit)) == "MILE"',
        'Unit="meter"',
    ):
        if workout_unit_token not in workout_unit_query:
            raise AssertionError(
                f"Workout canonical-unit contract lost {workout_unit_token!r}."
            )

    first_four_query = typed_queries["first_four_interval_duration_sum"]
    if first_four_query.index("ActivityCode in") > first_four_query.index("take 4"):
        raise AssertionError("The interval-workout filter was applied after first-4 selection.")
    conditional_query = typed_queries["run_steps_on_short_sleep_days"]
    if conditional_query.index("MetricValue1 <= 220") > conditional_query.index(
        "sum(TargetValue)"
    ):
        raise AssertionError("The sleep condition was applied after Run-step aggregation.")
    for dependency_case in (
        "run_steps_on_short_sleep_days",
        "steps_on_longest_sleep_day",
    ):
        base_statement = next(
            statement.strip()
            for statement in typed_queries[dependency_case].split(";")
            if statement.strip().startswith("let BaseEntities=")
        )
        if "| where" in base_statement:
            raise AssertionError(
                "Output availability was applied before condition/selector evidence "
                f"for {dependency_case}."
            )
    if 'ResultStatus=iif(isnotnull(todouble(MetricValue0)), "ok", "no_metric_evidence")' not in typed_queries[
        "steps_on_longest_sleep_day"
    ]:
        raise AssertionError(
            "A selected day with no requested value cannot expose no-evidence status."
        )

    if guard_decision(
        {
            "guard_status": "repair",
            "plan": typed_plans["run_steps_on_short_sleep_days"],
        }
    ) != "produce_sensor_result":
        raise AssertionError("A typed compiler query was allowed into model KQL repair.")
    direct_repair_result = repair_sensor_query(
        {
            "plan": typed_plans["run_steps_on_short_sleep_days"],
            "kql_query": typed_queries["run_steps_on_short_sleep_days"],
            "error": "synthetic guard rejection",
        }
    )
    if (
        direct_repair_result.get("guard_status") != "error"
        or direct_repair_result.get("execution_status") != "blocked"
    ):
        raise AssertionError("A direct typed-query repair call did not fail closed.")
    if query_generation_decision({"kql_query": None}) != "produce_sensor_result":
        raise AssertionError("An empty compiler result was allowed into the KQL guard.")
    typed_retry_candidates = _build_retry_candidates(
        primary_query="print 1",
        user_id="local-smoke-user",
        question="Rebuild this exact typed calculation.",
        plan=typed_plans["run_steps_on_short_sleep_days"],
    )
    if (
        len(typed_retry_candidates) != 1
        or typed_retry_candidates[0][0] != "deterministic fallback"
        or typed_retry_candidates[0][1]
        != typed_queries["run_steps_on_short_sleep_days"]
    ):
        raise AssertionError(
            "A typed ADX retry was replaced by or mixed with a legacy fallback."
        )
    typed_coverage = _coverage_context(
        typed_plans["run_steps_on_short_sleep_days"],
        [
            {
                "Metric": "workout_steps",
                "BaseCoverageRows": 12,
                "MatchedRows": 3,
                "SelectedRows": 3,
                "EvidenceRows": 3,
                "ResultStatus": "ok",
            }
        ],
    )
    if typed_coverage != {
        "policy": "typed_computation_evidence",
        "entity": "day",
        "base_coverage_rows": 12,
        "matched_rows": 3,
        "selected_rows": 3,
        "evidence_rows_by_metric": {"workout_steps": 3},
        "result_status_by_metric": {"workout_steps": "ok"},
    }:
        raise AssertionError(
            f"Typed aggregate coverage was interpreted as missing dates: {typed_coverage}"
        )

    series_coverage_plan = {
        **typed_plans["steps_daily_series"],
        "time_range": {
            "mode": "absolute",
            "value": 7,
            "unit": "day",
            "anchor": "absolute",
            "start_date": "2026-09-15",
            "end_date": "2026-09-21",
        },
    }
    series_coverage = _coverage_context(
        series_coverage_plan,
        [
            {
                "Metric": "steps",
                "Aggregation": "none",
                "Period": "2026-09-15T00:00:00",
                "Value": 8123,
                "BaseCoverageRows": 2,
                "MatchedRows": 2,
                "SelectedRows": 2,
                "EvidenceRows": 1,
                "ResultStatus": "ok",
            },
            {
                "Metric": "steps",
                "Aggregation": "none",
                "Period": "2026-09-17T00:00:00",
                "Value": 0,
                "BaseCoverageRows": 2,
                "MatchedRows": 2,
                "SelectedRows": 2,
                "EvidenceRows": 1,
                "ResultStatus": "ok",
            },
        ],
    )
    if (
        series_coverage.get("evidence_rows_by_metric") != {"steps": 2}
        or series_coverage.get("observed_dates_by_metric")
        != {"steps": ["2026-09-15", "2026-09-17"]}
        or series_coverage.get("missing_dates_by_metric", {}).get("steps")
        != [
            "2026-09-16",
            "2026-09-18",
            "2026-09-19",
            "2026-09-20",
            "2026-09-21",
        ]
        or series_coverage.get("missing_day_policy")
        != "do_not_fill_missing_days_with_zero"
    ):
        raise AssertionError(
            "Typed daily-series coverage no longer distinguishes missing dates "
            f"from a recorded zero: {series_coverage}"
        )

    # Period-only follow-ups retain the exact typed operation while replacing
    # only the requested time window.
    previous_typed_plan = typed_plans["deep_sleep_percent_sum"]
    inherited_body, inheritance_errors = sanitize_computation(
        None,
        last_computation=previous_typed_plan,
        inherit=True,
    )
    if inheritance_errors or inherited_body != previous_typed_plan["computation"]:
        raise AssertionError("Direct typed-computation inheritance changed semantics.")
    typed_followup_plan = sanitize_plan(
        {
            "is_followup": True,
            "time_range": {
                "mode": "relative",
                "value": 30,
                "unit": "day",
                "anchor": "rolling",
            },
        },
        "What about the past 30 days?",
        previous_typed_plan,
    )
    if (
        typed_followup_plan.get("execution_ready") is not True
        or typed_followup_plan.get("computation")
        != previous_typed_plan["computation"]
        or typed_followup_plan.get("time_range", {}).get("value") != 30
    ):
        raise AssertionError(
            f"Typed period follow-up did not inherit safely: {typed_followup_plan}"
        )

    # A complete new topic must override a previous v2 calculation even when
    # the planner correctly marks the conversational turn as a follow-up.
    explicit_v1_after_v2 = sanitize_plan(
        {
            "schema_version": 1,
            "case_type": "monitoring",
            "is_followup": True,
            "metrics": ["heart_rate"],
            "resources_priority": [HEALTH_SAMPLES],
            "time_range": {
                "mode": "relative",
                "value": 1,
                "unit": "day",
                "anchor": "yesterday",
            },
        },
        "What about my heart rate yesterday?",
        previous_typed_plan,
    )
    if (
        explicit_v1_after_v2.get("schema_version") != 1
        or explicit_v1_after_v2.get("metrics") != ["heart_rate"]
        or "computation" in explicit_v1_after_v2
        or explicit_v1_after_v2.get("execution_ready") is False
    ):
        raise AssertionError(
            "A prior v2 calculation leaked into an explicit v1 topic change: "
            f"{explicit_v1_after_v2}"
        )

    oversized_v2_plan = sanitize_plan(
        {
            "schema_version": 2,
            "case_type": "monitoring",
            "time_range": {
                "mode": "relative",
                "value": 3661,
                "unit": "day",
                "anchor": "rolling",
            },
            "computation": typed_case_specs[0][3],
        },
        "Calculate this over the requested historical window.",
        {},
    )
    if (
        oversized_v2_plan.get("execution_ready") is not False
        or not any(
            error.get("code") == "time_range_too_large"
            for error in oversized_v2_plan.get("validation_errors", [])
        )
    ):
        raise AssertionError(
            f"An oversized typed time window did not fail closed: {oversized_v2_plan}"
        )

    # Calendar anchors have a single unit-specific meaning.  Invalid pairings
    # and unknown anchors must not drift into the compiler's rolling fallback.
    for invalid_time_range in (
        {
            "mode": "relative",
            "value": 1,
            "unit": "day",
            "anchor": "last_week",
        },
        {
            "mode": "relative",
            "value": 7,
            "unit": "day",
            "anchor": "unknown_window",
        },
        {
            "mode": "relative",
            "value": True,
            "unit": "day",
            "anchor": "rolling",
        },
        {
            "mode": "relative",
            "value": 1.5,
            "unit": "day",
            "anchor": "rolling",
        },
        {
            "mode": "relative",
            "value": "0" * 5000 + "1",
            "unit": "day",
            "anchor": "rolling",
        },
        {
            "mode": "unexpected",
            "value": 1,
            "unit": "day",
            "anchor": "today",
        },
        {
            "mode": "relative",
            "value": 1,
            "unit": "day",
            "anchor": "absolute",
            "start_date": "2026-09-01",
            "end_date": "2026-09-01",
        },
        {
            "mode": "absolute",
            "value": 1,
            "unit": "day",
            "anchor": "absolute",
            "start_date": "2026-09-01",
        },
    ):
        invalid_anchor_plan = sanitize_plan(
            {
                "schema_version": 2,
                "case_type": "monitoring",
                "time_range": invalid_time_range,
                "computation": typed_case_specs[0][3],
            },
            "Calculate this over the requested historical window.",
            {},
        )
        if invalid_anchor_plan.get("execution_ready") is not False:
            raise AssertionError(
                "An invalid typed time range did not fail closed: "
                f"{invalid_anchor_plan}"
            )
        direct_invalid_plan = {
            "schema_version": 2,
            "time_range": invalid_time_range,
            "computation": typed_case_specs[0][3],
        }
        if compile_computation_query("local-smoke-user", direct_invalid_plan) is not None:
            raise AssertionError(
                "The direct typed compiler bypassed time-range validation: "
                f"{invalid_time_range}"
            )

    valid_v2_time_ranges = (
        {"mode": "relative", "value": 7, "unit": "day", "anchor": "rolling"},
        {"mode": "relative", "value": 2, "unit": "week", "anchor": "rolling"},
        {"mode": "relative", "value": 2, "unit": "month", "anchor": "rolling"},
        {"mode": "relative", "value": 1, "unit": "day", "anchor": "today"},
        {"mode": "relative", "value": 1, "unit": "day", "anchor": "yesterday"},
        {
            "mode": "relative",
            "value": 1,
            "unit": "day",
            "anchor": "day_before_yesterday",
        },
        {"mode": "relative", "value": 1, "unit": "week", "anchor": "this_week"},
        {"mode": "relative", "value": 1, "unit": "week", "anchor": "last_week"},
        {"mode": "relative", "value": 2, "unit": "week", "anchor": "last_week"},
        {"mode": "relative", "value": 1, "unit": "month", "anchor": "this_month"},
        {"mode": "relative", "value": 1, "unit": "month", "anchor": "last_month"},
        {
            "mode": "absolute",
            "value": 3,
            "unit": "day",
            "anchor": "absolute",
            "start_date": "2026-09-03",
            "end_date": "2026-09-01",
        },
    )
    for valid_time_range in valid_v2_time_ranges:
        valid_time_plan = sanitize_plan(
            {
                "schema_version": 2,
                "case_type": "monitoring",
                "time_range": valid_time_range,
                "computation": typed_case_specs[0][3],
            },
            "Calculate this over the requested historical window.",
            {},
        )
        if valid_time_plan.get("execution_ready") is not True:
            raise AssertionError(
                f"A valid typed time range was rejected: {valid_time_plan}"
            )
        if compile_computation_query("local-smoke-user", valid_time_plan) is None:
            raise AssertionError(
                f"The typed compiler rejected a valid time range: {valid_time_range}"
            )

    # Long-form v2 results are bounded per declared output so one alphabetically
    # earlier metric cannot consume the complete global result allowance.
    multi_series_plan = {
        "schema_version": 2,
        "computation": {
            "entity": "day",
            "outputs": [
                {"metric": "steps", "aggregation": "none"},
                {"metric": "total_sleep_time", "aggregation": "none"},
            ],
        },
    }
    multi_series_rows = [
        {"Metric": metric, "Aggregation": "none", "Value": index}
        for metric in ("steps", "total_sleep_time")
        for index in range(30)
    ]
    if len(trim_records_for_plan(multi_series_rows, multi_series_plan, 40)) != 60:
        raise AssertionError("Typed result trimming starved one of two 30-row outputs.")
    oversized_series_rows = [
        {"Metric": metric, "Aggregation": "none", "Value": index}
        for metric in ("steps", "total_sleep_time")
        for index in range(60)
    ]
    bounded_series = trim_records_for_plan(
        oversized_series_rows, multi_series_plan, 40
    )
    retained_by_metric = {
        metric: sum(1 for row in bounded_series if row.get("Metric") == metric)
        for metric in ("steps", "total_sleep_time")
    }
    if retained_by_metric != {"steps": 40, "total_sleep_time": 40}:
        raise AssertionError(
            f"Typed per-output result bounds drifted: {retained_by_metric}"
        )

    # Model output cannot add physical names or executable fragments.  Future
    # schema versions and structurally valid operations that this compiler does
    # not yet implement must also stop before query generation.
    injected_computation = {
        **typed_case_specs[0][3],
        "kql": 'HealthDataFlat | take 1; .drop table HealthDataFlat',
    }
    rejected_injection, injection_errors = sanitize_computation(
        {"schema_version": 2, "computation": injected_computation}
    )
    if rejected_injection is not None or not any(
        error.get("code") == "unknown_field" for error in injection_errors
    ):
        raise AssertionError("Executable planner text was not rejected by schema v2.")
    future_body, future_errors = sanitize_computation(
        {"schema_version": 99, "computation": typed_case_specs[0][3]}
    )
    if future_body is not None or not any(
        error.get("code") == "unsupported_schema_version"
        for error in future_errors
    ):
        raise AssertionError("A future typed-plan version did not fail closed.")

    unsupported_typed_bodies = (
        {
            "entity": "day",
            "outputs": [{"metric": "steps", "aggregation": "mean"}],
            "filters": [],
            "conditions": [],
            "group_by": ["day"],
        },
        {
            "entity": "sample",
            "outputs": [{"metric": "heart_rate", "aggregation": "mean"}],
            "filters": [],
            "conditions": [],
        },
    )
    for unsupported_body in unsupported_typed_bodies:
        canonical, schema_errors = sanitize_computation(unsupported_body)
        if canonical is None or schema_errors:
            raise AssertionError(
                "A public-schema operation could not reach compiler capability gating."
            )
        compiler_errors = validate_computation_support(canonical)
        if not compiler_errors:
            raise AssertionError(
                f"Unsupported typed operation was reported as compilable: {canonical}"
            )
        unsupported_plan = sanitize_plan(
            {
                "schema_version": 2,
                "case_type": "monitoring",
                "time_range": {
                    "mode": "relative",
                    "value": 7,
                    "unit": "day",
                    "anchor": "rolling",
                },
                "computation": unsupported_body,
            },
            "Evaluate this typed operation.",
            {},
        )
        if (
            unsupported_plan.get("execution_ready") is not False
            or not unsupported_plan.get("validation_errors")
            or compile_computation_query("local-smoke-user", unsupported_plan)
            is not None
        ):
            raise AssertionError(
                f"Unsupported typed operation did not fail closed: {unsupported_plan}"
            )

    evaluation_overrides = {
        logical_id: f"EvalResource{index:02d}"
        for index, logical_id in enumerate(SENSEI_ADX_V1.bindings, start=1)
    }
    evaluation_profile = with_overrides(
        "local_evaluation",
        evaluation_overrides,
    )
    try:
        with_overrides("unsafe_evaluation", {ACTIVITY_DAILY: "Bad.Name"})
    except ValueError:
        pass
    else:
        raise AssertionError(
            "A non-identifier physical resource name was accepted into a data profile."
        )
    production_names = SENSEI_ADX_V1.physical_names()
    with use_data_profile(evaluation_profile):
        for case_name, typed_plan in typed_plans.items():
            evaluation_typed_query = compile_computation_query(
                "local-smoke-user", typed_plan
            )
            if not evaluation_typed_query:
                raise AssertionError(
                    f"Evaluation profile produced no typed KQL for {case_name}."
                )
            leaked_names = [
                name for name in production_names if name in evaluation_typed_query
            ]
            if leaked_names:
                raise AssertionError(
                    f"Typed evaluation KQL leaked production resources for "
                    f"{case_name}: {leaked_names}"
                )
            evaluation_typed_decision = inspect_query_safety(
                evaluation_typed_query,
                "local-smoke-user",
            )
            if not evaluation_typed_decision["is_safe"]:
                raise AssertionError(
                    f"Typed evaluation KQL failed its profile-derived guard for "
                    f"{case_name}: {evaluation_typed_decision}"
                )

        evaluation_queries = (
            build_deterministic_query(
                "local-smoke-user", "How many steps today?", legacy_plan
            ),
            build_deterministic_query(
                "local-smoke-user",
                "Show my sleep for the last 7 days",
                {
                    "case_type": "monitoring",
                    "metrics": ["total_sleep_time"],
                    "time_range": {"mode": "relative", "value": 7, "unit": "day", "anchor": "rolling"},
                    "granularity": "daily",
                    "latest_sample": False,
                    "comparison_requested": False,
                },
            ),
            build_deterministic_query(
                "local-smoke-user",
                "What is my current heart rate?",
                {
                    "case_type": "monitoring",
                    "metrics": ["heart_rate"],
                    "time_range": {"mode": "relative", "value": 1, "unit": "day", "anchor": "today"},
                    "granularity": "intraday",
                    "latest_sample": True,
                    "comparison_requested": False,
                },
            ),
        )
        for evaluation_query in evaluation_queries:
            if not evaluation_query:
                raise AssertionError("Evaluation profile produced no KQL.")
            if any(name in evaluation_query for name in production_names):
                raise AssertionError(
                    "An evaluation-profile query leaked a production physical name."
                )
            evaluation_decision = inspect_query_safety(
                evaluation_query,
                "local-smoke-user",
            )
            if not evaluation_decision["is_safe"]:
                raise AssertionError(
                    "Evaluation-profile KQL failed its profile-derived guard: "
                    f"{evaluation_decision}"
                )

        from chatbot.entrypoint import _extract_kql_tables
        from chatbot.nodes.d1_sensor_data.generate_sensor_query import ADX_SCHEMA_CONTEXT

        combined_evaluation_kql = "\n".join(
            str(query or "") for query in evaluation_queries
        )
        extracted_evaluation_sources = set(
            _extract_kql_tables(combined_evaluation_kql)
        )
        for resource_id in (
            ACTIVITY_DAILY,
            SLEEP_NIGHTLY,
            HEALTH_SAMPLES,
        ):
            expected_source = physical_name(resource_id)
            if expected_source not in extracted_evaluation_sources:
                raise AssertionError(
                    "KQL telemetry did not recognize the active profile source: "
                    f"{expected_source}"
                )

        rendered_schema = ADX_SCHEMA_CONTEXT.format(user_id="local-smoke-user")
        if any(name in rendered_schema for name in evaluation_overrides.values()):
            raise AssertionError(
                "The canonical KQL schema unexpectedly followed a test-only "
                "physical profile override."
            )
        for function_name in expected_sensor_functions.values():
            if function_name not in rendered_schema:
                raise AssertionError(
                    "The canonical KQL schema omitted deployed Sensor function "
                    f"{function_name}."
                )
        if inspect_query_safety(safe_query, "local-smoke-user")["is_safe"]:
            raise AssertionError(
                "The evaluation-profile guard accepted a production-bound query."
            )

        evaluation_optional_setting = os.environ.get(optional_withings_env)
        try:
            os.environ[optional_withings_env] = "true"
            evaluation_availability_kql = build_metric_availability_kql(
                "local-smoke-user"
            )
        finally:
            if evaluation_optional_setting is None:
                os.environ.pop(optional_withings_env, None)
            else:
                os.environ[optional_withings_env] = evaluation_optional_setting
        for resource_id in (HEALTH_SAMPLES, *(
            WITHINGS_ACTIVITY,
            WITHINGS_SLEEP,
            WITHINGS_INTRADAY,
            WITHINGS_SLEEP_DETAIL,
            WITHINGS_WORKOUTS,
            WITHINGS_NIGHT_EVENTS,
        )):
            if physical_name(resource_id) not in evaluation_availability_kql:
                raise AssertionError(
                    "Metric availability did not follow the active evaluation "
                    f"profile for {resource_id}."
                )
        if any(name in evaluation_availability_kql for name in production_names):
            raise AssertionError(
                "Evaluation availability KQL leaked a production physical name."
            )

    # A known table anywhere in the text must not allow an unrelated direct
    # source to bypass the table allowlist.
    mixed_source_query = (
        'SensorHealthSamples("local-smoke-user", ago(1d), now())'
        ' | where UserId == "local-smoke-user"'
        " | where DateFrom >= ago(1d) and DateFrom < now()"
        " | union SecretTable | take 10"
    )
    mixed_source_decision = inspect_query_safety(
        mixed_source_query,
        "local-smoke-user",
    )
    if mixed_source_decision["is_safe"] or "SecretTable" not in str(
        mixed_source_decision.get("reason")
    ):
        raise AssertionError(
            "A query mixing an allowlisted table with an unknown source was accepted: "
            f"{mixed_source_decision}"
        )

    # A global UserId/time predicate on the first branch must not implicitly
    # authorize a second, direct allowlisted source branch.
    unscoped_allowed_branch_query = (
        'let TargetUserId="local-smoke-user"; '
        "let StartLocal=ago(1d); let EndLocal=now(); "
        "SensorHealthSamples(TargetUserId, StartLocal, EndLocal)"
        " | where UserId == TargetUserId"
        " | where DateFrom >= StartLocal and DateFrom < EndLocal"
        " | union SensorActivityDaily(TargetUserId, StartLocal, EndLocal) | take 10"
    )
    unscoped_allowed_branch_decision = inspect_query_safety(
        unscoped_allowed_branch_query,
        "local-smoke-user",
    )
    unscoped_allowed_reason = str(unscoped_allowed_branch_decision.get("reason"))
    if unscoped_allowed_branch_decision["is_safe"] or not all(
        token in unscoped_allowed_reason
        for token in ("SensorActivityDaily", "UserId filter")
    ):
        raise AssertionError(
            "An unscoped second allowlisted source branch was accepted: "
            f"{unscoped_allowed_branch_decision}"
        )

    # Scope/source syntax must be proven from executable KQL, never from a
    # comment, column alias, ambient optional-source flag, or unrelated branch.
    adversarial_guard_queries = {
        "cross-user OR scope": (
            'let TargetUserId="victim"; let StartLocal=datetime(2026-01-01); '
            'let EndLocal=datetime(2026-01-02); '
            'SensorHealthSamples(TargetUserId, StartLocal, EndLocal) '
            '| where UserId == "local-smoke-user" or 1 == 1 '
            '| where DateFrom >= StartLocal and DateFrom < EndLocal '
            '| summarize Value=count()'
        ),
        "comment-spoofed user binding": (
            'let TargetUserId="victim";\n'
            'let Dummy=1 // let TargetUserId="local-smoke-user";\n;\n'
            'SensorHealthSamples(TargetUserId, datetime(2026-01-01), datetime(2026-01-02)) '
            '| where UserId == "local-smoke-user" '
            '| where DateFrom >= datetime(2026-01-01) and DateFrom < datetime(2026-01-02) '
            '| summarize Value=count()'
        ),
        "quoted raw entity": (
            "['PHIASummary'] | where DateFrom >= datetime(2026-01-01) "
            "and DateFrom < datetime(2026-01-02) "
            '| extend SensorHealthSamples=1, UserId="local-smoke-user" | take 1'
        ),
        "local tabular function": (
            'let TargetUserId="local-smoke-user"; '
            'let StartLocal=datetime(2026-01-01); let EndLocal=datetime(2026-01-02); '
            'let Safe=SensorHealthSamples(TargetUserId, StartLocal, EndLocal) '
            '| where UserId == TargetUserId '
            '| where DateFrom >= StartLocal and DateFrom < EndLocal; '
            'let Evil=(){ SecretTable | summarize count() }; Safe | take 1'
        ),
        "parenthesized hidden source alias": (
            'let TargetUserId="local-smoke-user"; '
            'let StartLocal=datetime(2026-01-01); let EndLocal=datetime(2026-01-02); '
            'let Hidden=(SecretUnlisted); '
            'SensorHealthSamples(TargetUserId, StartLocal, EndLocal) '
            '| where UserId == TargetUserId '
            '| where DateFrom >= StartLocal and DateFrom < EndLocal '
            '| join kind=leftouter (Hidden) on UserId | summarize Value=count()'
        ),
        "materialized hidden source alias": (
            'let TargetUserId="local-smoke-user"; '
            'let StartLocal=datetime(2026-01-01); let EndLocal=datetime(2026-01-02); '
            'let Hidden=materialize(SecretUnlisted); '
            'SensorHealthSamples(TargetUserId, StartLocal, EndLocal) '
            '| where UserId == TargetUserId '
            '| where DateFrom >= StartLocal and DateFrom < EndLocal '
            '| join kind=leftouter (Hidden) on UserId | summarize Value=count()'
        ),
        "scalar hidden source": (
            'let TargetUserId="local-smoke-user"; '
            'let StartLocal=datetime(2026-01-01); let EndLocal=datetime(2026-01-02); '
            'SensorHealthSamples(TargetUserId, StartLocal, EndLocal) '
            '| where UserId == TargetUserId '
            '| where DateFrom >= StartLocal and DateFrom < EndLocal '
            '| summarize Value=count(), Leaked=toscalar(SecretUnlisted)'
        ),
        "view hidden source": (
            'let TargetUserId="local-smoke-user"; '
            'let StartLocal=datetime(2026-01-01); let EndLocal=datetime(2026-01-02); '
            'let Hidden=view () { SecretUnlisted }; '
            'SensorHealthSamples(TargetUserId, StartLocal, EndLocal) '
            '| where UserId == TargetUserId '
            '| where DateFrom >= StartLocal and DateFrom < EndLocal '
            '| join kind=leftouter (Hidden) on UserId | summarize Value=count()'
        ),
        "nested cross-user function": (
            'let TargetUserId="local-smoke-user"; '
            'let StartLocal=datetime(2026-01-01); let EndLocal=datetime(2026-01-02); '
            'SensorHealthSamples(TargetUserId, StartLocal, EndLocal) '
            '| where UserId == TargetUserId '
            '| where DateFrom >= StartLocal and DateFrom < EndLocal '
            '| summarize Value=count(), Leaked=toscalar('
            'SensorHealthSamples("victim", datetime(2026-01-01), datetime(2026-01-02)) '
            '| summarize count())'
        ),
        "multi-century function window": (
            'let TargetUserId="local-smoke-user"; '
            'let StartLocal=datetime(1900-01-01); let EndLocal=datetime(2100-01-01); '
            'SensorHealthSamples(TargetUserId, StartLocal, EndLocal) '
            '| where UserId == TargetUserId '
            '| where DateFrom >= StartLocal and DateFrom < EndLocal '
            '| summarize Value=count()'
        ),
        "additive overlong function window": (
            'let TargetUserId="local-smoke-user"; '
            'let StartLocal=now() - 3000d; let EndLocal=now() + 1000d; '
            'SensorHealthSamples(TargetUserId, StartLocal, EndLocal) '
            '| where UserId == TargetUserId '
            '| where DateFrom >= StartLocal and DateFrom < EndLocal '
            '| summarize Value=count()'
        ),
        "unsupported datetime-add unit": (
            'let TargetUserId="local-smoke-user"; '
            'let StartLocal=datetime_add("year", -1, now()); let EndLocal=now(); '
            'SensorHealthSamples(TargetUserId, StartLocal, EndLocal) '
            '| where UserId == TargetUserId '
            '| where DateFrom >= StartLocal and DateFrom < EndLocal '
            '| summarize Value=count()'
        ),
        "wrong function arity": (
            'let TargetUserId="local-smoke-user"; '
            'let StartLocal=datetime(2026-01-01); let EndLocal=datetime(2026-01-02); '
            'SensorHealthSamples(TargetUserId, StartLocal) '
            '| where UserId == TargetUserId '
            '| where DateFrom >= StartLocal and DateFrom < EndLocal '
            '| summarize Value=count()'
        ),
        "unused summary beside unbounded final stream": (
            'let TargetUserId="local-smoke-user"; '
            'let StartLocal=datetime(2026-01-01); let EndLocal=datetime(2026-01-02); '
            'let Dummy=SensorHealthSamples(TargetUserId, StartLocal, EndLocal) '
            '| where UserId == TargetUserId '
            '| where DateFrom >= StartLocal and DateFrom < EndLocal '
            '| summarize Value=count(); '
            'SensorHealthSamples(TargetUserId, StartLocal, EndLocal) '
            '| where UserId == TargetUserId '
            '| where DateFrom >= StartLocal and DateFrom < EndLocal '
            '| project UserId, DateFrom, ValueNumber'
        ),
    }
    for attack_name, attack_query in adversarial_guard_queries.items():
        attack_decision = inspect_query_safety(
            attack_query,
            "local-smoke-user",
        )
        if attack_decision["is_safe"]:
            raise AssertionError(
                f"Guard accepted {attack_name}: {attack_decision}"
            )

    previous_guard_optional = os.environ.get(optional_withings_env)
    try:
        os.environ[optional_withings_env] = "true"
        ambient_vendor_query = (
            'WithingsSleep | where AppUserId == "local-smoke-user" '
            '| where SleepEndLocal >= ago(1d) and SleepEndLocal < now() '
            '| summarize Value=count()'
        )
        if inspect_query_safety(
            ambient_vendor_query,
            "local-smoke-user",
        )["is_safe"]:
            raise AssertionError(
                "Ambient optional-vendor configuration expanded the canonical D1 guard."
            )
    finally:
        if previous_guard_optional is None:
            os.environ.pop(optional_withings_env, None)
        else:
            os.environ[optional_withings_env] = previous_guard_optional

    for wrong_arguments in (
        ("TargetUserId",),
        ("TargetUserId", "StartLocal", "EndLocal", "Unexpected"),
    ):
        try:
            tabular_call(HEALTH_SAMPLES, *wrong_arguments)
        except ValueError:
            pass
        else:
            raise AssertionError(
                "tabular_call accepted a non-contract function signature."
            )

    # Project changes columns but does not cap raw result cardinality.
    project_only_query = (
        'SensorHealthSamples("local-smoke-user", ago(1d), now())'
        ' | where UserId == "local-smoke-user"'
        " | where DateFrom >= ago(1d) and DateFrom < now()"
        " | project DateFrom, ValueNumber"
    )
    project_only_decision = inspect_query_safety(
        project_only_query,
        "local-smoke-user",
    )
    if project_only_decision["is_safe"]:
        raise AssertionError("A project-only unbounded result was accepted.")

    # Scalar lets are not table names, while a let-bound tabular expression is
    # a supported way to compose deterministic KQL.
    let_bound_query = (
        'let TargetUserId="local-smoke-user";\n'
        "let StartLocal=startofday(now());\n"
        "let EndLocal=StartLocal+1d;\n"
        "let scoped=SensorHealthSamples(TargetUserId, StartLocal, EndLocal)"
        " | where UserId == TargetUserId"
        " | where DateFrom >= StartLocal and DateFrom < EndLocal;\n"
        "scoped | summarize samples=count()"
    )
    let_bound_decision = inspect_query_safety(let_bound_query, "local-smoke-user")
    if not let_bound_decision["is_safe"]:
        raise AssertionError(
            f"Known-safe let-bound KQL was rejected: {let_bound_decision}"
        )

    # SensorSleepNights is explicitly windowed and emits at most one selected
    # main-sleep row per wake-up day, so its projected nightly result is bounded
    # without a separate take operator.
    phone_sleep_query = (
        'let TargetUserId="local-smoke-user";\n'
        "let StartLocal=startofday(now())-7d;\n"
        "let EndLocal=startofday(now())+1d;\n"
        "SensorSleepNights(TargetUserId, StartLocal, EndLocal)"
        " | where UserId == TargetUserId"
        " | where SleepEndLocal >= StartLocal and SleepEndLocal < EndLocal"
        " | project SleepDay, AsleepMin"
    )
    phone_sleep_decision = inspect_query_safety(
        phone_sleep_query,
        "local-smoke-user",
    )
    if not phone_sleep_decision["is_safe"]:
        raise AssertionError(
            f"Bounded SensorSleepNights KQL was rejected: {phone_sleep_decision}"
        )

    unsafe_decision = inspect_query_safety(".drop table HealthDataFlat", "local-smoke-user")
    if unsafe_decision["is_safe"]:
        raise AssertionError("Known-destructive KQL was accepted.")

    print(
        json.dumps(
            {
                "status": "ok",
                "azure_functions": len(functions),
                "graph_nodes": len(EXPECTED_GRAPH_NODES),
                "prompt_resources": len(prompt_files),
                "role_prompt_resources": role_prompt_count,
                "general_chat_prompt_contract": "passed",
                "latest_heart_rate_followups": "passed",
                "typed_computation_plans": "passed",
                "guard_sensor_query": "allowlist, result-bound, safe, and destructive cases passed",
            },
            indent=2,
        )
    )
    return 0


async def _ask(*, user_id: str, chat_id: str, message: str, reply_to_text: str = "") -> int:
    from chatbot.entrypoint import handle_user_message

    payload: dict[str, Any] = {}
    if reply_to_text.strip():
        payload.update({"reply_to_text": reply_to_text.strip(), "reply_to_role": "assistant"})
    response = await handle_user_message(
        app_user_id=user_id,
        telegram_chat_id=chat_id,
        message_text=message,
        inbound_payload=payload,
    )
    details = response.payload if isinstance(response.payload, dict) else {}
    print(
        json.dumps(
            {
                "text": response.text,
                "route": response.route,
                "intent": response.intent,
                "execution_status": response.execution_status,
                "kql_tables": response.kql_tables,
                "adx_row_count": response.adx_row_count,
                "guard_status": details.get("guard_status"),
                "error": details.get("error"),
                "plan": details.get("plan"),
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )
    return 1 if response.execution_status == "error" else 0


def _system_preview(target_date: str, current: float, median: float) -> int:
    from chatbot.proactive.system_steps_alert import compose_steps_alert_message

    target = dt.date.fromisoformat(target_date)
    row = {
        "CurrentValue": current,
        "MedianValue": median,
        "WindowStartLocal": (target - dt.timedelta(days=28)).isoformat(),
        "WindowEndExclusiveLocal": target.isoformat(),
    }
    print(compose_steps_alert_message(row, target))
    return 0


async def _system_reply(args: argparse.Namespace) -> int:
    if not args.confirm_write:
        raise SystemExit("system-reply writes conversation state; rerun with --confirm-write.")

    from chatbot.episode_service import suspend_active_episode_for_alert, thread_id_for_chat
    from chatbot.services.memory import create_operational_storage

    chat_id = args.chat_id or args.user_id
    repository = create_operational_storage(fail_soft=False)
    saved = suspend_active_episode_for_alert(
        args.user_id,
        thread_id_for_chat(chat_id),
        alert_context={"kind": "local_system_reply_test", "metric_type": "STEPS"},
        alert_message_text=args.alert_text,
        repository=repository,
    )
    if not saved:
        raise RuntimeError("The synthetic system episode could not be saved.")
    return await _ask(
        user_id=args.user_id,
        chat_id=chat_id,
        message=args.message,
        reply_to_text=args.alert_text,
    )


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    args = _parser().parse_args()
    _load_environment()

    command = args.command or "smoke"
    if command == "smoke":
        return _smoke()
    if command == "ask":
        print("LIVE: this command may call Azure OpenAI, ADX, and Table Storage.", file=sys.stderr)
        return asyncio.run(
            _ask(
                user_id=args.user_id,
                chat_id=args.chat_id,
                message=args.message,
                reply_to_text=args.reply_to_text,
            )
        )
    if command == "system-preview":
        return _system_preview(args.target_date, args.current, args.median)
    if command == "system-reply":
        print("LIVE/WRITE: this command modifies configured conversation storage.", file=sys.stderr)
        return asyncio.run(_system_reply(args))
    raise AssertionError(f"Unknown command: {command}")


if __name__ == "__main__":
    raise SystemExit(main())
