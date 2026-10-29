"""Sensor discovery tests: bounded metadata never carries answer values."""

from __future__ import annotations

import json
import unittest
from unittest.mock import Mock, patch

from chatbot.services.sensor_inventory import observe_sensor_schema, observe_workout_payload_schema


class SensorInventoryTests(unittest.TestCase):
    def test_optional_sleep_flag_counts_do_not_become_answer_values(self):
        execute = Mock(return_value=[{"Resource": "sleep.nightly", "RowCount": 4,
            "HasAsleepTrueRows": 0, "HasAsleepFalseRows": 1, "HasAsleepNullRows": 3,
            "AsleepMin": 987.65}])
        result = observe_sensor_schema("user-1", "2025-04-01", "2025-04-09", execute)
        self.assertEqual(result["observations"][0]["HasAsleepNullRows"], 3)
        self.assertNotIn("AsleepMin", result["observations"][0])
        self.assertIn("HasAsleepNullRows=countif(isnull(HasAsleepDuration))", execute.call_args.args[0])

    def test_shared_guard_accepts_bounded_metadata_query(self) -> None:
        execute = Mock(return_value=[
            {"Resource": "health.samples", "Type": "STEPS", "Unit": "COUNT",
             "RowCount": 3, "NumericRows": 3, "FirstEventLocal": "2023-10-24",
             "LastEventLocal": "2023-10-30"},
            {"Resource": "activity.daily", "Type": "STEPS", "Unit": "COUNT", "RowCount": 0},
            {"Resource": "sleep.nightly", "RowCount": 2, "AsleepMinRows": 2, "TimeInBedMinRows": 2},
            {"Resource": "workout.sessions", "RowCount": 1,
             "ActivityCodes": '["RUN"]', "ActiveZoneMinutesRows": 0},
        ])
        result = observe_sensor_schema(
            "user-1", "datetime(2023-10-24)", "2023-10-31", execute
        )
        self.assertEqual(result["status"], "observed")
        self.assertEqual(len(result["observations"]), 4)
        self.assertEqual(result["observations"][-1]["ActivityCodes"], ["RUN"])
        query = execute.call_args.args[0]
        for name in ("SensorHealthSamples", "SensorActivityDaily", "SensorSleepNights", "SensorWorkoutSessions"):
            self.assertIn(f"{name}(TargetUserId, StartLocal, EndLocal)", query)
        self.assertIn("make_set(ActivityCode, 50)", query)
        self.assertIn("DateFrom < EndLocal and DateTo > StartLocal", query)
        self.assertNotIn("avg(", query)
        # Only cardinality counters are summed, never private measurement values.
        self.assertNotIn("sum(ValueNumber)", query)
        self.assertNotIn("sum(todouble(ValueNumber))", query)
        self.assertNotIn("sum(ValueSumExact)", query)
        self.assertIn("RowCount=sum(Copies)", query)

    def test_same_window_cardinality_is_metadata_not_measurement_evidence(self):
        execute = Mock(return_value=[{"Resource": "health.samples", "RowCount": 9,
            "RepeatedWindowRows": 8, "DistinctRecordSameWindowRows": 6,
            "MultiSourceSameWindowRows": 4, "ValueNumber": 777.123}])
        result = observe_sensor_schema("user-1", "2025-04-01", "2025-04-09", execute)
        self.assertEqual(result["observations"][0]["MultiSourceSameWindowRows"], 4)
        self.assertNotIn("777.123", json.dumps(result))
        self.assertIn("by Type, Unit, DateFrom, DateTo", execute.call_args.args[0])

    def test_measurements_labels_and_backend_details_are_removed(self) -> None:
        execute = Mock(return_value=[
            {"Resource": "health.samples", "RowCount": 1, "Type": "STEPS",
             "ValueNumber": 12345, "label": 12345, "Database": "private",
             "SourceName": "some_dataset", "NumericRows": 1},
            {"Resource": "untrusted.backend", "RowCount": 2},
        ])
        result = observe_sensor_schema("user-1", "2023-10-24", "2023-10-31", execute)
        self.assertEqual(result["observations"], [
            {"resource": "health.samples", "Type": "STEPS", "RowCount": 1, "NumericRows": 1}
        ])
        self.assertNotIn("12345", json.dumps(result))
        self.assertNotIn("some_dataset", json.dumps(result))

    def test_optional_field_and_category_metadata_is_preserved_without_values(self) -> None:
        execute = Mock(return_value=[
            {"Resource": "sleep.nightly", "RowCount": 2, "RemPctRows": 0,
             "DeepPctRows": 1, "RemPct": 19.5},
            {"Resource": "workout.sessions", "RowCount": 1, "ActivityNameRows": 1,
             "ActivityNames": '["Trail run"]', "ActivityCodes": ["TRAIL_RUN"],
             "Steps": 9999},
        ])
        result = observe_sensor_schema("user-1", "2025-04-01", "2025-04-09", execute)
        self.assertEqual(result["observations"][0]["RemPctRows"], 0)
        self.assertEqual(result["observations"][1]["ActivityNames"], ["Trail run"])
        self.assertNotIn("RemPct", result["observations"][0])
        self.assertNotIn("Steps", result["observations"][1])
        query = execute.call_args.args[0]
        self.assertIn("RemPctRows=countif(isnotnull(RemPct))", query)
        self.assertIn("ActivityNames=make_set(ActivityName, 50)", query)

    def test_sleep_record_shape_is_metadata_not_an_answer(self) -> None:
        execute = Mock(return_value=[{"Resource": "health.samples", "Type": "SLEEP_LIGHT",
            "RowCount": 4, "NumericRows": 4, "IntervalDurationMatchRows": 1,
            "IntervalDurationMismatchRows": 3, "ValueNumber": 91.2}])
        result = observe_sensor_schema("user-1", "2025-04-01", "2025-04-09", execute)
        self.assertEqual(result["observations"][0]["IntervalDurationMismatchRows"], 3)
        self.assertNotIn("ValueNumber", result["observations"][0])
        self.assertIn("datetime_diff('second', DateTo, DateFrom)", execute.call_args.args[0])

    def test_backend_failure_does_not_claim_zero_records(self) -> None:
        for execute in (Mock(return_value="Error: authentication failed"),
                        Mock(side_effect=RuntimeError("private endpoint key"))):
            with self.subTest(execute=execute):
                result = observe_sensor_schema("user-1", "2023-10-24", "2023-10-31", execute)
                self.assertEqual(result["status"], "unavailable")
                self.assertEqual(result["observations"], [])
                self.assertNotIn("private", json.dumps(result))
                self.assertNotIn("RowCount", result)

    def test_successful_empty_discovery_is_not_an_execution_failure(self) -> None:
        result = observe_sensor_schema("user-1", "2025-04-01", "2025-04-09", Mock(return_value=[]))
        self.assertEqual(result["status"], "observed")
        self.assertEqual(set(result["resource_status"].values()), {"empty"})

    def test_failed_daily_resource_does_not_hide_flat_inventory(self) -> None:
        calls = []
        def execute(query):
            calls.append(query)
            if "SensorActivityDaily(" in query:
                return "Error: backend view unavailable"
            if "SensorHealthSamples(" in query:
                return [{"Resource": "health.samples", "Type": "STEPS", "Unit": "COUNT",
                         "RowCount": 5, "NumericRows": 5}]
            return []
        result = observe_sensor_schema("user-1", "2025-04-01", "2025-04-09", execute)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["resource_status"]["activity.daily"], "unavailable")
        self.assertEqual(result["resource_status"]["health.samples"], "observed")
        self.assertEqual(result["observations"][0]["NumericRows"], 5)
        self.assertEqual(len(calls), 5)
        for query in calls:
            self.assertIn('let TargetUserId = "user-1";', query)
            self.assertIn('let StartLocal = datetime(2025-04-01 00:00:00);', query)

    def test_truncated_discovery_cannot_establish_missing_type(self) -> None:
        result = observe_sensor_schema("user-1", "2025-04-01", "2025-04-09", Mock(return_value=[
            {"Resource": "health.samples", "Type": "STEPS", "RowCount": 1}
        ] * 256))
        self.assertTrue(result["metadata_limit_reached"])
        self.assertEqual(set(result["resource_status"].values()), {"unknown"})

    def test_guard_rejection_prevents_backend_execution(self) -> None:
        execute = Mock()
        with patch("chatbot.services.sensor_inventory.inspect_query_safety",
                   return_value={"is_safe": False, "issues": ["rejected"]}):
            result = observe_sensor_schema("user-1", "2023-10-24", "2023-10-31", execute)
        self.assertEqual(result["reason"], "guard_rejected")
        execute.assert_not_called()

    def test_scope_cannot_contain_arbitrary_kql_or_invalid_date_order(self) -> None:
        for start, end in (("now()-100d", "now()"), ("2023-10-31", "2023-10-24"),
                           ("2023-10-24); .drop table T", "2023-10-31")):
            execute = Mock()
            result = observe_sensor_schema("user-1", start, end, execute)
            self.assertEqual(result["status"], "unavailable")
            execute.assert_not_called()

    def test_long_date_range_is_not_an_authorization_failure(self) -> None:
        execute = Mock(return_value=[])
        observe_sensor_schema("user-1", "1900-01-01", "2100-01-01", execute)
        execute.assert_called()

    def test_malformed_counts_are_not_converted_into_absence(self) -> None:
        execute = Mock(return_value=[{"Resource": "sleep.nightly", "RowCount": None}])
        result = observe_sensor_schema("user-1", "2023-10-24", "2023-10-31", execute)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["observations"], [])

    def test_payload_probe_discovers_keys_not_measurement_answers(self) -> None:
        execute = Mock(return_value=[{"Payload": {
            "__type": "WorkoutHealthValue", "workoutActivityType": "Indoor rowing",
            "durationMinutes": 17.25, "averageHeartRate": 137,
            "totalDistance": 876.54, "totalDistanceUnit": "METER",
            "speed": 0.0, "notes": "private note", "accountId": "private id",
        }}])
        result = observe_workout_payload_schema("user-1", "2025-04-01", "2025-04-09", execute)
        self.assertEqual(result["status"], "observed")
        fields = {tuple(f["path"]): f for f in result["fields"]}
        self.assertEqual(fields[("workoutActivityType",)]["observed_labels"], ["Indoor rowing"])
        self.assertEqual(fields[("totalDistanceUnit",)]["observed_labels"], ["METER"])
        self.assertEqual(fields[("speed",)]["nonnull_rows"], 1)
        self.assertEqual(fields[("speed",)]["types"], ["number"])
        for private in ("876.54", "17.25", "private note", "private id"):
            self.assertNotIn(private, json.dumps(result))
        query = execute.call_args.args[0]
        self.assertIn("arg_max(IngestionTime, *)", query)
        self.assertIn("parse_json(tostring(Value))", query)
        self.assertIn("take 65", query)
        self.assertNotIn("Value_1", query)

    def test_payload_discovery_handles_alternate_shapes_arrays_and_encoded_objects(self) -> None:
        execute = Mock(return_value=[{"Payload": {
            "sport": {"name": "Indoor rowing"},
            "metrics": [{"distance-km": "0.0", "unit": "KILOMETER"},
                        {"distance-km": "2.5", "unit": "KILOMETER"}],
            "extra": '{"durationSeconds":987.0,"unit":"SECOND"}',
        }}])
        result = observe_workout_payload_schema("user-1", "2025-04-01", "2025-04-09", execute)
        fields = {tuple(f["path"]): f for f in result["fields"]}
        self.assertEqual(fields[("sport", "name")]["observed_labels"], ["Indoor rowing"])
        self.assertEqual(fields[("metrics", "[]", "distance-km")]["types"], ["numeric_string"])
        self.assertEqual(fields[("metrics", "[]", "distance-km")]["present_rows"], 1)
        self.assertEqual(fields[("extra",)]["decoded_json_types"], ["dictionary"])
        self.assertIn(("extra", "durationSeconds"), fields)
        self.assertNotIn("987", json.dumps(result))
        self.assertNotIn('"2.5"', json.dumps(result))

    def test_unparsed_or_hidden_payload_is_not_empty_history(self) -> None:
        for payload in (None, "bad JSON", [], 19):
            result = observe_workout_payload_schema("user-1", "2025-04-01", "2025-04-09",
                Mock(return_value=[{"Payload": payload, "Value_1": {"workoutActivityType": "hidden"}}]))
            self.assertEqual(result["status"], "unusable")
            self.assertEqual(result["sampled_rows"], 1)
            self.assertNotIn("hidden", json.dumps(result))
        result = observe_workout_payload_schema("user-1", "2025-04-01", "2025-04-09", Mock(return_value=[]))
        self.assertEqual(result["status"], "empty")

    def test_payload_limits_are_reported_and_absent_fields_not_claimed_complete(self) -> None:
        deep = {"a": {"b": {"c": {"d": {"e": {"hidden": 22}}}}}}
        rows = [{"Payload": deep}] * 65
        result = observe_workout_payload_schema("user-1", "2025-04-01", "2025-04-09", Mock(return_value=rows))
        self.assertTrue(result["sample_limit_reached"])
        self.assertTrue(result["schema_limit_reached"])
        self.assertEqual(result["sampled_rows"], 64)
        self.assertNotIn("hidden", json.dumps(result))

    def test_payload_probe_uses_the_same_safety_scope_and_handles_errors(self) -> None:
        execute = Mock(return_value=[])
        result = observe_workout_payload_schema("user-1", "now()-3d", "now()", execute)
        self.assertEqual(result["reason"], "invalid_scope")
        execute.assert_not_called()
        result = observe_workout_payload_schema("user-1", "2025-04-01", "2025-04-09",
            Mock(side_effect=RuntimeError("private details")))
        self.assertEqual(result["status"], "unavailable")
        self.assertNotIn("private", json.dumps(result))

    def test_optional_payload_discovery_is_attached_without_changing_resource_inventory(self) -> None:
        def execute(query):
            if "project Payload=" in query:
                return [{"Payload": {"sportLabel": "Indoor rowing", "metres": 812.0}}]
            return [{"Resource": "workout.sessions", "RowCount": 1, "ActivityNameRows": 0}]
        result = observe_sensor_schema("user-1", "2025-04-01", "2025-04-09", execute,
                                      include_workout_payload=True)
        self.assertEqual(result["resource_status"]["workout.sessions"], "observed")
        self.assertEqual(result["workout_payload"]["status"], "observed")
        self.assertNotIn("812", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
