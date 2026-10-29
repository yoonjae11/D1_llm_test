"""Clock overrides stay caller-scoped and are identical across resources."""
import unittest
from datetime import date
from types import SimpleNamespace

from local_llm_test import evaluation_clock, trace_record
from chatbot.policies.time_ranges import (
    active_evaluation_time, use_evaluation_time, _local_today,
)


class LocalEvaluationClockTests(unittest.TestCase):
    def test_current_mode_does_not_install_a_fixture_clock(self):
        self.assertIsNone(evaluation_clock(None, None))

    def test_latest_logical_day_is_shared_and_context_is_restored(self):
        clock = evaluation_clock("2023-10-31", "2023-10-01")
        for family in ("summary", "workout"):
            self.assertEqual(clock.bounds(family), (date(2023, 10, 1), date(2023, 10, 31)))
        with use_evaluation_time(clock):
            self.assertEqual(_local_today("Asia/Seoul"), date(2023, 10, 31))
        self.assertIsNone(active_evaluation_time())

    def test_invalid_clock_never_silently_falls_back_to_machine_time(self):
        for reference, minimum in (("2023-10-31", None), (None, "2023-10-01"),
                                   ("2023-10-31", "2023-11-01"), ("wrong", "2023-10-01")):
            with self.subTest(reference=reference, minimum=minimum), self.assertRaises(ValueError):
                evaluation_clock(reference, minimum)

    def test_full_graph_planner_resolves_v1_yesterday_before_kql_generation(self):
        from chatbot.nodes.d1_sensor_data.plan_sensor_query import _plan_node_result
        from chatbot.policies.kql_generation import _kql_phone_window_for_plan
        plan = {"schema_version": 1, "metrics": ["steps"], "timezone_iana": "Asia/Seoul",
                "time_range": {"mode": "relative", "value": 1, "unit": "day", "anchor": "yesterday"}}
        with use_evaluation_time(evaluation_clock("2023-10-31", "2023-10-01")):
            result = _plan_node_result(plan, {}, plan)
            self.assertEqual(_kql_phone_window_for_plan(result["plan"]),
                             ("datetime(2023-10-30)", "datetime(2023-10-31)"))
        self.assertEqual(plan["time_range"]["mode"], "relative")

    def test_completed_seven_days_share_fixed_clock_on_workout_plan(self):
        from chatbot.nodes.d1_sensor_data.plan_sensor_query import _plan_node_result
        from chatbot.policies.kql_generation import _kql_phone_window_for_plan
        plan = {"schema_version": 2, "metrics": ["workout_duration"],
                "computation": {"entity": "workout_session"}, "timezone_iana": "Asia/Seoul",
                "time_range": {"mode": "relative", "value": 7, "unit": "day", "anchor": "rolling"}}
        with use_evaluation_time(evaluation_clock("2023-10-31", "2023-10-01")):
            result = _plan_node_result(plan, {}, plan)
            self.assertEqual(_kql_phone_window_for_plan(result["plan"]),
                             ("datetime(2023-10-24)", "datetime(2023-10-31)"))

    def test_trace_contains_real_query_and_not_secret_payload(self):
        answer = SimpleNamespace(
            text="recorded result", execution_status="ok",
            payload={"api_key": "not-for-export",
                     "final_state": {"kql_query": "actual query", "query_result": [{"Value": 7}]}},
        )
        trace = trace_record(answer, "synthetic-user", "selected-model", "question", None)
        self.assertEqual(trace["kql"], "actual query")
        self.assertEqual(trace["query_result"], [{"Value": 7}])
        self.assertNotIn("api_key", trace)


if __name__ == "__main__":
    unittest.main()
