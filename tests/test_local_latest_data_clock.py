"""Discover logical data dates without model calls or overnight endpoint shifts."""

from datetime import date
import unittest
from unittest.mock import Mock

from local_llm_test import resolve_local_clock
from chatbot.policies.kql_safety import inspect_query_safety
from chatbot.services.local_data_clock import (
    LocalClockDiscoveryError, build_clock_metadata_kql, discover_latest_data_clock,
)


class LatestDataClockTests(unittest.TestCase):
    def test_latest_day_is_shared_across_summary_and_workout_resources(self):
        port = Mock()
        port.execute_query.return_value = [{"DataStartLocal": "2023-10-01",
                                          "AsOfLocal": "2023-10-31", "ClockRecordCount": 20}]
        result = discover_latest_data_clock("synthetic-user", port=port, today=date(2026, 10, 8))
        self.assertEqual(result.context.bounds("summary"), (date(2023, 10, 1), date(2023, 10, 31)))
        self.assertEqual(result.context.bounds("workout"), result.context.bounds("summary"))
        port.execute_query.assert_called_once()

    def test_primary_lookup_excludes_overnight_sleep_dates(self):
        query = build_clock_metadata_kql("synthetic-user", date(2023, 10, 1), date(2023, 11, 2))
        self.assertIn('Type in (', query)
        self.assertIn('"STEPS"', query)
        self.assertIn('"RESTING_HEART_RATE"', query)
        self.assertNotIn('"SLEEP_SESSION"', query)
        self.assertNotIn('"SLEEP_DEEP"', query)
        self.assertNotIn('SensorSleepNights', query)
        self.assertNotIn('startofday(DateTo)', query)
        decision = inspect_query_safety(query, "synthetic-user")
        self.assertTrue(decision["is_safe"], decision["issues"])

    def test_sleep_only_fallback_has_an_explicit_limitation(self):
        port = Mock()
        port.execute_query.side_effect = [
            [{"DataStartLocal": None, "AsOfLocal": None, "ClockRecordCount": 0}],
            [{"DataStartLocal": "2023-10-01", "AsOfLocal": "2023-11-01", "ClockRecordCount": 3}],
        ]
        result = discover_latest_data_clock("synthetic-user", port=port, today=date(2026, 10, 8))
        self.assertEqual(result.basis, "normalized_sleep_day")
        self.assertIn("wake day", result.limitation)
        self.assertEqual(port.execute_query.call_count, 2)
        self.assertIn("SleepDay", result.queries[-1])
        self.assertTrue(inspect_query_safety(result.queries[-1], "synthetic-user")["is_safe"])

    def test_missing_or_failed_metadata_never_falls_back_to_now(self):
        for response in ([], "Error: authentication failed", [{"unexpected": "metadata"}]):
            with self.subTest(response=response):
                port = Mock()
                port.execute_query.return_value = response
                with self.assertRaises(LocalClockDiscoveryError):
                    discover_latest_data_clock("synthetic-user", port=port, today=date(2026, 10, 8))

    def test_current_clock_and_explicit_dates_do_not_query_adx(self):
        port = Mock()
        clock, details = resolve_local_clock("synthetic-user", "current", port=port)
        self.assertIsNone(clock)
        self.assertEqual(details, {"mode": "current"})
        clock, details = resolve_local_clock(
            "synthetic-user", "latest-data", as_of="2023-10-31", data_start="2023-10-01", port=port,
        )
        self.assertEqual(clock.summary_reference_date, date(2023, 10, 31))
        self.assertEqual(details["mode"], "explicit")
        port.execute_query.assert_not_called()

    def test_lookup_window_cannot_exceed_guarded_maximum(self):
        with self.assertRaises(ValueError):
            build_clock_metadata_kql("synthetic-user", date(2010, 1, 1), date(2026, 1, 1))


if __name__ == "__main__":
    unittest.main()
