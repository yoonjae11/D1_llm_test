"""Offline regression tests for bounded, re-guarded LLM syntax repair."""

from __future__ import annotations

import importlib
import unittest
from types import SimpleNamespace
from unittest.mock import patch

workflow = importlib.import_module("chatbot.nodes.d1_sensor_data.workflow")
repair = importlib.import_module("chatbot.nodes.d1_sensor_data.repair_sensor_query")
guard = importlib.import_module("chatbot.nodes.d1_sensor_data.guard_sensor_query")

VALID_QUERY = (
    'let TargetUserId="study-user";\n'
    'let StartLocal=datetime(2025-04-01);\n'
    'let EndLocal=datetime(2025-04-09);\n'
    'SensorHealthSamples(TargetUserId, StartLocal, EndLocal) '
    '| where UserId == TargetUserId and DateFrom >= StartLocal and DateFrom < EndLocal '
    '| summarize Value=max(ValueNumber)'
)
INVALID_SYNTAX_QUERY = VALID_QUERY.replace(
    "Value=max(ValueNumber)", "Value=max(datetime(DateFrom))"
)
SYNTAX_ERROR = (
    "Error (Kusto): Request is invalid and cannot be processed: "
    "Syntax error: SYN0002: DateFrom could not be parsed as a literal of type datetime"
)


