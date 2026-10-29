"""Source-neutral, offline contracts for LLM daily/flat validation repair."""

from __future__ import annotations

import unittest
import json
from types import SimpleNamespace
from unittest.mock import patch

from langchain_core.runnables import RunnableLambda

from chatbot.nodes.d1_sensor_data import daily_flat_recheck as recheck
from chatbot.policies.kql_safety import inspect_query_safety


PLAN = {
    "metrics": ["steps"], "granularity": "daily",
    "time_range": {"mode": "absolute", "anchor": "absolute",
                   "start_date": "2025-04-01", "end_date": "2025-04-08"},
}
CANDIDATE = (
    'let TargetUserId="study-user";\n'
    'let StartLocal=datetime(2025-04-01);\nlet EndLocal=datetime(2025-04-09);\n'
    'let D=SensorActivityDaily(TargetUserId, StartLocal, EndLocal) '
    '| where UserId == TargetUserId and LocalDate >= StartLocal and LocalDate < EndLocal '
    '| project Period=LocalDate, Value=todouble(ValueSumExact), Type;\n'
    'let F=SensorHealthSamples(TargetUserId, StartLocal, EndLocal) '
    '| where UserId == TargetUserId and DateFrom >= StartLocal and DateFrom < EndLocal '
    '| project Period=startofday(DateFrom), Value=todouble(ValueNumber), Type;\n'
    'D | union F | where Type == "STEPS" | summarize Value=max(Value) by Period '
    '| sort by Period asc | take 40'
)
REPAIRED = CANDIDATE.replace("take 40", "take 30")
FLAT_ONLY = (
    'let TargetUserId="study-user";\n'
    'let StartLocal=datetime(2025-04-01);\nlet EndLocal=datetime(2025-04-09);\n'
    'SensorHealthSamples(TargetUserId, StartLocal, EndLocal) '
    '| where UserId == TargetUserId and DateFrom >= StartLocal and DateFrom < EndLocal '
    '| where Type == "STEPS" and isnotnull(ValueNumber) '
    '| summarize Value=sum(ValueNumber)'
)
SEMANTIC_ERROR = (
    "Error (Kusto): Request is invalid and cannot be processed: "
    "Semantic error: SEM0104: Operator source expression should be table or column"
)
GAPS = [{"Day": "2025-04-02", "Type": "STEPS", "FlatRows": 1,
         "DayTotalRows": 1, "IntervalRows": 0, "Sources": 1}]
ROWS = [{"Period": "2025-04-02", "Value": 3100}]


