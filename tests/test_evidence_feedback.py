"""The empty-evidence step reports observed facts; it never picks a measurement."""
import json
import unittest
from unittest import mock

from chatbot.nodes.d1_sensor_data import workflow
from chatbot.policies.evidence_feedback import (
    feedback_message, result_has_no_evidence, unobserved_selection,
)

OBSERVED = {"status": "observed", "observations": [
    {"Type": "RESTING_HEART_RATE", "RowCount": 30},
    {"Type": "STEPS", "RowCount": 900},
    {"Type": "HEART_RATE", "RowCount": 0},
    {"Relation": "SensorWorkoutSessions", "ActivityNames": ["Run", "Outdoor Bike"]},
]}
EMPTY = json.dumps([{"Value": None, "EvidenceRows": 0}])


class EvidenceFeedbackTest(unittest.TestCase):
    def test_empty_results(self):
        self.assertTrue(result_has_no_evidence("[]"))
        self.assertTrue(result_has_no_evidence(EMPTY))
        self.assertFalse(result_has_no_evidence(json.dumps([{"Value": 0, "EvidenceRows": 3}])))
        self.assertFalse(result_has_no_evidence(json.dumps([{"Value": 61.0}])))

    def test_flags_only_fully_unobserved_selection(self):
        finding = unobserved_selection("X | where Type == 'HEART_RATE'", OBSERVED)
        self.assertEqual(finding["types"], ["HEART_RATE"])
        self.assertIn("RESTING_HEART_RATE", finding["observed_types"])
        # One observed type among those selected: the query is the model's choice.
        self.assertIsNone(unobserved_selection(
            "X | where Type in ('HEART_RATE', 'RESTING_HEART_RATE')", OBSERVED))
        self.assertEqual(unobserved_selection(
            'X | where ActivityName == "Running"', OBSERVED)["labels"], ["RUNNING"])
        self.assertIsNone(unobserved_selection('X | where ActivityName == "Run"', OBSERVED))
        self.assertIsNone(unobserved_selection("X | where Type == 'HEART_RATE'",
                                               {"status": "unavailable"}))

    def test_message_lists_facts_without_choosing(self):
        message = feedback_message(unobserved_selection("X | where Type == 'HEART_RATE'", OBSERVED))
        self.assertIn("HEART_RATE", message)
        self.assertIn("RESTING_HEART_RATE", message)
        self.assertIn("keep the empty result", message)

    def _state(self, **overrides):
        state = {"execution_status": "success", "guard_status": "execute",
                 "query_result": EMPTY, "kql_query": "X | where Type == 'HEART_RATE'",
                 "kql_generation_trace": {"method": "llm",
                                          "source_review": {"schema_observations": OBSERVED}}}
        state.update(overrides)
        return state

    def test_routes_back_to_generation_once(self):
        self.assertEqual(workflow.execution_decision(self._state()), "request_evidence_feedback")
        self.assertEqual(workflow.execution_decision(self._state(evidence_feedback_used=True)),
                         "produce_sensor_result")
        self.assertEqual(workflow.execution_decision(self._state(
            query_result=json.dumps([{"Value": 70.0, "EvidenceRows": 4}]))), "produce_sensor_result")
        deterministic = self._state(kql_generation_trace={
            "method": "deterministic", "source_review": {"schema_observations": OBSERVED}})
        self.assertEqual(workflow.execution_decision(deterministic), "produce_sensor_result")
        request = workflow.request_evidence_feedback(self._state())["evidence_feedback_request"]
        self.assertIn("Earlier candidate:", request["message"])

    def test_generation_uses_request_only(self):
        from chatbot.nodes.d1_sensor_data import generate_sensor_query as node
        seen = {}

        def fake(user_id, question, plan, method=None, query_feedback=None):
            seen["feedback"] = query_feedback
            return {"status": "success", "kql_query": "Y", "kql_generation_trace": {"method": "llm"}}

        with mock.patch.object(node, "generate_kql_candidate", fake), \
                mock.patch.object(node, "_latest_question", lambda state: "q"):
            # Leftover state from an earlier turn must not trigger feedback.
            node.generate_sensor_query({**self._state(), "plan": {}})
            self.assertIsNone(seen["feedback"])
            out = node.generate_sensor_query({**self._state(), "plan": {},
                                              "evidence_feedback_request": {"message": "M"}})
            self.assertEqual(seen["feedback"], "M")
            self.assertTrue(out["evidence_feedback_used"])
            self.assertIsNone(out["execution_status"])
            self.assertEqual(out["kql_generation_trace"]["evidence_feedback"], {"message": "M"})


if __name__ == "__main__":
    unittest.main()
