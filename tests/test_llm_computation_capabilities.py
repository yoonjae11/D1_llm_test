"""Public semantic grains must not inherit a query compiler's limitations."""

import os
import unittest
from unittest.mock import patch

from chatbot.policies.plan_sanitization import sanitize_plan
from chatbot.policies.query_algebra import validate_llm_computation_support
from chatbot.policies.query_algebra_kql import validate_computation_support


def candidate(entity, metric, aggregation="max", **computation_fields):
    return {
        "schema_version": 2,
        "metrics": [metric],
        "time_range": {"mode": "absolute", "anchor": "absolute", "unit": "day",
                       "value": 4, "start_date": "2025-05-05", "end_date": "2025-05-08"},
        "computation": {"entity": entity,
                        "outputs": [{"metric": metric, "aggregation": aggregation}],
                        **computation_fields},
    }


class LLMComputationCapabilitiesTests(unittest.TestCase):
    def test_model_resolved_latest_entity_scope_is_not_a_vague_recent_period(self):
        request = candidate("day", "rmssd", "none",
            conditions=[{"metric": "workout_count", "operator": "gt", "value": 0, "unit": "native"}],
            filters=[{"dimension": "activity_name", "operator": "eq", "value": "Rowing", "applies_to": "workout_count"}],
            selector={"mode": "last", "n": 1})
        with patch.dict(os.environ, {"KQL_GENERATE_METHOD": "llm"}):
            plan = sanitize_plan(request, "On my most recent training date, show the measurement.", {})
        self.assertTrue(plan["execution_ready"], plan)
        self.assertFalse(plan["needs_followup"], plan)
        self.assertEqual(plan["time_range"], request["time_range"])
        self.assertFalse(any("14 days by default" in a for a in plan["assumptions"]))

    def test_model_requested_period_clarification_is_preserved(self):
        request = candidate("day", "rmssd", "mean")
        request.update(needs_followup=True, followup_question="Which period do you mean?")
        with patch.dict(os.environ, {"KQL_GENERATE_METHOD": "llm"}):
            plan = sanitize_plan(request, "How has the measurement been recently?", {})
        self.assertTrue(plan["needs_followup"])
        self.assertEqual(plan["followup_question"], "Which period do you mean?")

    def test_workout_date_count_is_not_limited_by_template_compiler(self):
        computation = {"entity": "workout_session", "outputs": [
            {"entity": "day", "aggregation": "count_distinct"}],
            "filters": [{"dimension": "activity_name", "operator": "eq", "value": "Rowing"}],
            "conditions": [{"metric": "workout_duration", "operator": "gt", "value": 19, "unit": "minute"}]}
        self.assertEqual(validate_llm_computation_support(computation), [])
        self.assertTrue(validate_computation_support(computation))
        with patch.dict(os.environ, {"KQL_GENERATE_METHOD": "llm"}):
            plan = sanitize_plan({**candidate("workout_session", "workout_duration"),
                                  "computation": computation}, "Count eligible training dates.", {})
        self.assertTrue(plan["execution_ready"], plan)
        self.assertEqual(plan["computation"]["outputs"], computation["outputs"])

    def test_cross_stream_workout_conditions_and_grouping_are_llm_capabilities(self):
        computation = candidate("workout_session", "workout_steps", "sum",
            conditions=[{"metric": "deep_sleep", "operator": "gte", "value": 30, "unit": "minute"}],
            group_by=["day"])["computation"]
        self.assertEqual(validate_llm_computation_support(computation), [])
        self.assertTrue(validate_computation_support(computation))
        incompatible = candidate("workout_session", "rmssd")["computation"]
        self.assertIn("incompatible_entity_metric", {e["code"] for e in validate_llm_computation_support(incompatible)})

    def test_llm_sample_extremum_keeps_observation_grain(self):
        with patch.dict(os.environ, {"KQL_GENERATE_METHOD": "llm"}):
            plan = sanitize_plan(candidate("sample", "rmssd"), "Find the peak recorded value.", {})
        self.assertTrue(plan["execution_ready"])
        self.assertEqual(plan["computation"]["entity"], "sample")
        self.assertEqual(plan["computation"]["outputs"], [{"metric": "rmssd", "aggregation": "max"}])
        self.assertEqual(plan["granularity"], "intraday")

    def test_day_grain_is_preserved_when_explicitly_selected(self):
        with patch.dict(os.environ, {"KQL_GENERATE_METHOD": "llm"}):
            plan = sanitize_plan(candidate("day", "rmssd"), "Find the peak daily value.", {})
        self.assertTrue(plan["execution_ready"])
        self.assertEqual(plan["computation"]["entity"], "day")

    def test_deterministic_sample_support_is_unchanged(self):
        with patch.dict(os.environ, {"KQL_GENERATE_METHOD": "deterministic"}):
            plan = sanitize_plan(candidate("sample", "rmssd"), "Find the peak recorded value.", {})
        self.assertFalse(plan["execution_ready"])
        self.assertIn("unsupported_entity", {error["code"] for error in plan["validation_errors"]})
        self.assertTrue(validate_computation_support(candidate("sample", "rmssd")["computation"]))

    def test_llm_hour_grain_accepts_cumulative_activity_only(self):
        with patch.dict(os.environ, {"KQL_GENERATE_METHOD": "llm"}):
            supported = sanitize_plan(candidate("hour", "steps", "sum"), "Sum the hourly observations.", {})
            incompatible = sanitize_plan(candidate("hour", "rem_sleep_percent"), "Find the peak recorded value.", {})
        self.assertTrue(supported["execution_ready"])
        self.assertFalse(incompatible["execution_ready"])
        self.assertIn("incompatible_entity_metric", {
            error["code"] for error in incompatible["validation_errors"]
        })

    def test_sample_condition_is_preserved_and_incompatible_streams_fail_closed(self):
        condition = {"metric": "rmssd", "operator": "gte", "value": 20, "unit": "millisecond"}
        computation = candidate("sample", "rmssd", conditions=[condition])["computation"]
        self.assertEqual(validate_llm_computation_support(computation), [])
        incompatible = candidate("sample", "rmssd", conditions=[
            {"metric": "total_sleep_time", "operator": "gte", "value": 2, "unit": "hour"},
        ])["computation"]
        self.assertIn("incompatible_entity_metric", {
            error["code"] for error in validate_llm_computation_support(incompatible)
        })

    def test_sample_profile_fields_are_not_numeric_measurements(self):
        errors = validate_llm_computation_support(candidate("sample", "phone_gender")["computation"])
        self.assertIn("incompatible_entity_metric", {error["code"] for error in errors})

    def test_entity_count_requires_an_evidence_stream(self):
        errors = validate_llm_computation_support({"entity": "sample", "outputs": [
            {"entity": "sample", "aggregation": "count"},
        ]})
        self.assertIn("missing_entity_source", {error["code"] for error in errors})


if __name__ == "__main__":
    unittest.main()
