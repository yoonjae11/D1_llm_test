"""Synthetic unit tests, deliberately without benchmark questions/answers."""
import unittest

from local_evaluation_units import LocalEvaluationUnits, convert_speed
from chatbot.services.measurement_contract import (
    producer_measurement_contracts, use_workout_speed_unit,
)


class LocalEvaluationUnitsTests(unittest.TestCase):
    def test_opt_in_only(self):
        question = "What is the latest cycling speed?"
        self.assertEqual(LocalEvaluationUnits().prepare_question(question), question)
        self.assertEqual(producer_measurement_contracts(), [])

    def test_unrelated_measurements_unchanged(self):
        question = "What was my average resting pulse?"
        self.assertEqual(LocalEvaluationUnits("m/s", "m/min").prepare_question(question), question)

    def test_metadata_preserves_original_and_explicit_units(self):
        question = "How many sessions had speed over 12 km/h?"
        result = LocalEvaluationUnits("m/s", "m/min").prepare_question(question)
        self.assertTrue(result.startswith(question))
        self.assertIn("Explicit units in the original question take precedence", result)
        self.assertIn("Report recorded speed outputs in m/s", result)
        self.assertNotIn("PHIA", result)

    def test_context_restores_and_does_not_modify_values(self):
        with use_workout_speed_unit("METER_PER_SECOND"):
            self.assertEqual(producer_measurement_contracts()[0]["unit"], "METER_PER_SECOND")
            with use_workout_speed_unit("METER_PER_MINUTE"):
                self.assertEqual(producer_measurement_contracts()[0]["unit"], "METER_PER_MINUTE")
            self.assertFalse(producer_measurement_contracts()[0]["changes_values"])
        self.assertEqual(producer_measurement_contracts(), [])

    def test_dimensional_conversion_for_scorer_only(self):
        self.assertAlmostEqual(convert_speed(180, "m/min", "m/s"), 3)
        self.assertAlmostEqual(convert_speed(3, "m/s", "km/h"), 10.8)
        self.assertEqual(convert_speed(0, "m/min", "m/s"), 0)
        self.assertIsNone(convert_speed(None, "m/min", "m/s"))

    def test_invalid_contract_fails_before_requests(self):
        with self.assertRaises(ValueError):
            LocalEvaluationUnits(question_speed_unit="m/min")
        with self.assertRaises(ValueError):
            LocalEvaluationUnits("guessed")

    def test_stale_adapter_label_rejected_with_declared_contract(self):
        from chatbot.nodes.d1_sensor_data.generate_sensor_query import _workout_shape_conflicts
        observations = {"workout_payload": {"status": "observed", "object_rows": 1,
                        "fields": [{"path": ["speed"], "nonnull_rows": 1}]}}
        with use_workout_speed_unit("METER_PER_SECOND"):
            observations["producer_measurement_contracts"] = producer_measurement_contracts()
        query = "SensorWorkoutSessions(TargetUserId, StartLocal, EndLocal) | project Value=Speed, Unit=SpeedUnit"
        conflicts = _workout_shape_conflicts(query, observations)
        self.assertTrue(conflicts)
        self.assertIn("METER_PER_SECOND", conflicts[0])
        self.assertFalse(_workout_shape_conflicts(query.replace("Unit=SpeedUnit", 'Unit="METER_PER_SECOND"'), observations))

    def test_measurement_review_receives_declared_date_basis(self):
        import json
        from unittest.mock import Mock, patch
        from chatbot.nodes.d1_sensor_data.plan_sensor_query import _review_observed_measurement_scope
        computation = {"entity": "workout_session",
                       "outputs": [{"metric": "workout_calories", "aggregation": "sum"}],
                       "conditions": [{"metric": "total_sleep_time", "operator": "gt", "value": 6, "unit": "hour"}]}
        plan = {"schema_version": 2, "execution_ready": True, "computation": computation,
                "metrics": ["workout_calories", "total_sleep_time"], "sleep_date_basis": "night_day",
                "time_range": {"mode": "relative", "value": 4, "unit": "day", "anchor": "today"}}
        chain = Mock()
        chain.invoke.return_value = json.dumps({"computation": computation, "assumptions": [],
                                                "needs_followup": False, "followup_question": ""})
        with patch('chatbot.services.sensor_inventory.observe_sensor_schema', return_value={"status": "observed", "observations": []}):
            _review_observed_measurement_scope(plan, plan, {"user_id": "synthetic", "availability_context": {"WORKOUT": {}}},
                                              chain, {}, "Total cycling energy on eligible sleep nights", {}, None)
        feedback = json.loads(chain.invoke.call_args.args[0]["validation_feedback"])
        self.assertEqual(feedback["semantic_contract"]["sleep_date_basis"], "night_day")


if __name__ == "__main__":
    unittest.main()
