"""Recover model representation errors without replacing question semantics."""
import importlib
import json
import unittest
from unittest.mock import patch

from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableLambda


class GeneratedPlanRecoveryTests(unittest.TestCase):
    def test_numeric_reduction_needs_evidence_count_but_entity_count_can_be_zero(self):
        from chatbot.nodes.d1_sensor_data.generate_sensor_query import _numeric_evidence_conflicts
        plan = {"computation": {"outputs": [{"metric": "workout_speed", "aggregation": "sum"}]}}
        self.assertTrue(_numeric_evidence_conflicts('summarize Value=sum(Speed)', plan))
        self.assertFalse(_numeric_evidence_conflicts(
            'summarize Value=sum(Speed), EvidenceRows=count() | extend ResultStatus="ok"', plan))
        plan["computation"]["outputs"] = [{"entity": "workout_session", "aggregation": "count"}]
        self.assertFalse(_numeric_evidence_conflicts('summarize Count=count()', plan))

    def test_evidence_conflicts_are_diagnostics_not_a_code_directed_rewrite(self):
        node = importlib.import_module("chatbot.nodes.d1_sensor_data.generate_sensor_query")
        candidate = "SensorSleepNights(U,S,E) | summarize max(DeepMin)"
        metadata = {"observations": [{"resource": "health.samples", "Type": "SLEEP_DEEP",
                                      "IntervalDurationMismatchRows": 2}]}
        plan = {"metrics": ["deep_sleep"], "time_range": {"mode": "absolute", "anchor": "absolute",
                "start_date": "2025-02-03", "end_date": "2025-02-04"}}
        calls = []
        def respond(prompt):
            calls.append(prompt.to_string())
            return candidate
        audit = {}
        with patch.object(node, "llm", RunnableLambda(respond)), patch(
                "chatbot.services.sensor_inventory.observe_sensor_schema", return_value=metadata):
            result = node._generate_kql_with_llm("example-user", "Maximum deep sleep?", plan, audit)
        self.assertEqual(result, candidate)
        self.assertEqual(len(calls), 1)
        self.assertFalse(audit["attempted"])
        self.assertTrue(audit["diagnostic_conflicts"])
        self.assertEqual(audit["schema_observations"], metadata)

    def test_selected_or_filtered_outputs_do_not_fabricate_missing_dates(self):
        from chatbot.nodes.d1_sensor_data.execute_sensor_query import _coverage_context
        plan = {"schema_version": 2, "timezone_iana": "UTC",
                "time_range": {"mode": "absolute", "anchor": "absolute",
                               "start_date": "2025-02-01", "end_date": "2025-02-07"},
                "computation": {"entity": "day", "outputs": [
                    {"metric": "distance", "aggregation": "none"}],
                    "selector": {"mode": "bottom", "n": 1}}}
        rows = [{"Metric": "distance", "Value": 7.0, "Period": "2025-02-04",
                 "EvidenceRows": 1, "Aggregation": "none"}]
        context = _coverage_context(plan, rows)
        self.assertFalse(context["date_coverage_known"])
        self.assertIsNone(context["base_coverage_rows"])
        self.assertIsNone(context["selected_rows"])
        self.assertNotIn("missing_dates", context)
        self.assertNotIn("has_gap", context)
        del plan["computation"]["selector"]
        context = _coverage_context(plan, rows)
        self.assertTrue(context["date_coverage_known"])
        self.assertEqual(context["observed_day_count"], 1)

    def test_selected_aggregate_reports_actual_not_requested_cardinality(self):
        from chatbot.nodes.d1_sensor_data.generate_sensor_query import _selection_evidence_conflicts
        plan = {"computation": {"selector": {"mode": "last", "n": 5}, "outputs": [
            {"metric": "workout_calories", "aggregation": "sum"}]}}
        self.assertTrue(_selection_evidence_conflicts('summarize Value=sum(CaloriesKcal)', plan))
        self.assertFalse(_selection_evidence_conflicts(
            'summarize Value=sum(CaloriesKcal), SelectedRows=count()', plan))
        self.assertFalse(_selection_evidence_conflicts('summarize Value=sum(CaloriesKcal)', {}))

    def test_dependent_day_lookup_retains_selected_date_when_target_is_missing(self):
        from chatbot.nodes.d1_sensor_data.generate_sensor_query import _selection_evidence_conflicts
        plan = {"computation": {"entity": "day", "selector": {"mode": "last", "n": 1},
            "outputs": [{"metric": "rmssd", "aggregation": "none"}],
            "conditions": [{"metric": "workout_count", "operator": "gt", "value": 0, "unit": "native"}]}}
        lost = 'Targets | join kind=inner SelectedDates on Day | project Value'
        self.assertTrue(_selection_evidence_conflicts(lost, plan))
        fields = ' | project Period=Day, Value, EvidenceRows, ResultStatus'
        self.assertTrue(_selection_evidence_conflicts('Targets | join SelectedDates on Day'+fields, plan))
        self.assertFalse(_selection_evidence_conflicts('SelectedDates | join kind=leftouter Targets on Day'+fields, plan))
        simple = {"computation": {"entity": "day", "selector": {"mode": "last", "n": 1},
            "outputs": [{"metric": "rmssd", "aggregation": "none"}]}}
        self.assertFalse(_selection_evidence_conflicts(lost, simple))

    def test_legacy_speed_unit_is_not_payload_unit_evidence(self):
        from chatbot.nodes.d1_sensor_data.generate_sensor_query import _workout_shape_conflicts
        metadata = {"workout_payload": {"status": "observed", "object_rows": 2,
                    "sample_limit_reached": False, "schema_limit_reached": False,
                    "fields": [{"path": ["speed"], "nonnull_rows": 2}]}}
        query = 'SensorWorkoutSessions(U,S,E) | project Value=Speed, Unit=SpeedUnit'
        self.assertTrue(_workout_shape_conflicts(query, metadata))
        self.assertFalse(_workout_shape_conflicts(
            'SensorHealthSamples(U,S,E) | project Value=Payload.speed, Unit="UNSPECIFIED_NATIVE"', metadata))
        self.assertFalse(_workout_shape_conflicts(
            'SensorWorkoutSessions(U,S,E) | project Value=Distance/DurationMin, Unit="METER_PER_MINUTE"', metadata))
        metadata["workout_payload"]["fields"].append({"path": ["speedUnit"], "nonnull_rows": 2})
        self.assertFalse(_workout_shape_conflicts(query, metadata))

    def test_unknown_sleep_flag_does_not_erase_existing_measurement_evidence(self):
        from chatbot.nodes.d1_sensor_data.generate_sensor_query import _sleep_shape_conflicts
        metadata = {"observations": [{"resource": "sleep.nightly", "RowCount": 4,
                    "AsleepMinRows": 4, "HasAsleepTrueRows": 0, "HasAsleepNullRows": 4}]}
        self.assertTrue(_sleep_shape_conflicts(
            "SensorSleepNights(U,S,E) | where HasAsleepDuration == true | summarize stdev(AsleepMin)", metadata))
        self.assertFalse(_sleep_shape_conflicts(
            "SensorHealthSamples(U,S,E) | summarize stdev(ValueNumber)", metadata))
        metadata["observations"][0]["HasAsleepNullRows"] = 0
        self.assertFalse(_sleep_shape_conflicts(
            "SensorSleepNights(U,S,E) | where HasAsleepDuration == true", metadata))

    def test_workout_compatibility_check_uses_metadata_not_question_categories(self):
        from chatbot.nodes.d1_sensor_data.generate_sensor_query import _workout_shape_conflicts
        metadata = {"observations": [{"resource": "workout.sessions", "RowCount": 2,
                    "ActivityNameRows": 0, "ActivityCodeRows": 0, "DistanceRows": 0}],
                    "workout_payload": {"status": "observed", "object_rows": 2}}
        bad = 'SensorWorkoutSessions(U,S,E) | where ActivityName =~ "RUN" | summarize sum(Distance)'
        self.assertTrue(_workout_shape_conflicts(bad, metadata))
        self.assertFalse(_workout_shape_conflicts(
            'SensorHealthSamples(U,S,E) | extend Payload=parse_json(tostring(Value)) | project Payload.sportLabel', metadata))
        self.assertFalse(_workout_shape_conflicts('SensorWorkoutSessions(U,S,E) | summarize count()', metadata))
        metadata["workout_payload"]["status"] = "unusable"
        self.assertTrue(_workout_shape_conflicts(
            'SensorHealthSamples(U,S,E) | where Type == "WORKOUT" | extend Payload=parse_json(tostring(Value))', metadata))
        self.assertFalse(_workout_shape_conflicts(bad + '\n| project Value=real(null), DiagnosticReason="unusable_workout_payload"', metadata))
        self.assertTrue(_workout_shape_conflicts(bad +
            '\n| extend Dummy=real(null), DiagnosticReason="unusable_workout_payload" | summarize Value=count()', metadata))

    def test_encoded_payload_slot_requires_json_decode_not_direct_child_access(self):
        from chatbot.nodes.d1_sensor_data.generate_sensor_query import _workout_shape_conflicts
        metadata = {"workout_payload": {"status": "observed", "fields": [
            {"path": ["metrics"], "types": ["string"], "decoded_json_types": ["dictionary"]}]}}
        self.assertTrue(_workout_shape_conflicts('extend X=todouble(Payload.metrics.speed.value)', metadata))
        self.assertFalse(_workout_shape_conflicts(
            'extend M=parse_json(tostring(Payload.metrics)) | extend X=todouble(M.speed.value)', metadata))

    def test_sleep_shape_check_keeps_interval_and_timing_queries_available(self):
        from chatbot.nodes.d1_sensor_data.generate_sensor_query import _sleep_shape_conflicts
        observation = {"observations": [{"resource": "health.samples", "Type": "SLEEP_DEEP",
                        "IntervalDurationMismatchRows": 3}]}
        duration = "SensorSleepNights(U,S,E) | summarize max(DeepMin)"
        self.assertTrue(_sleep_shape_conflicts(duration, observation))
        self.assertFalse(_sleep_shape_conflicts("SensorSleepNights(U,S,E) | project OnsetLocal, WakeLocal", observation))
        self.assertFalse(_sleep_shape_conflicts("SensorHealthSamples(U,S,E) | summarize max(ValueNumber)", observation))
        observation["observations"][0]["IntervalDurationMismatchRows"] = 0
        self.assertFalse(_sleep_shape_conflicts(duration, observation))

    def test_schema_repair_preserves_metric_operation_and_window(self):
        node = importlib.import_module("chatbot.nodes.d1_sensor_data.plan_sensor_query")
        valid = {"schema_version": 2, "metrics": ["distance"],
                 "time_range": {"mode": "absolute", "anchor": "absolute", "unit": "day",
                                "value": 2, "start_date": "2025-02-03", "end_date": "2025-02-04"},
                 "computation": {"entity": "day", "outputs": [
                     {"metric": "distance", "aggregation": "sum"}]}}
        invalid = json.loads(json.dumps(valid))
        invalid["computation"]["outputs"][0]["unit"] = "meter"
        inputs = []
        def respond(prompt):
            inputs.append(prompt.to_string())
            return json.dumps(invalid if len(inputs) == 1 else valid)
        with patch.object(node, "llm", RunnableLambda(respond)):
            result = node.plan_sensor_query({"messages": [HumanMessage(
                content="Total distance for February 3 through February 4, 2025?")]})
        self.assertEqual(len(inputs), 2)
        self.assertIn("unknown_field", inputs[1])
        self.assertTrue(result["plan"]["execution_ready"])
        self.assertEqual(result["plan"]["computation"]["outputs"], valid["computation"]["outputs"])
        self.assertEqual(result["plan"]["computation"]["entity"], "day")
        self.assertEqual(result["plan"]["time_range"]["start_date"], "2025-02-03")
        self.assertTrue(result["sensor_plan_trace"]["succeeded"])

    def test_failed_repair_retains_non_executable_plan(self):
        node = importlib.import_module("chatbot.nodes.d1_sensor_data.plan_sensor_query")
        invalid = {"schema_version": 2, "metrics": ["distance"],
                   "computation": {"entity": "day", "outputs": [
                       {"metric": "distance", "aggregation": "sum", "unknown": 1}]}}
        calls = []
        def respond(_):
            calls.append(1)
            return json.dumps(invalid)
        with patch.object(node, "llm", RunnableLambda(respond)):
            result = node.plan_sensor_query({"messages": [HumanMessage(content="Total distance yesterday?")]})
        self.assertEqual(len(calls), 2)
        self.assertFalse(result["plan"]["execution_ready"])
        self.assertFalse(result["sensor_plan_trace"]["succeeded"])

    def test_routing_review_is_model_decided_and_audited(self):
        node = importlib.import_module("chatbot.nodes.planning.plan_domain_execution")
        replies = [json.dumps({"intent": "clarification"}),
                   json.dumps({"intent": "data", "selected_domains": ["D1"],
                               "execution_stages": [["D1"]]})]
        with patch.object(node.support, "call_chat_completion", side_effect=replies) as call:
            result = node.plan_domain_execution({"messages": [HumanMessage(content="Recorded distance today?")]})
        self.assertEqual(call.call_count, 2)
        self.assertEqual(result["domain_execution_plan"]["selected_domains"], ["D1"])
        self.assertTrue(result["orchestration_trace"]["review_attempted"])

    def test_genuine_ambiguity_remains_clarification(self):
        node = importlib.import_module("chatbot.nodes.planning.plan_domain_execution")
        with patch.object(node.support, "call_chat_completion", return_value='{"intent":"clarification"}'):
            result = node.plan_domain_execution({"messages": [HumanMessage(content="How about that?")]})
        self.assertEqual(result["intent"], "clarification")
        self.assertEqual(result["domain_execution_plan"]["selected_domains"], [])


if __name__ == "__main__":
    unittest.main()
