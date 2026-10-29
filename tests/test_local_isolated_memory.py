"""Local memory isolation prevents Storage I/O without replacing D1 nodes."""

import asyncio
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from langchain_core.messages import AIMessage, HumanMessage

from local_llm_test import isolated_memory
from chatbot.nodes.common.prepare_context import prepare_context
from chatbot.nodes.common.persist_trace_and_context import persist_trace_and_context
from chatbot.services.memory import create_operational_storage, get_registered_user_profile


class LocalIsolatedMemoryTests(unittest.TestCase):
    def test_scope_is_restored_and_registered_profile_stays_in_memory(self):
        with patch("chatbot.adapters.defaults.create_operational_storage_adapter",
                   side_effect=AssertionError("Cloud Storage must not be constructed")):
            with isolated_memory("synthetic-user", "Europe/London") as repository:
                self.assertIs(create_operational_storage("any-connection"), repository)
                profile = get_registered_user_profile("synthetic-user")
                self.assertEqual(profile["TimezoneIana"], "Europe/London")
                self.assertEqual(profile["Locale"], "en")
            with self.assertRaisesRegex(AssertionError, "Cloud Storage"):
                create_operational_storage()

    def test_context_propagates_into_the_entrypoint_worker(self):
        async def worker():
            return await asyncio.to_thread(create_operational_storage)
        with isolated_memory("synthetic-user") as repository:
            self.assertIs(asyncio.run(worker()), repository)

    def test_lazy_inventory_refresh_and_turn_persistence_use_same_repository(self):
        now = datetime.now(timezone.utc).isoformat()
        observed_inventory = [{"Type": "HEART_RATE", "SourceTable": "SensorHealthSamples",
                               "FlatRowCount": 2, "LatestIngestionTimeUtc": now,
                               "EarliestDateFromLocal": "2025-02-01T00:00:00",
                               "LatestDateToLocal": "2025-02-02T00:00:00"}]
        with patch("chatbot.adapters.defaults.create_operational_storage_adapter",
                   side_effect=AssertionError("Cloud Storage must not be constructed")), \
             patch("chatbot.availability_refresh.adx_manager.execute_query",
                   return_value=observed_inventory) as inventory_query, \
             isolated_memory("synthetic-user") as repository:
            state = {"user_id": "synthetic-user", "thread_id": "local-test"}
            hydrated = prepare_context(state)
            self.assertFalse(hydrated["onboarding_required"])
            self.assertTrue(hydrated["timezone_resolved"])
            self.assertEqual(hydrated["timezone_iana"], "Asia/Seoul")
            self.assertIn("HEART_RATE", hydrated["availability_context"])
            inventory_query.assert_called_once()
            self.assertTrue(repository.get_metric_availability_map("synthetic-user"))
            persist_trace_and_context({
                **state, **hydrated,
                "messages": [HumanMessage(content="A question"), AIMessage(content="An answer")],
            })
            saved = repository.get_conversation_state("synthetic-user", "local-test")
            self.assertEqual(len(saved.recent_turns), 2)
            self.assertEqual(saved.recent_turns[-1]["message_text"], "An answer")

    def test_new_isolated_session_starts_without_previous_conversation(self):
        with isolated_memory("synthetic-user") as repository:
            state = repository.get_conversation_state("synthetic-user", "thread-a")
            state.recent_turns = [{"message_text": "old"}]
            repository.save_conversation_state(state)
        with isolated_memory("synthetic-user") as repository:
            self.assertEqual(repository.get_conversation_state("synthetic-user", "thread-a").recent_turns, [])


if __name__ == "__main__":
    unittest.main()
