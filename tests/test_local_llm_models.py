"""Offline contracts for local model selection and provider wire formats."""

import asyncio
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx

from chatbot import llm_client
from chatbot.local_models import active_model, complete, load_models, use_model
import local_llm_test


class LocalModelTests(unittest.TestCase):
    def setUp(self):
        self.profiles = load_models(Path(local_llm_test.__file__).with_name("local_models.json"))

    def test_configured_routes_and_shared_key(self):
        self.assertEqual(len(self.profiles), 11)
        self.assertEqual(self.profiles["gpt-5"].api, "responses")
        self.assertEqual(self.profiles["claude-opus-5-5"].api, "anthropic")
        for profile in self.profiles.values():
            self.assertEqual(profile.api_key_env, "AZURE_OPENAI_API_KEY")
            self.assertIn(profile.name, self.profiles)
            self.assertTrue(profile.endpoint.startswith("https://your-resource.services.ai.azure.com/"))

    def test_each_protocol_sends_selected_deployment(self):
        fixtures = {
            "responses": {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": "OK"}]}]},
            "chat": {"choices": [{"finish_reason": "stop", "message": {"content": "OK"}}]},
            "anthropic": {"stop_reason": "end_turn", "content": [{"type": "text", "text": "OK"}]},
        }
        for profile in self.profiles.values():
            with self.subTest(model=profile.name):
                def respond(request):
                    self.assertEqual(str(request.url), profile.endpoint)
                    body = json.loads(request.content)
                    self.assertEqual(body["model"], profile.deployment)
                    self.assertEqual(request.headers["x-api-key" if profile.api == "anthropic" else "api-key"], "test-key")
                    return httpx.Response(200, json=fixtures[profile.api])
                with patch.dict(os.environ, {"AZURE_OPENAI_API_KEY": "test-key"}):
                    answer = complete(profile, [{"role": "user", "content": "Say OK"}],
                                      transport=httpx.MockTransport(respond))
                self.assertEqual(answer["text"], "OK")

    def test_internal_and_final_llm_share_selected_profile(self):
        profile = self.profiles["claude-opus-5-5"]
        with use_model(profile), patch.object(llm_client, "complete_local_model", return_value={"text": "OK"}) as call:
            self.assertEqual(llm_client.call_chat_completion([{"role": "user", "content": "question"}]), "OK")
            self.assertEqual(llm_client.call_user_facing_response("prompt")["text"], "OK")
            self.assertEqual([item.args[0] for item in call.call_args_list], [profile, profile])
        self.assertIsNone(active_model())

    def test_model_scope_reaches_graph_worker(self):
        async def worker():
            return await asyncio.to_thread(active_model)
        with use_model(self.profiles["gpt-6-1-sol"]):
            self.assertEqual(asyncio.run(worker()).name, "gpt-6-1-sol")

    def test_retry_is_bounded_and_keeps_the_same_request(self):
        requests = []
        def respond(request):
            requests.append(request.content)
            return httpx.Response(503) if len(requests) == 1 else httpx.Response(200, json={
                "status": "completed", "output": [{"type": "message", "content": [
                    {"type": "output_text", "text": "OK"}]}]})
        with patch.dict(os.environ, {"AZURE_OPENAI_API_KEY": "test-key", "SENSEI_LOCAL_LLM_MAX_RETRIES": "1"}), \
                patch("chatbot.local_models.time.sleep"):
            result = complete(self.profiles["gpt-5"], [{"role": "user", "content": "Say OK"}],
                              transport=httpx.MockTransport(respond))
        self.assertEqual(result["transport_attempts"], 2)
        self.assertEqual(requests[0], requests[1])

    def test_authentication_failure_is_not_retried(self):
        requests = []
        def respond(request):
            requests.append(request)
            return httpx.Response(401)
        with patch.dict(os.environ, {"AZURE_OPENAI_API_KEY": "test-key"}), \
                self.assertRaisesRegex(RuntimeError, "HTTP 401"):
            complete(self.profiles["gpt-5"], [], transport=httpx.MockTransport(respond))
        self.assertEqual(len(requests), 1)

    def test_cli_lists_models_without_secret(self):
        with patch("sys.argv", ["local_llm_test.py", "--list-models"]), patch.dict(os.environ, {"AZURE_OPENAI_API_KEY": "test-key"}):
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(local_llm_test.main(), 0)
        self.assertIn("claude-opus-5-5", output.getvalue())
        self.assertNotIn("test-key", output.getvalue())

    def test_comparison_runs_same_questions_on_each_model_with_fresh_threads(self):
        calls, memories = [], []
        async def respond(**kwargs):
            calls.append((active_model().name, kwargs["message_text"], kwargs["thread_id"]))
            return SimpleNamespace(text="result", execution_status="success", payload={})
        from contextlib import nullcontext
        def memory(*args):
            marker = object()
            memories.append(marker)
            return nullcontext(marker)
        argv = ["local_llm_test.py", "--models", "gpt-5", "claude-opus-5-5",
                "--user-id", "synthetic-user", "--clock", "current", "--question", "A question",
                "--question", "Another question"]
        with patch("sys.argv", argv), patch.object(local_llm_test, "load_local_environment"), \
                patch.object(local_llm_test, "isolated_memory", side_effect=memory), \
                patch("chatbot.local_models.require_key", return_value="test-key"), \
                patch("chatbot.entrypoint.handle_user_message", side_effect=respond), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(local_llm_test.main(), 0)
        self.assertEqual([(model, question) for model, question, _ in calls], [
            ("gpt-5", "A question"), ("gpt-5", "Another question"),
            ("claude-opus-5-5", "A question"), ("claude-opus-5-5", "Another question")])
        self.assertEqual(len({thread for _, _, thread in calls}), 4)
        self.assertEqual(len(memories), 3)  # validation plus a new repository per model
        self.assertIsNone(active_model())

    def test_comparison_continues_after_failure_without_model_substitution(self):
        calls = []
        async def respond(**kwargs):
            name = active_model().name
            calls.append(name)
            if name == "gpt-5":
                raise RuntimeError("synthetic failure")
            return SimpleNamespace(text="result", execution_status="success", payload={})
        argv = ["local_llm_test.py", "--models", "gpt-5", "claude-opus-5-5", "--user-id", "synthetic-user",
                "--clock", "current", "--question", "A question"]
        with patch("sys.argv", argv), patch.object(local_llm_test, "load_local_environment"), \
                patch("chatbot.local_models.require_key", return_value="test-key"), \
                patch("chatbot.entrypoint.handle_user_message", side_effect=respond), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(local_llm_test.main(), 1)
        self.assertEqual(calls, ["gpt-5", "claude-opus-5-5"])
        self.assertIsNone(active_model())

    def test_comparison_requires_noninteractive_question(self):
        with patch("sys.argv", ["local_llm_test.py", "--all-models"]), \
                patch.object(local_llm_test, "load_local_environment"), \
                self.assertRaises(SystemExit) as exc:
            local_llm_test.main()
        self.assertEqual(exc.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
