"""Ensure recovery heuristics do not overwrite valid planner decisions."""

import importlib
import unittest
from unittest.mock import patch

from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableLambda

from chatbot.policies.plan_sanitization import sanitize_plan
from chatbot.policies.response_format import _build_analysis_footer


class LLMPlanPreservationTests(unittest.TestCase):
    def test_typed_temporal_meaning_survives_keyword_recovery(self):
        from datetime import date
        from chatbot.policies.time_ranges import compute_window_bounds
        for include_today, expected in ((True, (date(2022,8,24),date(2022,8,30))),
                                         (False,(date(2022,8,23),date(2022,8,29)))):
            raw={"mode":"relative","value":7,"unit":"day","anchor":"rolling",
                 "include_today":include_today}
            plan=sanitize_plan({"schema_version":2,"time_range":raw,
                "computation":{"entity":"workout_session","outputs":[{"metric":"workout_duration","aggregation":"none"}],
                               "selector":{"mode":"last","n":1}}},
                "Find the most recent session within the last 7 days",{})
            self.assertTrue(plan["execution_ready"])
            self.assertEqual(plan["time_range"],raw)
            self.assertEqual(compute_window_bounds(plan["time_range"],reference_date=date(2022,8,30)),expected)

    def test_sleep_dates_and_scan_padding_do_not_change_logical_period(self):
        from chatbot.policies.reactive_query_scope import query_time_scope
        for basis in ("night_day","wake_day","start_day"):
            plan=sanitize_plan({"schema_version":2,"sleep_date_basis":basis,
                "time_range":{"mode":"absolute","anchor":"absolute","unit":"day","value":1,
                              "start_date":"2022-08-23","end_date":"2022-08-23"},
                "computation":{"entity":"sleep_session","outputs":[{"metric":"deep_sleep","aggregation":"mean"}]}},
                "A sleep question with an explicitly declared date meaning",{})
            self.assertEqual(plan["sleep_date_basis"],basis)
            scope=query_time_scope(plan)
            self.assertEqual(scope["logical_start"],"datetime(2022-08-23 00:00:00)")
            self.assertEqual(scope["logical_end_exclusive"],"datetime(2022-08-24 00:00:00)")
            if basis=="night_day":
                self.assertEqual(scope["scan_end_exclusive"],"datetime(2022-08-25 00:00:00)")
            elif basis=="wake_day":
                self.assertEqual(scope["scan_start"],"datetime(2022-08-22 00:00:00)")
            else:
                self.assertEqual(scope["scan_end_exclusive"],scope["logical_end_exclusive"])

    def test_invalid_include_today_is_not_coerced_to_true(self):
        from chatbot.policies.time_ranges import validate_v2_time_range
        errors=validate_v2_time_range({"mode":"relative","value":7,"unit":"day","anchor":"rolling",
                                       "include_today":"yes"})
        self.assertEqual(errors[0]["path"],"time_range.include_today")

    def test_explicit_metric_list_is_not_mistaken_for_a_default(self):
        metrics = ["total_sleep_time", "steps", "heart_rate"]
        plan = sanitize_plan(
            {"schema_version": 1, "metrics": metrics},
            "Show my latest heart rate in context.",
            {},
        )
        self.assertEqual(plan["metrics"], metrics)

    def test_explicit_followup_metric_is_not_replaced_by_previous_metric(self):
        plan = sanitize_plan(
            {"schema_version": 1, "metrics": ["phone_resting_heart_rate"],
             "is_followup": True, "latest_sample": True},
            "When was it recorded?",
            {"metrics": ["heart_rate"], "latest_sample": True},
        )
        self.assertEqual(plan["metrics"], ["phone_resting_heart_rate"])

    def test_missing_metrics_still_allow_narrow_recovery(self):
        plan = sanitize_plan({}, "What is my latest heart rate?", {})
        self.assertEqual(plan["metrics"], ["heart_rate"])
        self.assertTrue(plan["latest_sample"])

    def test_explicit_false_is_not_replaced_by_latest_keyword(self):
        plan = sanitize_plan(
            {"schema_version": 1, "metrics": ["heart_rate"],
             "latest_sample": False},
            "Show my latest heart rate history.",
            {},
        )
        self.assertFalse(plan["latest_sample"])

    def test_explicit_true_is_not_vetoed_by_aggregate_keyword(self):
        plan = sanitize_plan(
            {"schema_version": 1, "metrics": ["heart_rate"],
             "latest_sample": True},
            "Show the latest heart-rate observation rather than an average.",
            {},
        )
        self.assertTrue(plan["latest_sample"])

    def test_missing_or_invalid_latest_flag_can_be_recovered(self):
        for candidate in ({}, {"latest_sample": "unsure"}):
            with self.subTest(candidate=candidate):
                plan = sanitize_plan(
                    {"schema_version": 1, "metrics": ["heart_rate"], **candidate},
                    "What is my latest heart rate?",
                    {},
                )
                self.assertTrue(plan["latest_sample"])

    def test_typed_computation_keeps_its_own_selection_contract(self):
        plan = sanitize_plan(
            {"schema_version": 2, "metrics": ["steps"],
             "latest_sample": True,
             "time_range": {"mode": "relative", "value": 7,
                            "unit": "day", "anchor": "rolling"},
             "computation": {"entity": "day",
                             "outputs": [{"metric": "steps", "aggregation": "sum"}]}},
            "What is my total number of steps in the past seven days?",
            {},
        )
        self.assertFalse(plan["latest_sample"])
        self.assertTrue(plan["execution_ready"])
        self.assertEqual(plan["computation"]["outputs"],
                         [{"metric": "steps", "aggregation": "sum"}])

    def test_analysis_case_does_not_claim_an_unplanned_fixed_comparison(self):
        for comparison_requested in (False, True):
            with self.subTest(comparison_requested=comparison_requested):
                plan = sanitize_plan(
                    {"case_type": "factor_analysis", "metrics": ["steps"],
                     "comparison_requested": comparison_requested,
                     "time_range": {"mode": "absolute", "anchor": "absolute",
                                    "start_date": "2025-02-01", "end_date": "2025-02-28"},
                     "assumptions": ["Use the explicitly selected observation period."]},
                    "Explain my recorded activity.",
                    {},
                )
                self.assertNotIn(
                    "For interpretation, I compared the recent 7 days with the previous 7 days.",
                    plan["assumptions"],
                )
                self.assertIn("Use the explicitly selected observation period.", plan["assumptions"])
                self.assertEqual(plan["time_range"]["start_date"], "2025-02-01")
                self.assertEqual(plan["comparison_requested"], comparison_requested)

    def test_analysis_footer_does_not_invent_comparison_from_response_style(self):
        for case_type in ("factor_analysis", "prescription"):
            with self.subTest(case_type=case_type):
                footer = _build_analysis_footer(
                    {"case_type": case_type, "metrics": ["steps"],
                     "comparison_requested": False,
                     "time_range": {"mode": "absolute", "anchor": "absolute",
                                    "start_date": "2025-02-01", "end_date": "2025-02-28"}},
                    "Explain activity on days with less than 6000 steps.",
                )
                self.assertNotIn("baseline", footer)
                self.assertIn("2025-02-01 to 2025-02-28", footer)

    def test_analysis_footer_keeps_an_explicit_comparison(self):
        footer = _build_analysis_footer(
            {"case_type": "factor_analysis", "metrics": ["steps"],
             "comparison_requested": True,
             "time_range": {"mode": "relative", "value": 7,
                            "unit": "day", "anchor": "rolling"}},
            "Compare the two periods.",
        )
        self.assertIn("compared with baseline", footer)

    def test_empty_or_unparseable_llm_plan_never_selects_default_metrics(self):
        planner = importlib.import_module("chatbot.nodes.d1_sensor_data.plan_sensor_query")
        for output in ("not JSON", "[]", "{}"):
            with self.subTest(output=output), patch.object(
                planner, "llm", RunnableLambda(lambda _, text=output: text),
            ):
                result = planner.plan_sensor_query(
                    {"messages": [HumanMessage(content="Show my recorded activity minutes.")],
                     "timezone_iana": "Asia/Seoul", "locale": "en"},
                )
                plan = result["plan"]
                self.assertEqual(plan["metrics"], [])
                self.assertFalse(plan["execution_ready"])
                self.assertTrue(plan["needs_followup"])
                self.assertEqual(plan["validation_errors"][0]["code"], "planner_invalid_output")
                self.assertEqual(plan["timezone_iana"], "Asia/Seoul")

    def test_failed_llm_call_is_non_executable_without_invented_metrics(self):
        planner = importlib.import_module("chatbot.nodes.d1_sensor_data.plan_sensor_query")

        def fail(_):
            raise RuntimeError("simulated model transport failure")

        with patch.object(planner, "llm", RunnableLambda(fail)):
            plan = planner.plan_sensor_query(
                {"messages": [HumanMessage(content="Show my recorded activity minutes.")]},
            )["plan"]
        self.assertEqual(plan["metrics"], [])
        self.assertFalse(plan["execution_ready"])
        self.assertEqual(plan["validation_errors"][0]["code"], "planner_call_failed")


if __name__ == "__main__":
    unittest.main()
