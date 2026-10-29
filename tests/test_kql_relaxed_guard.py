"""Synthetic examples: query style is advisory, authorization stays mandatory."""

import unittest

from chatbot.policies.kql_safety import inspect_query_safety


USER = "guard-example-user"
HEADER = (
    "let TargetUserId='guard-example-user'; "
    "let Begin=datetime(2025-04-02); let Finish=datetime(2025-04-09); "
)
SOURCE = (
    "SensorHealthSamples(TargetUserId, Begin, Finish) "
    "| where UserId == TargetUserId "
)


class RelaxedGuardTests(unittest.TestCase):
    def assert_allowed(self, query):
        decision = inspect_query_safety(query, USER)
        self.assertTrue(decision["is_safe"], decision["reason"])
        self.assertFalse(decision["should_repair"])
        return decision

    def assert_rejected(self, query):
        decision = inspect_query_safety(query, USER)
        self.assertFalse(decision["is_safe"])
        return decision

    def test_dynamic_string_arrays(self):
        for array in ('dynamic(["A", "B"])', "dynamic(['A', 'B'])"):
            with self.subTest(array=array):
                self.assert_allowed(HEADER + f"let Labels={array}; " + SOURCE
                                    + "| where Type in~ (Labels) | summarize Value=count()")

    def test_json_fields_and_quoted_output_columns(self):
        self.assert_allowed(HEADER + SOURCE
                            + '| extend speed=todouble(Value["speed"]) '
                            + '| summarize ["Mean speed"]=avg(speed)')

    def test_dynamic_dictionary_and_nested_arrays(self):
        self.assert_allowed(HEADER
                            + 'let Meta=dynamic({"labels":["A", "B"]}); '
                            + SOURCE + '| where Type in~ (Meta["labels"]) '
                            + '| summarize Value=count()')

    def test_no_result_shape_rule(self):
        decision = self.assert_allowed(HEADER + SOURCE + "| project ValueNumber")
        self.assertTrue(decision["warnings"])

    def test_date_alias_is_not_a_security_error(self):
        self.assert_allowed(HEADER + SOURCE
                            + "| summarize Value=sum(ValueNumber) by date=startofday(DateFrom)")

    def test_multi_sort_syntax_is_left_to_adx(self):
        decision = self.assert_allowed(HEADER + SOURCE
                                      + "\n| top 3 by DateFrom desc, DateTo desc")
        self.assertTrue(decision["warnings"])

    def test_multiple_result_statements_are_repairable(self):
        # Only the first result table is read, so a second one would be
        # silently ignored. It is not dangerous: the model rewrites it once.
        decision = self.assert_rejected(HEADER + SOURCE + "| count; print Value=0")
        self.assertTrue(decision["should_repair"])
        self.assertIn("more than one result statement", decision["reason"])
        decision = self.assert_rejected(HEADER + SOURCE + "| project DateFrom | as Days; "
                                        + SOURCE + "| where DateFrom in (Days) | count")
        self.assertTrue(decision["should_repair"])
        self.assert_allowed(HEADER + "let Days = " + SOURCE + "| project DateFrom; "
                            + SOURCE + "| where DateFrom in (Days) | summarize Value=count()")

    def test_writes_still_rejected(self):
        for command in (".drop table HealthDataFlat", ".delete table HealthDataFlat records <| HealthDataFlat"):
            self.assert_rejected(HEADER + SOURCE + "| count; " + command)

    def test_other_user_still_rejected_in_every_call(self):
        self.assert_rejected(HEADER + SOURCE.replace("TargetUserId, Begin", "'other-user', Begin") + "| count")
        self.assert_rejected(HEADER + SOURCE + "| count; "
                             + "SensorHealthSamples('other-user', Begin, Finish) | count")

    def test_unapproved_raw_and_external_sources_still_rejected(self):
        for suffix in (
            "; PrivateTable | take 1",
            "; externaldata(x:string)['https://example.invalid/data']",
            "; database('Other').PrivateTable | take 1",
        ):
            self.assert_rejected(HEADER + SOURCE + "| count" + suffix)

    def test_quoted_tables_still_rejected_only_in_source_positions(self):
        for suffix in (
            '; ["PrivateTable"] | take 1',
            '; let Hidden=["PrivateTable"]; Hidden | take 1',
            '; /* comment */ let Hidden=["PrivateTable"]; Hidden | take 1',
            '; // comment\n ["PrivateTable"] | take 1',
            '| union (["PrivateTable"] | take 1)',
            '| join kind=inner (["PrivateTable"] | take 1) on UserId',
            '| extend X=toscalar(["PrivateTable"] | count)',
            '| union (SensorHealthSamples(TargetUserId, Begin, Finish) | where UserId == TargetUserId), ["PrivateTable"]',
        ):
            with self.subTest(suffix=suffix):
                self.assert_rejected(HEADER + SOURCE + suffix + " | take 1")

    def test_column_named_search_is_not_a_search_operator(self):
        self.assert_allowed(HEADER + SOURCE + "| project search=Type | take 1")
        self.assert_rejected(HEADER + SOURCE + "| count; search * | take 1")


if __name__ == "__main__":
    unittest.main()
