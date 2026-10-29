"""PHIA D1 evaluation rules live in run_phia_all.py only, never in the chatbot."""

import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from langchain_core.messages import HumanMessage


class ChatbotHasNoEvaluationSwitchTests(unittest.TestCase):
    def setUp(self):
        self.planner = importlib.import_module("chatbot.nodes.planning.plan_domain_execution")
        self.state = {"messages": [HumanMessage(content="A synthetic sensor-data question")],
                      "user_id": "synthetic-account"}

    def test_environment_cannot_bypass_domain_routing(self):
        raw = {"intent": "data", "selected_domains": ["D3"],
               "domain_requirements": {"D3": {"need": "General health information"}},
               "dependencies": {"D3": []}, "execution_stages": [["D3"]]}
        with patch.dict(os.environ, {"SENSEI_D1_EVALUATION": "1", "SENSEI_EVAL_ROUTING": "d1"}), \
                patch.object(self.planner.support, "call_chat_completion",
                             return_value=json.dumps(raw)) as model:
            result = self.planner.plan_domain_execution(self.state)
        model.assert_called_once()
        self.assertEqual(result["domain_execution_plan"]["selected_domains"], ["D3"])
        self.assertEqual(result["orchestration_trace"]["planner_llm_calls"], 1)

    def test_chat_payload_cannot_enable_evaluation(self):
        state = {**self.state, "inbound_payload": {"SENSEI_D1_EVALUATION": "1", "d1_only": True}}
        with patch.object(self.planner.support, "call_chat_completion", return_value="not JSON") as model:
            result = self.planner.plan_domain_execution(state)
        model.assert_called_once()
        self.assertEqual(result["orchestration_trace"]["planner_status"], "error")
        self.assertEqual(result["domain_execution_plan"]["selected_domains"], [])

    def test_chatbot_allows_one_kql_repair(self):
        repair = importlib.import_module("chatbot.nodes.d1_sensor_data.repair_sensor_query")
        with patch.dict(os.environ, {"SENSEI_D1_EVALUATION": "1"}):
            self.assertEqual(repair.repair_attempt_limit({}), 1)


