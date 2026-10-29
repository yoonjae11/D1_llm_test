"""Regression checks for the shared D1 planning and KQL few-shots."""

from __future__ import annotations

import importlib
import json
import os
import re
import unittest
from datetime import date, datetime
from unittest.mock import patch

from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableLambda

from chatbot.config import _kql_generate_method
from chatbot.nodes.d1_sensor_data import daily_flat_recheck
from chatbot.nodes.common.generate_final_response import _answer_today
from chatbot.nodes.d1_sensor_data.execute_sensor_query import _coverage_context
from chatbot.nodes.d1_sensor_data.generate_sensor_query import _query_reference_clock
from chatbot.nodes.d1_sensor_data.workflow import guard_decision
from chatbot.policies.kql_generation import _kql_phone_window_for_plan
from chatbot.policies.kql_safety import inspect_query_safety
from chatbot.policies.plan_sanitization import sanitize_plan
from chatbot.policies.semantic_resources import prompt_resource_values
from chatbot.policies.time_ranges import EvaluationTimeContext, use_evaluation_time
from chatbot.policies.reactive_query_scope import reactive_query_bounds
from chatbot.prompts import load_prompt, load_role_prompt


class D1PromptContractTests(unittest.TestCase):
    def test_unverified_unit_label_is_not_a_diagnostic_basis(self):
        from chatbot.nodes.d1_sensor_data.generate_sensor_query import _workout_shape_conflicts
        observations={"workout_payload":{"status":"observed","object_rows":1,
            "fields":[{"path":["speed"],"nonnull_rows":1}]}}
        source='SensorWorkoutSessions(TargetUserId, StartLocal, EndLocal) '
        conflicts=_workout_shape_conflicts(source+'| project Value=Speed, Unit=SpeedUnit',observations)
        self.assertTrue(conflicts)
        self.assertIn('do not use', conflicts[0])
        self.assertFalse(_workout_shape_conflicts(source+'| project Value=Speed, Unit=""',observations))

    def test_query_generation_and_repair_preserve_message_roles(self):
        for resource in ("generate_sensor_query.txt", "repair_sensor_query.txt", "plan_sensor_query.txt"):
            system, user = load_role_prompt("d1_sensor_data/" + resource)
            self.assertRegex(system, r"\b(Reply|Return)\b")
            self.assertNotIn("{question}", system)
            self.assertIn("{question}", user)
            self.assertIn("{validation_feedback}" if resource == "plan_sensor_query.txt"
                          else "{schema_observations}", user)

    def test_prompts_carry_shared_conventions_without_benchmark_values(self):
        schema = load_prompt("d1_sensor_data/adx_schema_context.txt")
        query = load_prompt("d1_sensor_data/generate_sensor_query.txt")
        planner = load_prompt("d1_sensor_data/plan_sensor_query.txt")
        # Record identity, sleep night label and workout payload meaning.
        self.assertIn("arg_max(IngestionTime, *) by Type, Uuid, DateFrom.", schema)
        self.assertIn("IntervalDurationMatchRows", schema)
        self.assertIn("arg_max(IngestionTime, *) by WorkoutId", schema)
        self.assertIn("startofday(DateFrom - 12h)", schema)
        for field in ("workoutActivityType", "durationMinutes", "averageHeartRate",
                      "totalDistanceUnit", "totalEnergyBurnedUnit", "totalSteps",
                      "activeZoneMinutes", "parse_json(tostring(Value))"):
            self.assertIn(field, schema)
        # Declared producer units win over adapter labels.
        self.assertIn("producer_measurement_contracts", schema)
        # Process steps the examples rely on.
        self.assertIn("real(null)", query)
        self.assertIn("percentile(x, 50) is not an exact median", query)
        self.assertIn("rather than switching to another activity", query)
        self.assertIn("decide from the metadata", query)
        self.assertIn('"anchor":"rolling"', planner)
        self.assertIn("Sunday-Saturday", planner)
        for text in (schema, query, planner):
            for leak in ("PHIA", "phia-", "kist008", "431.63", "943.8", "162.57"):
                self.assertNotIn(leak, text)

    def test_invalid_json_is_repaired_once_without_changing_the_request(self):
        node = importlib.import_module("chatbot.nodes.d1_sensor_data.plan_sensor_query")
        valid = {"schema_version": 1, "metrics": ["distance"],
                 "time_range": {"mode": "relative", "anchor": "today", "unit": "day", "value": 1}}
        calls = []
        def respond(prompt):
            calls.append(prompt.to_string())
            return "not JSON" if len(calls) == 1 else json.dumps(valid)
        with patch.object(node, "llm", RunnableLambda(respond)):
            result = node.plan_sensor_query({"messages": [HumanMessage(content="Distance today?")]})
        self.assertEqual(len(calls), 2)
        self.assertIn("Distance today?", calls[1])
        self.assertEqual(result["plan"]["metrics"], ["distance"])
        self.assertTrue(result["sensor_plan_trace"]["succeeded"])

    def test_json_repair_does_not_open_a_second_schema_repair_budget(self):
        node = importlib.import_module("chatbot.nodes.d1_sensor_data.plan_sensor_query")
        calls = []
        def respond(_):
            calls.append(1)
            return "not JSON" if len(calls) == 1 else json.dumps({
                "schema_version": 2, "metrics": ["distance"],
                "computation": {"entity": "unknown", "outputs": []}})
        with patch.object(node, "llm", RunnableLambda(respond)):
            result = node.plan_sensor_query({"messages": [HumanMessage(content="Distance today?")]})
        self.assertEqual(len(calls), 2)
        self.assertFalse(result["plan"]["execution_ready"])
        self.assertFalse(result["sensor_plan_trace"]["succeeded"])

    def test_transport_error_is_not_retried_as_json_repair(self):
        node = importlib.import_module("chatbot.nodes.d1_sensor_data.plan_sensor_query")
        calls = []
        def respond(_):
            calls.append(1)
            raise RuntimeError("transport unavailable")
        with patch.object(node, "llm", RunnableLambda(respond)):
            result = node.plan_sensor_query({"messages": [HumanMessage(content="Distance today?")]})
        self.assertEqual(len(calls), 1)
        self.assertFalse(result["sensor_plan_trace"]["attempted"])
        self.assertFalse(result["plan"]["execution_ready"])

    def test_repeated_clock_window_keeps_last_day_not_only_first_day(self):
        plan = {"time_range": {"mode": "absolute", "anchor": "absolute", "unit": "day", "value": 4,
                "start_date": "2025-04-01", "end_date": "2025-04-04"},
                "reference_clock_local": "2025-04-09 09:15:00",
                "time_of_day": {"start_hour": 6, "end_hour": 9, "end_minute": 15}}
        self.assertEqual(reactive_query_bounds(plan),
                         ("datetime(2025-04-01 06:00:00)", "datetime(2025-04-04 09:15:00)"))

    def test_comparison_periods_survive_single_phrase_parser(self):
        candidate = {"schema_version": 2, "metrics": ["distance"],
            "time_range": {"mode": "relative", "unit": "day", "value": 1, "anchor": "today"},
            "comparison_time_range": {"mode": "relative", "unit": "day", "value": 4, "anchor": "rolling"},
            "time_of_day": {"start_hour": 0, "end_hour": 9, "end_minute": 15},
            "computation": {"entity": "day", "outputs": [{"metric": "distance", "aggregation": "sum"}]}}
        result = sanitize_plan(candidate, "Compare distance so far today against the past 4 days.", {})
        self.assertTrue(result["execution_ready"], result)
        self.assertEqual(result["time_range"]["anchor"], "today")
        self.assertEqual(result["comparison_time_range"]["value"], 4)
        result["reference_clock_local"] = "2025-04-09 09:15:00"
        self.assertEqual(reactive_query_bounds(result),
                         ("datetime(2025-04-05 00:00:00)", "datetime(2025-04-09 09:15:00)"))

    def test_invalid_comparison_is_not_silently_dropped(self):
        for reference in (None, "yesterday", {"mode": "relative", "unit": "day", "value": 999999, "anchor": "rolling"}):
            result = sanitize_plan({"schema_version": 1, "metrics": ["steps"],
                "time_range": {"mode": "relative", "unit": "day", "value": 1, "anchor": "today"},
                "comparison_time_range": reference}, "Compare my activity.", {})
            self.assertFalse(result["execution_ready"])
            self.assertTrue(result["validation_errors"])

    def test_eval_clock_stays_date_only_and_missing_hour_is_not_invented(self):
        clock = EvaluationTimeContext(reference_date=date(2025, 4, 9), summary_min_date=date(2025, 4, 1))
        with use_evaluation_time(clock):
            self.assertEqual(_query_reference_clock({}), "2025-04-09")
            with self.assertRaises(ValueError):
                reactive_query_bounds({"time_range": {"mode": "relative", "unit": "hour", "value": 2, "anchor": "rolling"}})

    def test_frozen_timezone_clock_allows_live_metadata_discovery(self):
        from chatbot.services.sensor_inventory import observe_sensor_schema
        plan = {"reference_clock_local": "2025-04-09 09:15:00", "timezone_iana": "Asia/Seoul",
                "time_range": {"mode": "relative", "unit": "day", "value": 1, "anchor": "yesterday"}}
        start, end = reactive_query_bounds(plan)
        calls = []
        result = observe_sensor_schema("synthetic-user", start, end, lambda q: calls.append(q) or [])
        self.assertEqual(len(calls), 1)
        self.assertNotEqual(result.get("reason"), "invalid_scope")
        self.assertIn("2025-04-08", calls[0])

    def test_clarification_plan_does_not_run_sensor_query(self):
        from chatbot.nodes.d1_sensor_data.plan_sensor_query import _plan_node_result
        from chatbot.nodes.d1_sensor_data.workflow import plan_decision
        result = _plan_node_result({"needs_followup": True, "followup_question": "Which cutoff time?"}, {}, {})
        self.assertEqual(plan_decision(result), "produce_sensor_result")

    def test_llm_is_the_default_kql_producer(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(_kql_generate_method(), "llm")

    def test_answer_clock_stays_frozen_across_midnight(self):
        self.assertEqual(_answer_today({"reference_clock_local": "2025-04-09 23:59:59"}, {}),
                         "2025-04-09")

    def test_insufficient_time_resolution_is_not_a_successful_exact_value(self):
        from chatbot.nodes.d1_sensor_data.produce_sensor_result import produce_sensor_result
        row = {"Metric": "distance", "Value": None, "ResultStatus": "insufficient_time_resolution"}
        result = produce_sensor_result({"query_result": [row], "execution_status": "success"})["d1_result"]
        self.assertEqual(result["status"], "insufficient_information")
        self.assertEqual(result["structured_result"]["records"], [row])

    def test_domain_evaluation_clock_is_preserved_after_planning(self):
        from chatbot.nodes.d1_sensor_data.plan_sensor_query import _plan_node_result
        clock = EvaluationTimeContext(summary_reference_date=date(2025, 4, 9),
            summary_min_date=date(2025, 4, 1), workout_reference_date=date(2025, 4, 7),
            workout_min_date=date(2025, 4, 1))
        plan = {"time_range": {"mode": "relative", "unit": "day", "value": 1, "anchor": "yesterday"},
                "computation": {"entity": "workout_session"}, "reference_clock_local": "2025-04-09"}
        with use_evaluation_time(clock):
            result = _plan_node_result(plan, {}, {})["plan"]
        self.assertEqual(result["reference_clock_local"], "2025-04-07")
        self.assertEqual(result["time_range"]["start_date"], "2025-04-06")

    def test_evaluation_response_uses_domain_reference_date(self) -> None:
        clock = EvaluationTimeContext(
            summary_reference_date=date(2023, 10, 31),
            summary_min_date=date(2023, 10, 1),
            workout_reference_date=date(2023, 10, 9),
            workout_min_date=date(2023, 10, 2),
        )
        with use_evaluation_time(clock):
            self.assertEqual(_answer_today({"metrics": ["steps"]}, {}), "2023-10-31")
            self.assertEqual(_answer_today({"computation": {"entity": "workout_session"}}, {}),
                             "2023-10-09")

    def test_scalar_result_does_not_claim_missing_days_without_dated_buckets(self) -> None:
        plan = {
            "metrics": ["steps"],
            "time_range": {"mode": "absolute", "anchor": "absolute",
                           "start_date": "2026-10-06", "end_date": "2026-10-06"},
        }
        coverage = _coverage_context(plan, [{"Metric": "steps", "Value": 13162.1,
                                             "EvidenceRows": 1, "ResultStatus": "ok"}])
        self.assertFalse(coverage["has_gap"])
        self.assertEqual(coverage["missing_dates"], [])

    def test_query_prompt_clock_is_request_local_and_restores_real_now(self) -> None:
        clock = EvaluationTimeContext(
            summary_reference_date=date(2024, 6, 20),
            summary_min_date=date(2024, 6, 1),
        )
        with patch("chatbot.policies.reactive_query_scope.datetime", wraps=datetime) as wall_clock:
            wall_clock.now.return_value = datetime(2026, 10, 8, 12, 34)
            self.assertEqual(_query_reference_clock({}), "2026-10-08 12:34:00")
            with use_evaluation_time(clock):
                self.assertEqual(_query_reference_clock({}), "2024-06-20")
            self.assertEqual(_query_reference_clock({}), "2026-10-08 12:34:00")

    def test_independent_scope_prompt_renders_without_candidate_or_answers(self) -> None:
        rendered = load_prompt("d1_sensor_data/review_measurement_scope.txt").format(
            question="Compare my selected session measurements.",
            history="None", last_plan="{}", current_local_time="2024-06-20",
            available_metrics="workout_duration, workout_steps",
            validation_feedback="Observed measurement schema only.",
        )
        self.assertIn("Compare my selected session measurements.", rendered)
        self.assertIn("Observed measurement schema only.", rendered)
        self.assertNotIn("previous_plan", rendered)
        self.assertNotIn("PHIA", rendered)
        self.assertNotIn("kist008", rendered)
        system, user = load_role_prompt("d1_sensor_data/review_measurement_scope.txt")
        self.assertNotIn("{question}", system)
        self.assertIn("{question}", user)

    def test_json_null_followup_uses_a_real_clarification_question(self) -> None:
        plan = sanitize_plan({"schema_version": 1, "metrics": ["distance"],
            "time_range": {"mode": "relative", "anchor": "today", "unit": "day", "value": 1},
            "needs_followup": True, "followup_question": None}, "Distance?", {})
        self.assertTrue(plan["needs_followup"])
        self.assertTrue(plan["followup_question"])
        self.assertNotEqual(plan["followup_question"], "None")

    def test_answer_prompt_contains_no_worked_reference_answers(self) -> None:
        text = load_prompt("common/generate_final_response/sensor_data.txt")
        self.assertNotIn("Representative examples", text)
        self.assertNotIn("8,679", text)
        self.assertNotIn("Question: What", text)
        self.assertLess(len(text.split()), 400)

    def test_absolute_plan_end_date_becomes_exclusive_bound(self) -> None:
        start, end = _kql_phone_window_for_plan(
            {
                "timezone_iana": "Asia/Seoul",
                "time_range": {
                    "mode": "absolute",
                    "anchor": "absolute",
                    "start_date": "2025-04-01",
                    "end_date": "2025-04-08",
                },
            }
        )
        self.assertEqual(start, "datetime(2025-04-01)")
        self.assertEqual(end, "datetime(2025-04-09)")

    def test_daily_flat_probe_uses_only_shared_guarded_functions(self) -> None:
        plan = {
            "metrics": ["steps"], "granularity": "daily",
            "time_range": {
                "mode": "absolute", "anchor": "absolute",
                "start_date": "2025-04-01", "end_date": "2025-04-08",
            },
        }
        probe = daily_flat_recheck.build_daily_flat_probe(
            plan, "example-user", "SensorActivityDaily(TargetUserId, StartLocal, EndLocal)"
        )
        self.assertIsNotNone(probe)
        self.assertTrue(inspect_query_safety(probe, "example-user")["is_safe"])
        self.assertIn("SensorHealthSamples(TargetUserId, StartLocal, EndLocal)", probe)
        self.assertIn("SensorActivityDaily(TargetUserId, StartLocal, EndLocal)", probe)
        self.assertNotIn("PHIA", probe)
        self.assertIsNone(daily_flat_recheck.build_daily_flat_probe(
            plan, "example-user", "SensorSleepNights(TargetUserId, StartLocal, EndLocal)"
        ))

    def test_comment_before_let_is_not_a_second_query(self) -> None:
        query = (
            'let TargetUserId="example-user";\n'
            'let StartLocal=datetime(2025-04-01);\n'
            'let EndLocal=datetime(2025-04-09);\n'
            '// daily evidence\n'
            'let Daily=SensorActivityDaily(TargetUserId, StartLocal, EndLocal) '
            '| where UserId == TargetUserId and LocalDate >= StartLocal and LocalDate < EndLocal '
            '| summarize Value=sum(ValueSumExact);\n'
            'Daily | take 1'
        )
        self.assertTrue(inspect_query_safety(query, "example-user")["is_safe"])

    def test_literal_datatable_guard_rejection_can_be_repaired_once(self) -> None:
        from chatbot.nodes.d1_sensor_data.workflow import _is_synthetic_source_rejection
        self.assertTrue(_is_synthetic_source_rejection({
            "kql_query": "Rows | union (datatable(x:long)[1])",
            "kql_guard": {"issues": [
                "KQL uses an unsupported or dynamically resolved tabular source.",
                "KQL uses a direct tabular source outside the health-chatbot allowlist: datatable.",
            ]},
        }))
        self.assertFalse(_is_synthetic_source_rejection({
            "kql_query": "Rows | union (datatable(x:long)[1])",
            "kql_guard": {"issues": [
                "KQL uses an unsupported or dynamically resolved tabular source.",
                "KQL is missing the required user filter.",
            ]},
        }))
        self.assertEqual(guard_decision({
            "guard_status": "error", "kql_query": "let DefaultRow=datatable(x:long)[1];",
            "kql_guard": {"issues": ["KQL uses an unsupported or dynamically resolved tabular source."]},
            "kql_generation_trace": {"method": "llm"}, "repair_attempts": 0,
        }), "repair_sensor_query")
        self.assertEqual(guard_decision({
            "guard_status": "error", "kql_query": ".drop table HealthDataFlat",
            "kql_guard": {"issues": ["KQL contains a management or write command."]},
            "kql_generation_trace": {"method": "llm"}, "repair_attempts": 0,
        }), "produce_sensor_result")

    def test_flat_recheck_runs_only_on_an_observed_daily_gap(self) -> None:
        plan = {
            "metrics": ["steps"], "granularity": "daily",
            "time_range": {
                "mode": "absolute", "anchor": "absolute",
                "start_date": "2025-04-01", "end_date": "2025-04-08",
            },
        }
        primary = "SensorActivityDaily(TargetUserId, StartLocal, EndLocal)"
        calls = []
        def no_gap(query: str):
            calls.append(query)
            return []
        self.assertIsNone(daily_flat_recheck.recheck_daily_with_flat(
            plan=plan, user_id="example-user", question="How many steps?",
            primary_kql=primary, primary_rows=[], execute_query=no_gap,
        ))
        self.assertEqual(len(calls), 1)

        candidate = (
            'let TargetUserId="example-user";\n'
            'let StartLocal=datetime(2025-04-01);\n'
            'let EndLocal=datetime(2025-04-09);\n'
            'let D=SensorActivityDaily(TargetUserId, StartLocal, EndLocal) '
            '| where UserId == TargetUserId and LocalDate >= StartLocal and LocalDate < EndLocal '
            '| project Period=LocalDate, Value=todouble(ValueSumExact), Type;\n'
            'let F=SensorHealthSamples(TargetUserId, StartLocal, EndLocal) '
            '| where UserId == TargetUserId and DateFrom >= StartLocal and DateFrom < EndLocal '
            '| project Period=startofday(DateFrom), Value=todouble(ValueNumber), Type;\n'
            'D | union F | where Type == "STEPS" | summarize Value=max(Value) by Period '
            '| sort by Period asc | take 10'
        )
        self.assertTrue(inspect_query_safety(candidate, "example-user")["is_safe"])
        calls.clear()
        def observed_gap(query: str):
            calls.append(query)
            if len(calls) == 1:
                return [{"Day": "2025-04-02", "Type": "STEPS", "FlatRows": 1,
                         "DayTotalRows": 1, "IntervalRows": 0, "Sources": 1}]
            return [{"Period": "2025-04-02", "Value": 3100}]
        with patch.object(daily_flat_recheck, "llm", RunnableLambda(lambda _: candidate)):
            outcome = daily_flat_recheck.recheck_daily_with_flat(
                plan=plan, user_id="example-user", question="How many steps?",
                primary_kql=primary, primary_rows=[], execute_query=observed_gap,
            )
        self.assertEqual(len(calls), 2)
        self.assertEqual(outcome["coverage"]["recheck_status"], "completed")
        self.assertEqual(outcome["rows"][0]["Value"], 3100)

    def test_flat_recheck_does_not_claim_success_for_empty_answer(self) -> None:
        plan = {
            "metrics": ["steps"], "granularity": "daily",
            "time_range": {
                "mode": "absolute", "anchor": "absolute",
                "start_date": "2025-04-01", "end_date": "2025-04-01",
            },
        }
        candidate = (
            'let TargetUserId="example-user";\n'
            'let StartLocal=datetime(2025-04-01);\n'
            'let EndLocal=datetime(2025-04-02);\n'
            'let D=SensorActivityDaily(TargetUserId, StartLocal, EndLocal) '
            '| where UserId == TargetUserId and LocalDate >= StartLocal and LocalDate < EndLocal '
            '| project Period=LocalDate, Value=todouble(ValueSumExact), Type;\n'
            'let F=SensorHealthSamples(TargetUserId, StartLocal, EndLocal) '
            '| where UserId == TargetUserId and DateFrom >= StartLocal and DateFrom < EndLocal '
            '| project Period=startofday(DateFrom), Value=todouble(ValueNumber), Type;\n'
            'D | union F | where Type == "STEPS" | project Period, Value | take 10'
        )
        calls = []
        def executor(query: str):
            calls.append(query)
            if len(calls) == 1:
                return [{"Day": "2025-04-01", "Type": "STEPS", "FlatRows": 1,
                         "DayTotalRows": 1, "IntervalRows": 0, "Sources": 1}]
            return []
        with patch.object(daily_flat_recheck, "llm", RunnableLambda(lambda _: candidate)):
            outcome = daily_flat_recheck.recheck_daily_with_flat(
                plan=plan, user_id="example-user", question="How many steps?",
                primary_kql="SensorActivityDaily(TargetUserId, StartLocal, EndLocal)",
                primary_rows=[], execute_query=executor,
            )
        self.assertEqual(outcome["coverage"]["recheck_status"], "no_evidence_after_recheck")
        self.assertEqual(outcome["rows"], [])
