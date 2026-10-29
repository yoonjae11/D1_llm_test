"""General KQL time-bound checks, using synthetic dates/users only."""

import unittest

from chatbot.policies.kql_safety import _has_bounded_time_range, inspect_query_safety


USER = "offline-user"
HEADER = (
    "let TargetUserId='offline-user'; "
    "let alpha=datetime(2025-03-02); let omega=datetime(2025-03-09); "
)


def query(source="SensorWorkoutSessions", start="alpha", end="omega", filters=""):
    return (
        HEADER + f"{source}(TargetUserId, {start}, {end}) "
        "| where UserId == TargetUserId " + filters + " | summarize Value=count()"
    )


class KqlTimeBoundsTests(unittest.TestCase):
    def assert_safe(self, text):
        result = inspect_query_safety(text, USER)
        self.assertTrue(result["is_safe"], result["reason"])

    def assert_blocked(self, text):
        self.assertFalse(inspect_query_safety(text, USER)["is_safe"])

    def assert_time_advisory(self, text):
        result = inspect_query_safety(text, USER)
        self.assertTrue(result["is_safe"], result["reason"])
        self.assertTrue(result["warnings"])
        self.assertFalse(result["should_repair"])

    def test_all_canonical_functions_use_existing_finite_arguments(self):
        for source in (
            "SensorWorkoutSessions", "SensorHealthSamples", "SensorSleepNights",
            "SensorActivityDaily", "SensorActivityHourly", "SensorActivityInterval",
        ):
            with self.subTest(source=source):
                self.assert_safe(query(source))

    def test_variable_names_do_not_determine_safety(self):
        for first, last in (("StartDate", "EndDate"), ("a", "b"), ("WindowStart", "WindowEnd")):
            with self.subTest(first=first):
                text = query().replace("alpha", first).replace("omega", last)
                self.assert_safe(text)
                explicit = text + f" | where StartLocal >= {first} and StartLocal < {last}"
                self.assertTrue(_has_bounded_time_range(explicit))

    def test_where_bounds_resolve_full_query_aliases(self):
        for first, last in (("StartDate", "EndDate"), ("a", "b")):
            context = HEADER.replace("alpha", first).replace("omega", last)
            branch = f"Rows | where DateFrom >= {first} and DateFrom < {last}"
            self.assertTrue(_has_bounded_time_range(branch, full_kql=context))

    def test_alias_chains_and_date_arithmetic(self):
        text = (
            "let TargetUserId='offline-user'; let anchor=datetime(2025-03-09); "
            "let endpoint=anchor; let beginning=endpoint-7d; "
            "SensorWorkoutSessions(TargetUserId, beginning, endpoint) "
            "| where UserId == TargetUserId | summarize Value=count()"
        )
        self.assert_safe(text)
        self.assertTrue(_has_bounded_time_range(
            "Rows | where DateFrom >= beginning and DateFrom < endpoint", full_kql=text,
        ))

    def test_between_literals_and_aliases(self):
        for bounds in ("alpha .. omega", "datetime(2025-03-02) .. datetime(2025-03-09)"):
            with self.subTest(bounds=bounds):
                self.assertTrue(_has_bounded_time_range(
                    HEADER + f"Rows | where DateFrom between ({bounds})",
                ))
                self.assert_safe(query(filters=f"| where StartLocal between ({bounds})"))

    def test_between_duration_upper_bound(self):
        self.assertTrue(_has_bounded_time_range(
            "Rows | where DateFrom between (datetime(2025-03-02) .. 7d)",
        ))

    def test_reversed_and_parenthesized_comparisons(self):
        self.assertTrue(_has_bounded_time_range(
            HEADER + "Rows | where (alpha <= DateFrom) and (omega > DateFrom)",
        ))
        self.assertTrue(_has_bounded_time_range(
            HEADER + "Rows | where (DateFrom >= alpha and DateFrom < omega)",
        ))

    def test_datetime_timestamps_are_literals_not_identifiers(self):
        self.assert_safe(query(
            start="datetime(2025-03-02T12:30:00Z)", end="datetime(2025-03-09T12:30:00Z)",
        ))

    def test_relative_and_calendar_bounds(self):
        self.assert_safe(query(start="ago(7d)", end="now()"))
        self.assert_safe(query(start="datetime_add('day', -365, now())", end="now()"))

    def test_each_union_branch_has_its_own_valid_arguments(self):
        text = HEADER + (
            "union (SensorWorkoutSessions(TargetUserId, alpha, omega) "
            "| where UserId == TargetUserId), "
            "(SensorHealthSamples(TargetUserId, alpha, omega) | where UserId == TargetUserId) "
            "| summarize Value=count()"
        )
        self.assert_safe(text)
        self.assert_time_advisory(text.replace("SensorHealthSamples(TargetUserId, alpha, omega)",
                                              "SensorHealthSamples(TargetUserId, unknown, omega)"))

    def test_user_scope_and_read_only_checks_are_unchanged(self):
        for bad in (
            query().replace("SensorWorkoutSessions(TargetUserId,", "SensorWorkoutSessions('another-user',"),
            query().replace("SensorWorkoutSessions", "HealthDataFlat"),
            ".drop table HealthDataFlat; " + query(),
        ):
            with self.subTest(query=bad):
                self.assert_blocked(bad)

    def test_canonical_account_argument_does_not_need_redundant_where_filter(self):
        self.assert_safe(query().replace("| where UserId == TargetUserId", ""))
        # A post-filter cannot expand the function's authorized account scope.
        self.assert_safe(query().replace("UserId == TargetUserId", "UserId == 'another-user'"))

    def test_date_compilation_is_deferred_but_call_scope_is_retained(self):
        for bound in ("unknown", "datetime(null)", "DateFrom"):
            with self.subTest(bound=bound):
                self.assert_time_advisory(query(start=bound))
        self.assert_blocked(query(start="toscalar(Rows | count)"))  # unauthorized source
        self.assert_blocked(query().replace("TargetUserId, alpha, omega", "TargetUserId, alpha"))

    def test_date_alias_compilation_is_not_an_authorization_decision(self):
        self.assert_time_advisory("let alpha=datetime(2025-03-01); " + query())
        text = query().replace("let alpha=datetime(2025-03-02)", "let alpha=other; let other=alpha")
        self.assert_time_advisory(text)

    def test_date_window_diagnostics_do_not_block_execution(self):
        self.assert_time_advisory(query(start="omega", end="alpha"))
        self.assert_time_advisory(query(start="datetime(1900-01-01)", end="datetime(2025-03-09)"))
        self.assert_time_advisory(query(start="ago(999999d)", end="now()"))
        self.assert_time_advisory(query(start="datetime_add('day', -999999, now())", end="now()"))

    def test_duration_budget_includes_alias_arithmetic(self):
        context = "let a=2000d; let b=2000d; let beginning=now()-a-b; "
        self.assert_time_advisory(context + query(start="beginning", end="now()"))

    def test_one_sided_or_record_relative_where_is_not_a_bound(self):
        for expression in (
            "DateFrom >= alpha", "DateFrom < omega",
            "DateFrom >= DateTo-7d and DateFrom < DateTo",
            "DateFrom >= unknown and DateFrom < omega",
        ):
            with self.subTest(expression=expression):
                self.assertFalse(_has_bounded_time_range(HEADER + "Rows | where " + expression))

    def test_or_and_not_cannot_fake_mandatory_bounds(self):
        for expression in (
            "DateFrom >= alpha or DateFrom < omega",
            "(DateFrom >= alpha and DateFrom < omega) or true",
            "not(DateFrom >= alpha and DateFrom < omega)",
            "DateFrom between (alpha .. omega) or true",
        ):
            with self.subTest(expression=expression):
                self.assertFalse(_has_bounded_time_range(HEADER + "Rows | where " + expression))

    def test_comments_and_strings_cannot_manufacture_bounds(self):
        for expression in (
            'Rows | where Label == "DateFrom >= alpha and DateFrom < omega"',
            'Rows // | where DateFrom between (alpha .. omega)',
            'Rows /* SensorWorkoutSessions("offline-user", datetime(2025-03-02), datetime(2025-03-09)) */',
        ):
            with self.subTest(expression=expression):
                self.assertFalse(_has_bounded_time_range(HEADER + expression))

    def test_bounded_nested_lookup_does_not_authorize_unbounded_outer_source(self):
        text = HEADER + (
            "Rows | where X > toscalar(SensorHealthSamples(TargetUserId, alpha, omega) "
            "| summarize max(ValueNumber)) | take 1"
        )
        self.assertFalse(_has_bounded_time_range(text))
        self.assert_blocked(text)


if __name__ == "__main__":
    unittest.main()