class PhiaRunnerRulesTests(unittest.TestCase):
    def setUp(self):
        self.runner = importlib.import_module("run_phia_all")

    def run_main(self, *extra):
        captured = []
        with tempfile.TemporaryDirectory(prefix="sensei-d1-routing-test-") as directory:
            output = Path(directory)

            def child(cmd, **kwargs):
                captured.append((cmd, kwargs["env"]))
                trace = Path(cmd[cmd.index("--trace-output") + 1])
                trace.write_text("[]", encoding="utf-8")
                return Mock(wait=Mock(return_value=0))

            argv = ["run_phia_all.py", str(output / "workbook.xlsx"), "--users", "41", "--repeats", "1",
                    "--workers", "1", "--models", "llama-4-maverick", "--out-dir", str(output / "traces"),
                    "--csv", str(output / "result.csv"), *extra]
            with patch.object(sys, "argv", argv), patch.object(self.runner, "load_questions", return_value=(
                    {"41": ["A synthetic sensor-data question"]}, {"41": []})), \
                    patch.object(self.runner.subprocess, "Popen", side_effect=child), \
                    patch.object(self.runner.subprocess, "run"):
                self.assertEqual(self.runner.main(), 0)
        self.assertEqual(len(captured), 1)
        return captured[0]

    def test_chunk_process_installs_rules_then_runs_local_test_arguments(self):
        cmd, environment = self.run_main()
        self.assertEqual(Path(cmd[1]).name, "run_phia_all.py")
        self.assertEqual(cmd[2:4], [self.runner.CHILD_FLAG, "d1"])
        self.assertEqual(cmd[cmd.index("--models") + 1], "llama-4-maverick")
        self.assertEqual(cmd[cmd.index("--clock") + 1], "referenced-data")
        self.assertIn("--answer-only", cmd)
        self.assertNotIn("--max-output-tokens", cmd)
        self.assertNotIn("SENSEI_D1_EVALUATION", environment)
        self.assertNotIn("SENSEI_EVAL_ROUTING", environment)

    def test_auto_routing_is_passed_to_the_chunk_process(self):
        cmd, _ = self.run_main("--routing", "auto")
        self.assertEqual(cmd[2:4], [self.runner.CHILD_FLAG, "auto"])

    def test_install_patches_only_this_process_graph_hooks(self):
        graph = importlib.import_module("chatbot.graph.workflow")
        d1 = importlib.import_module("chatbot.nodes.d1_sensor_data.workflow")
        repair = importlib.import_module("chatbot.nodes.d1_sensor_data.repair_sensor_query")
        hooks = [(graph, "plan_domain_execution"), (d1, "plan_sensor_query"), (d1, "guard_decision"),
                 (d1, "repair_attempt_limit"), (repair, "repair_attempt_limit")]
        for routing, routed in (("d1", True), ("auto", False)):
            originals = {(module, name): getattr(module, name) for module, name in hooks}
            patches = [patch.object(module, name, value) for (module, name), value in originals.items()]
            for item in patches:
                item.start()
            try:
                self.runner.install_phia_d1_rules(routing)
                self.assertEqual(graph.plan_domain_execution is self.runner.forced_d1_plan, routed)
                self.assertIsNot(d1.plan_sensor_query, originals[(d1, "plan_sensor_query")])
                self.assertIs(repair.repair_attempt_limit, self.runner.evaluation_repair_attempt_limit)
            finally:
                for item in patches:
                    item.stop()

    def test_forced_route_selects_d1_without_a_domain_llm_call(self):
        planner = importlib.import_module("chatbot.nodes.planning.plan_domain_execution")
        with patch.object(planner.support, "call_chat_completion",
                          side_effect=AssertionError("no domain LLM")) as model:
            result = self.runner.forced_d1_plan({"messages": [HumanMessage(content="q")]})
        model.assert_not_called()
        self.assertEqual(result["route"], "personal_data_supported")
        self.assertEqual(result["domain_execution_plan"]["selected_domains"], ["D1"])
        self.assertEqual(result["orchestration_trace"]["planner_llm_calls"], 0)
        self.assertEqual(result["orchestration_trace"]["routing_mode"], "phia_d1_runner")

    def test_planner_receives_the_evaluation_clock_as_context(self):
        seen = []
        node = self.runner.with_evaluation_clock_context(lambda state: seen.append(state) or {})
        node({"memory_context": "Stored fact."})
        self.assertTrue(seen[0]["memory_context"].startswith("Stored fact.\n"))
        self.assertIn(self.runner.EVALUATION_CLOCK_CONTEXT, seen[0]["memory_context"])

    def test_clarification_on_a_valid_plan_is_not_asked(self):
        plan = {"metrics": ["steps"], "needs_followup": True, "execution_ready": False,
                "followup_question": "Which day did you mean?", "validation_errors": [],
                "time_range": {"mode": "relative", "unit": "day", "value": 1, "anchor": "today"}}
        node = self.runner.proceed_without_clarification(
            lambda state: {"plan": plan, "prev_plan": {}, "raw_plan": {}})
        result = node({"timezone_iana": "Asia/Seoul"})
        self.assertIsNot(result["plan"].get("execution_ready"), False)
        self.assertFalse(result["plan"]["needs_followup"])
        self.assertIn("Unasked clarification: Which day did you mean?", result["plan"]["assumptions"])
        self.assertEqual(result["sensor_plan_trace"]["evaluation_fallback"]["mode"], "clarification_skipped")

    def test_unusable_plan_is_reported_with_its_failure(self):
        plan = {"metrics": [], "execution_ready": False,
                "validation_errors": [{"code": "llm_plan_unavailable", "exception": "RuntimeError"}]}
        node = self.runner.proceed_without_clarification(
            lambda state: {"plan": plan, "prev_plan": {}, "raw_plan": {}})
        result = node({"timezone_iana": "Asia/Seoul"})
        self.assertEqual(result["error"], "D1 query plan unavailable (llm_plan_unavailable RuntimeError)")
        self.assertTrue(self.runner.is_retryable({"execution_status": "error", "error": result["error"]}))

    def test_a_chunk_without_new_output_is_stopped_as_stalled(self):
        import subprocess
        import time as clock

        class Hung:
            killed = False

            def wait(self, timeout=None):
                if self.killed:
                    return 1
                clock.sleep(0.02)
                raise subprocess.TimeoutExpired("chunk", timeout)

            def kill(self):
                self.killed = True

        with tempfile.TemporaryDirectory(prefix="sensei-stall-test-") as directory:
            log = Path(directory) / "chunk.log"
            log.write_text("You> a question\n", encoding="utf-8")
            proc = Hung()
            code, stalled = self.runner.wait_or_stall(proc, log, stall_seconds=0.05)
        self.assertTrue(stalled)
        self.assertTrue(proc.killed)
        self.assertEqual(code, 1)
        finished = Mock(wait=Mock(return_value=0))
        self.assertEqual(self.runner.wait_or_stall(finished, Path("unused.log"), 60), (0, False))

    def test_evaluation_allows_two_repairs_and_retries_an_invalid_rewrite(self):
        self.assertEqual(self.runner.evaluation_repair_attempt_limit({}), 2)
        decide = self.runner.retry_invalid_rewrite(lambda state: "produce_sensor_result", lambda state: True)
        state = {"guard_status": "error", "repair_attempts": 1,
                 "kql_generation_trace": {"repair_history": [{"origin": "adx_validation"}]}}
        self.assertEqual(decide(state), "repair_sensor_query")
        self.assertEqual(decide({**state, "repair_attempts": 2}), "produce_sensor_result")
        self.assertEqual(decide({**state, "kql_generation_trace": {}}), "produce_sensor_result")


if __name__ == "__main__":
    unittest.main()
