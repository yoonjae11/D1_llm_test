"""Account-scope problems block execution but may be rewritten; writes may not."""

import unittest

from chatbot.policies.kql_safety import inspect_query_safety

USER = "41"
BODY = " | summarize EvidenceRows=count() | project Value=todouble(EvidenceRows), EvidenceRows"


class ScopeRepairTests(unittest.TestCase):
    def test_scope_violations_block_but_are_repairable(self):
        for query in (
            "SensorWorkoutSessions('999', datetime(2023-10-01), datetime(2023-10-02))" + BODY,
            "SensorWorkoutSessions(tostring(41), datetime(2023-10-01), datetime(2023-10-02))" + BODY,
            "let TargetUserId='999';\nSensorWorkoutSessions(TargetUserId, datetime(2023-10-01), "
            "datetime(2023-10-02))" + BODY,
        ):
            decision = inspect_query_safety(query, USER)
            self.assertFalse(decision["is_safe"], query)
            self.assertTrue(decision["scope_violation"], query)
            self.assertTrue(decision["should_repair"], query)

    def test_authenticated_user_is_allowed(self):
        query = "SensorWorkoutSessions('41', datetime(2023-10-01), datetime(2023-10-02))" + BODY
        self.assertTrue(inspect_query_safety(query, USER)["is_safe"])

    def test_writes_and_external_access_are_never_repaired(self):
        for query in (".drop table HealthDataFlat",
                      "externaldata(x:string)['https://example.invalid']" + BODY):
            decision = inspect_query_safety(query, USER)
            self.assertFalse(decision["is_safe"], query)
            self.assertFalse(decision["should_repair"], query)


if __name__ == "__main__":
    unittest.main()
