"""Missing observations are null; real zeros and execution failures are distinct."""

import unittest

from langchain_core.messages import HumanMessage

from chatbot.nodes.common.generate_final_response import _generate_standard_response
from chatbot.policies.result_processing import (
    _filter_meaningless_metrics,
    normalize_result_values,
)


class SensorMissingValueTests(unittest.TestCase):
    def test_default_empty_aggregate_is_null_only_with_explicit_no_evidence(self):
        for reason in ("no_metric_evidence", "no_condition_evidence", "insufficient_time_resolution",
                       "insufficient_observations", "insufficient_unit_evidence"):
            row = {"Metric": "distance", "Value": 0, "EvidenceRows": 0, "ResultStatus": reason}
            result = normalize_result_values(row)
            self.assertIsNone(result["Value"])
            self.assertEqual(result["EvidenceRows"], 0)
            self.assertEqual(row["Value"], 0)  # Do not mutate actual ADX evidence.

    def test_recorded_zero_and_zero_count_survive_response_display(self):
        for metric, aggregation in (("distance", "sum"), ("workout_session", "count")):
            row = {"Metric": metric, "Aggregation": aggregation, "Value": 0,
                   "EvidenceRows": 0 if aggregation == "count" else 1,
                   "ResultStatus": "ok"}
            self.assertEqual(_filter_meaningless_metrics([row]), [row])

    def _state(self, **updates):
        state = {"intent": "query", "messages": [HumanMessage(content="A sensor question")],
                 "plan": {"metrics": ["distance"]}, "query_result": "[]",
                 "execution_status": "success", "coverage_context": {}}
        state.update(updates)
        return state

    def test_successful_empty_query_displays_null(self):
        for coverage in ({}, {"recheck_status": "no_evidence_after_recheck"}):
            result = _generate_standard_response(self._state(coverage_context=coverage))
            self.assertEqual(result["messages"][-1].content, "null")

    def test_completed_missing_metric_retains_known_entity_in_domain_context(self):
        from chatbot.nodes.common.aggregate_domain_results import aggregate_domain_results
        from chatbot.nodes.d1_sensor_data.produce_sensor_result import produce_sensor_result
        row = {"Metric": "distance", "Period": "2025-04-03", "Value": None,
               "EvidenceRows": 0, "ResultStatus": "no_metric_evidence"}
        domain = produce_sensor_result(self._state(query_result=[row]))["d1_result"]
        context = aggregate_domain_results({"domain_results": {"D1": domain}})["response_domain_context"]["D1"]
        self.assertEqual(context["status"], "insufficient_information")
        self.assertEqual(context["structured_result"]["records"], [row])

    def test_unexecuted_missing_metric_is_not_retrieval_evidence(self):
        from chatbot.nodes.common.aggregate_domain_results import aggregate_domain_results
        domain = {"domain": "D1", "status": "insufficient_information",
                  "structured_result": {"records": [{"Period": "2025-04-03"}]},
                  "provenance": {"execution_status": "needs_clarification"}}
        context = aggregate_domain_results({"domain_results": {"D1": domain}})["response_domain_context"]["D1"]
        self.assertEqual(context["structured_result"], {})

    def test_missing_condition_is_not_a_successful_measurement(self):
        from chatbot.nodes.d1_sensor_data.produce_sensor_result import produce_sensor_result
        for reason in ("no_condition_evidence", "insufficient_observations", "insufficient_unit_evidence"):
            row = {"Metric": "distance", "Value": None, "EvidenceRows": 0, "ResultStatus": reason}
            result = produce_sensor_result(self._state(query_result=[row]))["d1_result"]
            self.assertEqual(result["status"], "insufficient_information")
            self.assertEqual(result["structured_result"]["records"], [row])
            self.assertTrue(result["missing_information"])

    def test_query_or_generation_failure_is_not_reported_as_missing_value(self):
        for updates in (
            {"execution_status": "error", "error": "Error (Kusto): SEM0104"},
            {"coverage_context": {"recheck_status": "generation_failed"}},
            {"coverage_context": {"recheck_status": "adx_failed"}},
        ):
            result = _generate_standard_response(self._state(**updates))
            self.assertNotEqual(result["messages"][-1].content, "null")


if __name__ == "__main__":
    unittest.main()