class DailyFlatRecheckRepairTests(unittest.TestCase):
    def _run(self, responses, *, repair_result=REPAIRED, candidate=CANDIDATE, limit=2, gaps=GAPS,
             generation_error=None):
        calls = []
        queue = [gaps, *responses]

        def executor(query):
            calls.append(query)
            response = queue.pop(0)
            if isinstance(response, Exception):
                raise response
            return response

        def model(_):
            if generation_error is not None:
                raise generation_error
            return candidate

        with (
            patch.object(recheck, "llm", RunnableLambda(model)),
            patch.object(recheck, "get_settings", return_value=SimpleNamespace(
                max_kql_repair_attempts=limit,
            )),
            patch.object(recheck, "repair_query_with_llm",
                         **({"side_effect": repair_result} if isinstance(repair_result, Exception)
                            else {"return_value": repair_result})) as repair,
            patch.object(recheck, "inspect_query_safety", wraps=inspect_query_safety) as guard,
        ):
            outcome = recheck.recheck_daily_with_flat(
                plan=PLAN, user_id="study-user", question="Summarize the recorded activity.",
                primary_kql="SensorActivityDaily(TargetUserId, StartLocal, EndLocal)",
                primary_rows=[], execute_query=executor,
            )
        return outcome, calls, repair, guard

    def test_semantic_error_is_repaired_and_fully_guarded(self) -> None:
        outcome, calls, repair, guard = self._run([SEMANTIC_ERROR, ROWS])
        self.assertEqual(len(calls), 3)  # probe, original candidate, repaired candidate
        self.assertEqual(calls[1:], [CANDIDATE, REPAIRED])
        self.assertEqual(repair.call_count, 1)
        self.assertIn("SEM0104", repair.call_args.args[3])
        self.assertEqual(guard.call_count, 3)
        self.assertEqual(outcome["coverage"]["recheck_status"], "completed")
        self.assertEqual(outcome["pre_guard_kql"], CANDIDATE)
        self.assertEqual(outcome["kql"], REPAIRED)
        self.assertEqual(outcome["llm_repair_attempts"], 1)
        self.assertEqual(outcome["repair_history"][0]["failed_kql"], CANDIDATE)
        self.assertTrue(outcome["repair_history"][0]["guard_pass"])

    def test_thrown_validation_exception_uses_same_bounded_repair(self) -> None:
        outcome, calls, repair, _ = self._run([RuntimeError(SEMANTIC_ERROR), ROWS])
        self.assertEqual(len(calls), 3)
        self.assertEqual(repair.call_count, 1)
        self.assertEqual(outcome["coverage"]["recheck_status"], "completed")

    def test_outages_auth_and_unknown_errors_never_trigger_llm_repair(self) -> None:
        for error in (
            "Error (Kusto): Unauthorized", "Error: Request timed out",
            "Error: An unspecified request failed",
            "Error (Kusto): Semantic error: 503 service unavailable",
            RuntimeError("Network connection failed"),
        ):
            with self.subTest(error=error):
                outcome, calls, repair, _ = self._run([error])
                self.assertEqual(len(calls), 2)
                self.assertEqual(repair.call_count, 0)
                self.assertEqual(outcome["coverage"]["recheck_status"], "adx_failed")
                self.assertNotIn("rows", outcome)

    def test_unsafe_repair_never_reaches_adx(self) -> None:
        malicious = CANDIDATE + "; .drop table PrivateTable"
        outcome, calls, repair, guard = self._run([SEMANTIC_ERROR], repair_result=malicious)
        self.assertEqual(len(calls), 2)
        self.assertEqual(repair.call_count, 1)
        self.assertEqual(guard.call_count, 3)
        self.assertEqual(outcome["coverage"]["recheck_status"], "guard_rejected")
        self.assertFalse(outcome["repair_history"][0]["guard_pass"])
        self.assertNotIn("rows", outcome)

    def test_advisories_do_not_consume_server_repair_budget(self) -> None:
        # Query-style advisories no longer consume an LLM repair before ADX.
        # Actual server failures still share the existing single repair budget.
        advisory_candidate = CANDIDATE + "\n| top 3 by Period desc, Value desc"
        decision = inspect_query_safety(advisory_candidate, "study-user")
        self.assertTrue(decision["is_safe"])
        self.assertTrue(decision["warnings"])
        self.assertFalse(decision["should_repair"])
        outcome, calls, repair, _ = self._run(
            [SEMANTIC_ERROR, SEMANTIC_ERROR], candidate=advisory_candidate,
        )
        self.assertEqual(len(calls), 3)  # probe, candidate, one server repair
        self.assertEqual(calls[1], advisory_candidate)
        self.assertEqual(calls[-1], REPAIRED)
        self.assertEqual(repair.call_count, 1)
        self.assertEqual(outcome["llm_repair_attempts"], 1)
        self.assertEqual(outcome["repair_history"][0]["origin"], "adx_validation")
        self.assertEqual(outcome["coverage"]["recheck_status"], "adx_failed")

    def test_finite_function_bounds_do_not_need_redundant_upper_filter(self) -> None:
        candidate = CANDIDATE.replace(" and DateFrom < EndLocal", "")
        outcome, calls, repair, _ = self._run([ROWS], candidate=candidate)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[-1], candidate)
        repair.assert_not_called()
        self.assertEqual(outcome["coverage"]["recheck_status"], "completed")

    def test_second_validation_error_and_disabled_budget_stop(self) -> None:
        outcome, calls, repair, _ = self._run([SEMANTIC_ERROR, SEMANTIC_ERROR])
        self.assertEqual(len(calls), 3)
        self.assertEqual(repair.call_count, 1)
        self.assertEqual(outcome["coverage"]["recheck_status"], "adx_failed")
        outcome, calls, repair, _ = self._run([SEMANTIC_ERROR], limit=0)
        self.assertEqual(len(calls), 2)
        self.assertEqual(repair.call_count, 0)
        self.assertEqual(outcome["llm_repair_attempts"], 0)

    def test_empty_repair_is_guarded_but_never_executed(self) -> None:
        outcome, calls, repair, guard = self._run([SEMANTIC_ERROR], repair_result="")
        self.assertEqual(len(calls), 2)
        self.assertEqual(repair.call_count, 1)
        self.assertEqual(guard.call_count, 3)
        self.assertEqual(outcome["coverage"]["recheck_status"], "missing_required_evidence_stream")
        self.assertFalse(outcome["repair_history"][0]["guard_pass"])

    def test_confirmed_zero_daily_coverage_allows_flat_only(self) -> None:
        gaps = [{**GAPS[0], "TotalUsableDailyRows": 0}]
        outcome, calls, repair, guard = self._run([ROWS], candidate=FLAT_ONLY, gaps=gaps)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[-1], FLAT_ONLY)
        self.assertEqual(repair.call_count, 0)
        self.assertEqual(guard.call_count, 2)
        self.assertEqual(outcome["coverage"]["daily_evidence_state"], "confirmed_empty")
        self.assertEqual(outcome["coverage"]["daily_usable_rows_in_window"], 0)
        self.assertEqual(outcome["coverage"]["recheck_status"], "completed")

    def test_mixed_or_unconfirmed_daily_coverage_requires_daily_merge(self) -> None:
        for gaps in (
            GAPS,
            [{**GAPS[0], "TotalUsableDailyRows": 3}],
            [{**GAPS[0], "TotalUsableDailyRows": 0}, {**GAPS[0], "TotalUsableDailyRows": 2}],
            [{**GAPS[0], "TotalUsableDailyRows": float("nan")}],
        ):
            with self.subTest(gaps=gaps):
                outcome, calls, repair, _ = self._run([], candidate=FLAT_ONLY, gaps=gaps)
                self.assertEqual(len(calls), 1)
                self.assertEqual(repair.call_count, 0)
                self.assertEqual(outcome["coverage"]["recheck_status"], "missing_required_evidence_stream")
                self.assertNotIn("rows", outcome)
        outcome, calls, _, _ = self._run([ROWS], gaps=[{**GAPS[0], "TotalUsableDailyRows": 3}])
        self.assertEqual(len(calls), 2)
        self.assertEqual(outcome["coverage"]["recheck_status"], "completed")
        self.assertIn("SensorActivityDaily", outcome["kql"])
        self.assertIn("SensorHealthSamples", outcome["kql"])

    def test_comment_is_not_a_required_evidence_stream(self) -> None:
        query = FLAT_ONLY + "\n// SensorActivityDaily(TargetUserId, StartLocal, EndLocal)"
        outcome, calls, _, _ = self._run([], candidate=query)
        self.assertEqual(len(calls), 1)
        self.assertEqual(outcome["coverage"]["recheck_status"], "missing_required_evidence_stream")

    def test_probe_emits_whole_window_daily_evidence_count(self) -> None:
        query = recheck.build_daily_flat_probe(
            PLAN, "study-user", "SensorActivityDaily(TargetUserId, StartLocal, EndLocal)",
        )
        self.assertIn("TotalUsableDailyRows=toscalar(Daily | summarize Total=sum(DailyRows))", query)
        self.assertTrue(inspect_query_safety(query, "study-user")["is_safe"])

    def test_known_generation_diagnostics_are_useful_without_credentials(self) -> None:
        for error, kind in (
            ("Selected model returned an incomplete/failed response", "model_incomplete_response"),
            ("Selected model returned no final text (possibly exhausted its reasoning budget)", "model_no_final_text"),
            ("Model gpt-5 (responses): HTTP 429; check deployment, endpoint, key, quota and supported parameters", "model_http_error"),
            ("Model gpt-5: network/timeout error", "model_network_timeout"),
        ):
            with self.subTest(kind=kind):
                outcome, calls, repair, _ = self._run([], generation_error=RuntimeError(error))
                self.assertEqual(len(calls), 1)
                self.assertEqual(repair.call_count, 0)
                self.assertEqual(outcome["coverage"]["recheck_status"], "generation_failed")
                self.assertEqual(outcome["generation_error_type"], "RuntimeError")
                self.assertEqual(outcome["generation_error_kind"], kind)
                self.assertTrue(outcome["generation_error_reason"])

    def test_unknown_generation_and_repair_exceptions_redact_arbitrary_text(self) -> None:
        secret = "untrusted-message-with-mock-secret"
        outcome, calls, repair, _ = self._run([], generation_error=RuntimeError(secret))
        self.assertEqual(len(calls), 1)
        self.assertEqual(repair.call_count, 0)
        self.assertEqual(outcome["generation_error_kind"], "unrecognized_exception")
        self.assertNotIn(secret, json.dumps(outcome))
        outcome, calls, repair, _ = self._run([SEMANTIC_ERROR], repair_result=RuntimeError(secret))
        self.assertEqual(len(calls), 2)
        self.assertEqual(repair.call_count, 1)
        self.assertEqual(outcome["coverage"]["recheck_status"], "repair_failed")
        self.assertNotIn(secret, json.dumps(outcome))

    def test_successful_null_or_empty_results_are_preserved_without_retry(self) -> None:
        for rows in ([{"Value": None, "EvidenceRows": 0, "ResultStatus": "no_metric_evidence"}], []):
            with self.subTest(rows=rows):
                outcome, calls, repair, _ = self._run([rows])
                self.assertEqual(len(calls), 2)
                self.assertEqual(repair.call_count, 0)
                self.assertEqual(outcome["coverage"]["recheck_status"], "no_evidence_after_recheck")
                self.assertTrue(outcome["execution_completed"])
                self.assertEqual(outcome["rows"], rows)
                self.assertEqual(outcome["kql"], CANDIDATE)
                self.assertNotIn("daily_flat_gap_resolved", outcome["coverage"])


if __name__ == "__main__":
    unittest.main()