class KQLRepairRoutingTests(unittest.TestCase):
    def test_dependency_hint_covers_every_operator_without_rewriting(self):
        from chatbot.policies.kql_errors import extend_dependency_hints
        query = ('Rows | extend A=f(X,Y), B=A+1 '
                 '| extend C=g(B), D=C+1, E=D+2')
        self.assertEqual(extend_dependency_hints(query), [
            'B references sibling aliases A', 'D references sibling aliases C',
            'E references sibling aliases D'])
        self.assertEqual(extend_dependency_hints(
            'Rows | extend A="B", B=1 // C=A\n| extend C=A+1'), [])
        self.assertEqual(extend_dependency_hints(
            'Rows | extend A=P.B, B=1 | extend C=A+1'), [])

    def setUp(self) -> None:
        self.settings = SimpleNamespace(kql_generate_method="llm", max_kql_repair_attempts=2)
        self.patches = [
            patch.object(module, "get_settings", return_value=self.settings)
            for module in (workflow, repair, guard)
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def _state(self, **updates):
        state = {
            "user_id": "study-user", "plan": {}, "messages": [],
            "kql_query": INVALID_SYNTAX_QUERY,
            "kql_generation_trace": {"method": "llm"}, "repair_attempts": 0,
        }
        state.update(updates)
        return state

    def test_only_synthetic_unknown_sources_are_repairable(self) -> None:
        prefix = "KQL uses a direct tabular source outside the health-chatbot allowlist: "
        for names in ("print", "iff", "iff, print"):
            with self.subTest(names=names):
                self.assertEqual(workflow.guard_decision(self._state(
                    guard_status="error", kql_guard={"issues": [prefix + names + "."]},
                )), "repair_sensor_query")
        for issues in (
            [prefix + "UnknownTable."],
            [prefix + "print, UnknownTable."],
            [prefix + "print.", "KQL contains external data access."],
            ["KQL contains a management or write command."],
            ["KQL redefines or misbinds TargetUserId instead of using the authenticated user."],
        ):
            with self.subTest(issues=issues):
                self.assertEqual(workflow.guard_decision(self._state(
                    guard_status="error", kql_guard={"issues": issues},
                )), "produce_sensor_result")

    def test_one_shared_budget_respects_lower_configuration_and_state(self) -> None:
        self.assertEqual(repair.repair_attempt_limit({}), 1)
        self.assertEqual(repair.repair_attempt_limit({"max_repair_attempts": 5}), 1)
        self.assertEqual(repair.repair_attempt_limit({"max_repair_attempts": 0}), 0)
        self.settings.max_kql_repair_attempts = 0
        self.assertEqual(repair.repair_attempt_limit({"max_repair_attempts": 5}), 0)
        self.settings.max_kql_repair_attempts = 2
        self.assertEqual(workflow.guard_decision(self._state(
            guard_status="repair", repair_attempts=1,
        )), "produce_sensor_result")

    def test_execution_repairs_only_explicit_compiler_errors(self) -> None:
        base = self._state(guard_status="execute", execution_status="error")
        for error in (SYNTAX_ERROR, "Error (Kusto): SYN0002: invalid expression",
                      "Error (Kusto): Semantic error: SEM0100: column missing",
                      "Error (Kusto): SEM0104: operator source expression invalid"):
            with self.subTest(error=error):
                self.assertEqual(workflow.execution_decision({**base, "error": error}),
                                 "repair_sensor_query")
        for error in (
            "Error (Kusto): Unauthorized", "Request timed out", "Connection failed",
            "Error (Kusto): unspecified request failed",
            "Error (Kusto): Syntax error: authentication failed",
            "Error (Kusto): Syntax error: 503 temporarily unavailable",
        ):
            with self.subTest(error=error):
                self.assertEqual(workflow.execution_decision({**base, "error": error}),
                                 "produce_sensor_result")
        for updates in (
            {"repair_attempts": 1}, {"guard_status": "error"},
            {"execution_status": "success"},
            {"kql_generation_trace": {"method": "deterministic"}},
        ):
            self.assertEqual(workflow.execution_decision({**base, "error": SYNTAX_ERROR, **updates}),
                             "produce_sensor_result")

    def test_synthetic_source_repairs_reenter_full_guard_before_first_execution(self) -> None:
        for suffix in ("iff(true, Rows, Rows) | take 1", "Rows | union (print Value=real(null))"):
            with self.subTest(suffix=suffix):
                initial = VALID_QUERY.rsplit("SensorHealthSamples", 1)
                query = initial[0] + "let Rows=SensorHealthSamples" + initial[1] + ";\n" + suffix
                decision = guard.guard_sensor_query(self._state(kql_query=query))
                self.assertEqual(decision["guard_status"], "error")
                self.assertEqual(len(decision["kql_guard"]["issues"]), 1)
                seen = []

                def re_guard(state):
                    seen.append(state["kql_query"])
                    return guard.guard_sensor_query(state)

                with (
                    patch.object(workflow, "plan_sensor_query", side_effect=lambda state: {"plan": {}}),
                    patch.object(workflow, "generate_sensor_query", side_effect=lambda state: {}),
                    patch.object(workflow, "guard_sensor_query", side_effect=re_guard),
                    patch.object(workflow, "execute_sensor_query", return_value={
                        "execution_status": "success", "error": None,
                        "query_result": '[{"Value": 17}]',
                    }) as execute,
                    patch.object(repair, "repair_query_with_llm", return_value=VALID_QUERY) as model,
                ):
                    result = workflow.build_d1_graph().invoke(self._state(kql_query=query))
                self.assertEqual(seen, [query, VALID_QUERY])
                self.assertEqual(execute.call_count, 1)
                self.assertEqual(execute.call_args.args[0]["kql_query"], VALID_QUERY)
                self.assertEqual(model.call_count, 1)
                self.assertEqual(result["kql_generation_trace"]["repair_history"][0]["origin"],
                                 "guard")

    def _run_graph(self, repaired_query: str, repeated_failure: bool = False):
        calls = []
        guard_calls = []

        def execute(state):
            calls.append(state["kql_query"])
            if len(calls) == 1 or repeated_failure:
                return {"execution_status": "error", "error": SYNTAX_ERROR,
                        "query_result": "[]"}
            self.assertIsNone(state.get("error"))
            return {"execution_status": "success", "error": None,
                    "query_result": '[{"Value": 17}]'}

        def re_guard(state):
            guard_calls.append(state["kql_query"])
            return guard.guard_sensor_query(state)

        with (
            patch.object(workflow, "plan_sensor_query", side_effect=lambda state: {"plan": {}}),
            patch.object(workflow, "generate_sensor_query", side_effect=lambda state: {}),
            patch.object(workflow, "guard_sensor_query", side_effect=re_guard),
            patch.object(workflow, "execute_sensor_query", side_effect=execute),
            patch.object(repair, "repair_query_with_llm", return_value=repaired_query) as model,
        ):
            result = workflow.build_d1_graph().invoke(self._state())
            return result, calls, guard_calls, model.call_count

    def test_server_syntax_repair_reenters_guard_and_preserves_trace(self) -> None:
        result, calls, guard_calls, model_calls = self._run_graph(VALID_QUERY)
        self.assertEqual(calls, [INVALID_SYNTAX_QUERY, VALID_QUERY])
        self.assertEqual(guard_calls, [INVALID_SYNTAX_QUERY, VALID_QUERY])
        self.assertEqual(model_calls, 1)
        self.assertEqual(result["execution_status"], "success")
        self.assertEqual(result["repair_attempts"], 1)
        trace = result["kql_generation_trace"]
        self.assertEqual(trace["pre_guard_kql"], INVALID_SYNTAX_QUERY)
        self.assertEqual(trace["final_kql"], VALID_QUERY)
        self.assertTrue(trace["guard_pass"])
        self.assertEqual(trace["repair_history"][0], {
            "attempt": 1, "origin": "adx_validation", "failed_kql": INVALID_SYNTAX_QUERY,
            "error": SYNTAX_ERROR,
        })

    def test_unsafe_repair_is_never_executed(self) -> None:
        result, calls, guard_calls, model_calls = self._run_graph(".drop table PrivateTable")
        self.assertEqual(calls, [INVALID_SYNTAX_QUERY])
        self.assertEqual(len(guard_calls), 2)
        self.assertEqual(model_calls, 1)
        self.assertEqual(result["guard_status"], "error")
        self.assertFalse(result["kql_generation_trace"]["guard_pass"])
        self.assertIsNone(result["kql_generation_trace"]["final_kql"])

    def test_second_server_failure_does_not_get_another_repair(self) -> None:
        result, calls, guard_calls, model_calls = self._run_graph(VALID_QUERY, repeated_failure=True)
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(guard_calls), 2)
        self.assertEqual(model_calls, 1)
        self.assertEqual(result["execution_status"], "error")

    def test_empty_repair_is_guarded_and_does_not_retry_original(self) -> None:
        result, calls, guard_calls, model_calls = self._run_graph("")
        self.assertEqual(calls, [INVALID_SYNTAX_QUERY])
        self.assertEqual(guard_calls, [INVALID_SYNTAX_QUERY, ""])
        self.assertEqual(model_calls, 1)
        self.assertEqual(result["guard_status"], "error")


if __name__ == "__main__":
    unittest.main()
