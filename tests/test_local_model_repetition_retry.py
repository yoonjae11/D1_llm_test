"""A looped chat completion gets one retry with a repetition penalty."""

import json
import os
import unittest
from unittest.mock import patch

import httpx

from chatbot.local_models import ModelProfile, complete, looks_degenerate

_QUERY = """SensorWorkoutSessions('u7', datetime(2024-03-01 00:00:00), datetime(2024-03-11 00:00:00))
| where StartLocal >= datetime(2024-03-01 00:00:00) and StartLocal < datetime(2024-03-11 00:00:00)
| summarize arg_max(IngestionTime, *) by WorkoutId
| where ActivityName =~ "Rowing" and DurationMin >= 25
| summarize Total = sum(Distance), EvidenceRows = countif(isnotnull(Distance))
| project Metric = "workout_distance", Aggregation = "sum",
          Value = iff(EvidenceRows > 0, Total, real(null)), Unit = "METER",
          EvidenceRows, ResultStatus = iff(EvidenceRows > 0, "ok", "no_matching_sessions")"""
_LOOP = "SensorHealthSamples('u7', datetime(2024-17, datetime(2024-31.\n" * 60


def _chat(text: str, finish: str = "stop") -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"finish_reason": finish,
                                                  "message": {"content": text}}]})


class RepetitionRetryTests(unittest.TestCase):
    def setUp(self):
        self.profile = ModelProfile(
            name="chat-model", deployment="chat-model", api="chat",
            endpoint="https://example.services.ai.azure.com/openai/v1/chat/completions",
            api_key_env="AZURE_OPENAI_API_KEY", repetition_retry_frequency_penalty=0.3,
        )

    def test_detector_separates_loops_from_long_queries(self):
        self.assertFalse(looks_degenerate(_QUERY))
        self.assertFalse(looks_degenerate("\n".join([_QUERY] * 2)))
        self.assertTrue(looks_degenerate(_LOOP))
        self.assertFalse(looks_degenerate("short"))

    def test_looped_or_truncated_output_retries_once_with_penalty(self):
        for first in (_chat(_LOOP), _chat("partial", finish="length")):
            bodies = []

            def respond(request, first=first):
                bodies.append(json.loads(request.content))
                return first if len(bodies) == 1 else _chat(_QUERY)

            with patch.dict(os.environ, {"AZURE_OPENAI_API_KEY": "test-key"}):
                result = complete(self.profile, [{"role": "user", "content": "q"}],
                                  transport=httpx.MockTransport(respond))
            self.assertEqual(result["text"], _QUERY)
            self.assertEqual(len(bodies), 2)
            self.assertNotIn("frequency_penalty", bodies[0])
            self.assertEqual(bodies[1]["frequency_penalty"], 0.3)
            self.assertEqual(bodies[0]["messages"], bodies[1]["messages"])

    def test_models_without_penalty_setting_are_not_retried(self):
        profile = ModelProfile(name="m", deployment="m", api="chat", endpoint=self.profile.endpoint,
                               api_key_env="AZURE_OPENAI_API_KEY")
        bodies = []

        def respond(request):
            bodies.append(request)
            return _chat(_LOOP)

        with patch.dict(os.environ, {"AZURE_OPENAI_API_KEY": "test-key"}):
            complete(profile, [{"role": "user", "content": "q"}], transport=httpx.MockTransport(respond))
        self.assertEqual(len(bodies), 1)


if __name__ == "__main__":
    unittest.main()
