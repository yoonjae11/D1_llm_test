"""Offline tests for one-time CC and Telegram account pairing."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from azure.core.exceptions import (
    ResourceExistsError,
    ResourceModifiedError,
    ResourceNotFoundError,
)
from azure.data.tables import UpdateMode


APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from functions import pairing
from functions.pairing import PairingConsumeResult


TEST_SECRET = "test-only-pairing-secret-with-at-least-32-bytes"
NOW = 2_000_000_000


class FakeEntity(dict):
    def __init__(self, values, etag):
        super().__init__(values)
        self.metadata = {"etag": etag}


class FakeTableClient:
    """Small ETag-aware table double, including same-partition transactions."""

    def __init__(self):
        self.rows = {}
        self.versions = {}
        self.last_transaction = None

    @staticmethod
    def _key(entity):
        return str(entity["PartitionKey"]), str(entity["RowKey"])

    def seed(self, entity):
        key = self._key(entity)
        self.rows[key] = dict(entity)
        self.versions[key] = self.versions.get(key, 0) + 1

    def _etag(self, key):
        return f'etag-{self.versions[key]}'

    def get_entity(self, *, partition_key, row_key):
        key = str(partition_key), str(row_key)
        if key not in self.rows:
            raise ResourceNotFoundError("missing")
        return FakeEntity(dict(self.rows[key]), self._etag(key))

    def create_entity(self, entity=None, **kwargs):
        entity = entity or kwargs.get("entity")
        key = self._key(entity)
        if key in self.rows:
            raise ResourceExistsError("exists")
        self.seed(entity)

    def update_entity(
        self, entity=None, mode=UpdateMode.MERGE, *, etag=None, match_condition=None, **kwargs
    ):
        entity = entity or kwargs.get("entity")
        key = self._key(entity)
        if key not in self.rows:
            raise ResourceNotFoundError("missing")
        if etag is not None and etag != self._etag(key):
            raise ResourceModifiedError("etag mismatch")
        if mode == UpdateMode.REPLACE:
            self.rows[key] = dict(entity)
        else:
            self.rows[key].update(dict(entity))
        self.versions[key] += 1

    def delete_entity(self, *args, **kwargs):
        if args and isinstance(args[0], dict):
            key = self._key(args[0])
        else:
            key = str(kwargs["partition_key"]), str(kwargs["row_key"])
        if key not in self.rows:
            raise ResourceNotFoundError("missing")
        etag = kwargs.get("etag")
        if etag is not None and etag != self._etag(key):
            raise ResourceModifiedError("etag mismatch")
        del self.rows[key]
        del self.versions[key]

    def submit_transaction(self, operations):
        self.last_transaction = operations
        # Validate every conditional operation before mutating any row.
        for operation, entity, kwargs in operations:
            key = self._key(entity)
            if key not in self.rows:
                raise ResourceNotFoundError("missing")
            if kwargs.get("etag") != self._etag(key):
                raise ResourceModifiedError("etag mismatch")
        for operation, entity, kwargs in operations:
            if operation == "update":
                self.update_entity(entity, **kwargs)
            elif operation == "delete":
                self.delete_entity(entity, **kwargs)
            else:
                raise AssertionError(f"unexpected transaction operation: {operation}")
        return []


def seed_active_code(client, code="012345", app_user_id="app-user", expires_at=NOW + 300):
    code_hash = pairing.pairing_code_hash(code)
    pointer_key = pairing.active_pointer_row_key(app_user_id)
    client.seed(
        {
            "PartitionKey": "PairingRequest",
            "RowKey": code_hash,
            "EntityType": "PairingCode",
            "Status": "active",
            "AppUserId": app_user_id,
            "ExpiresAt": expires_at,
            "CreatedAt": pairing._utc_iso(expires_at - 300),
            "ActivePointerRowKey": pointer_key,
        }
    )
    client.seed(
        {
            "PartitionKey": "PairingRequest",
            "RowKey": pointer_key,
            "EntityType": "ActivePointer",
            "AppUserId": app_user_id,
            "CodeHash": code_hash,
            "ExpiresAt": expires_at,
            "UpdatedAt": "2033-05-18T03:28:20+00:00",
        }
    )
    return code_hash, pointer_key


class PairingStorageTests(unittest.TestCase):
    def setUp(self):
        self.secret_patch = patch.object(pairing, "PAIRING_HMAC_SECRET", TEST_SECRET)
        self.secret_patch.start()

    def tearDown(self):
        self.secret_patch.stop()

    def test_only_exact_six_ascii_digits_are_codes(self):
        self.assertTrue(pairing.is_pairing_code("012345"))
        for value in ("12345", "1234567", " 123456", "123456 ", "Verify my app: 123456", "１２３４５６"):
            self.assertFalse(pairing.is_pairing_code(value))

    def test_hash_contract_is_hmac_sha256_and_keeps_pin_out_of_row_key(self):
        digest = pairing.pairing_code_hash("012345")
        self.assertEqual(len(digest), 64)
        self.assertNotIn("012345", digest)
        self.assertEqual(digest, pairing.pairing_code_hash("012345"))
        self.assertNotEqual(digest, pairing.pairing_code_hash("012346"))

    def test_hash_contract_matches_smartphone_issuer_vector(self):
        shared_secret = "pairing-hmac-secret-for-tests-32-bytes-minimum"
        self.assertEqual(
            pairing.pairing_code_hash("000042", secret=shared_secret),
            "eec0fb0031a5380e05fd20f85baa15dc149d303ded46f80c65069e2e0bd18c28",
        )
        self.assertEqual(
            pairing.active_pointer_row_key("alice", secret=shared_secret),
            "active_4d557f425f0b0fcb7daa28333bfb8d68782551f28c2e54623cc68c528fa8b8d2",
        )

    def test_claim_and_finalize_are_conditional_and_remove_active_pointer(self):
        client = FakeTableClient()
        code_hash, pointer_key = seed_active_code(client)

        result = pairing.consume_pairing_code(
            "012345", "ConversationalCare", "cc_abcdefghijklmnop", now=NOW, table_client=client
        )

        self.assertTrue(result.paired)
        self.assertEqual(result.app_user_id, "app-user")
        claimed = client.rows[("PairingRequest", code_hash)]
        self.assertEqual(claimed["Status"], "claimed")
        self.assertNotIn(("PairingRequest", pointer_key), client.rows)
        stored_json = json.dumps(list(client.rows.values()))
        self.assertNotIn("012345", stored_json)
        self.assertNotIn("cc_abcdefghijklmnop", stored_json)
        self.assertEqual([item[0] for item in client.last_transaction], ["update", "delete"])
        self.assertTrue(all(item[2].get("etag") for item in client.last_transaction))

        self.assertTrue(
            pairing.finalize_pairing_code(
                "012345", "ConversationalCare", "cc_abcdefghijklmnop", now=NOW + 1, table_client=client
            )
        )
        self.assertEqual(client.rows[("PairingRequest", code_hash)]["Status"], "consumed")

    def test_same_identity_can_resume_claim_but_another_identity_cannot(self):
        client = FakeTableClient()
        seed_active_code(client)
        first = pairing.consume_pairing_code(
            "012345", "Telegram", 111, now=NOW, table_client=client
        )
        retry = pairing.consume_pairing_code(
            "012345", "Telegram", 111, now=NOW + 1, table_client=client
        )
        other = pairing.consume_pairing_code(
            "012345", "Telegram", 222, now=NOW + 1, table_client=client
        )
        self.assertTrue(first.paired)
        self.assertTrue(retry.paired)
        self.assertFalse(other.paired)
        self.assertEqual(other.status, "used")

    def test_same_app_user_can_pair_separate_channels_with_separate_codes(self):
        client = FakeTableClient()
        seed_active_code(client, code="123456", app_user_id="app-user")
        cc_result = pairing.consume_pairing_code(
            "123456",
            "ConversationalCare",
            "cc_abcdefghijklmnop",
            now=NOW,
            table_client=client,
        )
        self.assertTrue(cc_result.paired)
        self.assertTrue(
            pairing.finalize_pairing_code(
                "123456",
                "ConversationalCare",
                "cc_abcdefghijklmnop",
                now=NOW,
                table_client=client,
            )
        )

        seed_active_code(client, code="654321", app_user_id="app-user")
        telegram_result = pairing.consume_pairing_code(
            "654321",
            "Telegram",
            7001,
            now=NOW,
            table_client=client,
        )
        self.assertTrue(telegram_result.paired)
        self.assertEqual(telegram_result.app_user_id, "app-user")

    def test_replaced_code_is_rejected_by_active_pointer(self):
        client = FakeTableClient()
        _old_hash, pointer_key = seed_active_code(client, code="012345")
        new_hash = pairing.pairing_code_hash("654321")
        client.rows[("PairingRequest", pointer_key)]["CodeHash"] = new_hash
        client.versions[("PairingRequest", pointer_key)] += 1
        result = pairing.consume_pairing_code(
            "012345", "Telegram", 111, now=NOW, table_client=client
        )
        self.assertFalse(result.paired)
        self.assertEqual(result.status, "invalid")

    def test_expired_code_is_rejected_and_invalid_guesses_lock_identity(self):
        client = FakeTableClient()
        seed_active_code(client, expires_at=NOW)
        expired = pairing.consume_pairing_code(
            "012345", "Telegram", 111, now=NOW, table_client=client
        )
        self.assertEqual(expired.status, "expired")

        with patch.object(pairing, "PAIRING_MAX_ATTEMPTS", 2):
            first = pairing.consume_pairing_code(
                "111111", "Telegram", 222, now=NOW, table_client=client
            )
            second = pairing.consume_pairing_code(
                "222222", "Telegram", 222, now=NOW + 1, table_client=client
            )
            blocked = pairing.consume_pairing_code(
                "333333", "Telegram", 222, now=NOW + 2, table_client=client
            )
        self.assertEqual(first.status, "invalid")
        self.assertEqual(second.status, "rate_limited")
        self.assertEqual(blocked.status, "rate_limited")
        self.assertGreater(blocked.retry_after_seconds, 0)

    def test_missing_secret_fails_closed(self):
        with patch.object(pairing, "PAIRING_HMAC_SECRET", ""):
            with self.assertRaises(pairing.PairingConfigurationError):
                pairing.consume_pairing_code(
                    "123456", "Telegram", 111, now=NOW, table_client=FakeTableClient()
                )

    def test_missing_shared_storage_connection_fails_closed(self):
        with patch.object(pairing, "PAIRING_TABLE_CONN_STR", ""):
            with self.assertRaises(pairing.PairingConfigurationError):
                pairing.consume_pairing_code(
                    "123456", "Telegram", 111, now=NOW
                )

    def test_malformed_overlong_lifetime_is_rejected(self):
        client = FakeTableClient()
        seed_active_code(client, expires_at=NOW + 301)
        code_hash = pairing.pairing_code_hash("012345")
        client.rows[("PairingRequest", code_hash)]["CreatedAt"] = pairing._utc_iso(NOW)
        result = pairing.consume_pairing_code(
            "012345", "Telegram", 111, now=NOW, table_client=client
        )
        self.assertFalse(result.paired)


class MappingWriteTests(unittest.TestCase):
    def test_pairing_create_only_does_not_overwrite_existing_mapping(self):
        from functions import shared

        client = FakeTableClient()
        client.seed(
            {
                "PartitionKey": "ConversationalCare",
                "RowKey": "cc_abcdefghijklmnop",
                "AppUserId": "already-linked-user",
            }
        )
        with patch.object(shared, "get_mapping_client", return_value=client):
            saved = shared.save_channel_user_mapping(
                "ConversationalCare",
                "cc_abcdefghijklmnop",
                "different-user",
                overwrite=False,
            )

        self.assertFalse(saved)
        self.assertEqual(
            client.rows[("ConversationalCare", "cc_abcdefghijklmnop")][
                "AppUserId"
            ],
            "already-linked-user",
        )

    def test_pairing_create_only_is_idempotent_for_same_mapping(self):
        from functions import shared

        client = FakeTableClient()
        client.seed(
            {
                "PartitionKey": "Telegram",
                "RowKey": "7001",
                "AppUserId": "app-user",
            }
        )
        with patch.object(shared, "get_mapping_client", return_value=client):
            saved = shared.save_channel_user_mapping(
                "Telegram", 7001, "app-user", overwrite=False
            )

        self.assertTrue(saved)


class ConversationalCarePairingTests(unittest.TestCase):
    def test_unmapped_code_pairs_before_chatbot_and_finalizes(self):
        from functions import sensei_api_bp

        with (
            patch.object(sensei_api_bp, "get_channel_app_user_id", return_value=None),
            patch.object(
                sensei_api_bp,
                "consume_pairing_code",
                return_value=PairingConsumeResult("paired", "app-user"),
            ) as consume,
            patch.object(sensei_api_bp, "save_channel_user_mapping", return_value=True) as save,
            patch.object(sensei_api_bp, "finalize_pairing_code") as finalize,
            patch.object(sensei_api_bp, "_refresh_availability"),
            patch.object(sensei_api_bp, "handle_user_message", new_callable=AsyncMock) as chat,
        ):
            response = asyncio.run(
                sensei_api_bp._message(
                    {"message": "123456", "conversation_id": "thread-1"},
                    "cc_abcdefghijklmnop",
                )
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.get_body())["status"], "ok")
        consume.assert_called_once_with("123456", "ConversationalCare", "cc_abcdefghijklmnop")
        save.assert_called_once_with(
            "ConversationalCare",
            "cc_abcdefghijklmnop",
            "app-user",
            overwrite=False,
        )
        finalize.assert_called_once()
        chat.assert_not_awaited()

    def test_existing_mapping_never_changes_on_bare_six_digits(self):
        from functions import sensei_api_bp

        chat_result = SimpleNamespace(text="ordinary answer", route="chat")
        with (
            patch.object(sensei_api_bp, "get_channel_app_user_id", return_value="existing-user"),
            patch.object(sensei_api_bp, "get_interview_status", return_value="completed"),
            patch.object(sensei_api_bp, "consume_pairing_code") as consume,
            patch.object(sensei_api_bp, "save_channel_user_mapping") as save,
            patch.object(sensei_api_bp, "handle_user_message", new=AsyncMock(return_value=chat_result)),
        ):
            response = asyncio.run(
                sensei_api_bp._message(
                    {"message": "123456", "conversation_id": "thread-1"},
                    "cc_abcdefghijklmnop",
                )
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.get_body())["response"], "ordinary answer")
        consume.assert_not_called()
        save.assert_not_called()

    def test_unmapped_non_code_returns_app_pairing_guidance(self):
        from functions import sensei_api_bp

        with patch.object(sensei_api_bp, "get_channel_app_user_id", return_value=None):
            response = asyncio.run(
                sensei_api_bp._message(
                    {"message": "hello", "conversation_id": "thread-1"},
                    "cc_abcdefghijklmnop",
                )
            )
        body = json.loads(response.get_body())
        self.assertEqual(response.status_code, 401)
        self.assertIn("Custom Logger App", body["response"])
        self.assertIn("six-digit PIN", body["response"])

    def test_mapping_failure_keeps_claim_retryable_and_does_not_finalize(self):
        from functions import sensei_api_bp

        with (
            patch.object(sensei_api_bp, "get_channel_app_user_id", return_value=None),
            patch.object(
                sensei_api_bp,
                "consume_pairing_code",
                return_value=PairingConsumeResult("paired", "app-user"),
            ),
            patch.object(sensei_api_bp, "save_channel_user_mapping", return_value=False),
            patch.object(sensei_api_bp, "finalize_pairing_code") as finalize,
            patch.object(sensei_api_bp, "handle_user_message", new_callable=AsyncMock) as chat,
        ):
            response = asyncio.run(
                sensei_api_bp._message(
                    {"message": "123456", "conversation_id": "thread-1"},
                    "cc_abcdefghijklmnop",
                )
            )
        self.assertEqual(response.status_code, 503)
        self.assertIn("same code", json.loads(response.get_body())["response"])
        finalize.assert_not_called()
        chat.assert_not_awaited()


class TelegramPairingTests(unittest.TestCase):
    @staticmethod
    def _request(text, update_id=9001, chat_id=7001):
        return SimpleNamespace(
            headers={},
            get_json=lambda: {
                "update_id": update_id,
                "message": {
                    "message_id": update_id,
                    "date": NOW,
                    "chat": {"id": chat_id},
                    "text": text,
                },
            },
        )

    def test_unmapped_telegram_code_pairs_without_entering_chatbot(self):
        from functions import telegram_bp

        sent = []
        with (
            patch.object(telegram_bp, "TELEGRAM_WEBHOOK_SECRET_TOKEN", ""),
            patch.object(telegram_bp, "TELEGRAM_WEBHOOK_REQUIRE_SECRET", False),
            patch.object(telegram_bp, "_is_duplicate_update", return_value=False),
            patch.object(telegram_bp, "_safe_app_user_id", return_value=""),
            patch.object(telegram_bp, "get_register_withings_state", return_value=None),
            patch.object(telegram_bp, "get_pending_input_state", return_value=None),
            patch.object(telegram_bp, "_stage_inbound_event", return_value="event"),
            patch.object(telegram_bp, "_send_logged", side_effect=lambda _chat, text, **_kw: sent.append(text)),
            patch.object(
                telegram_bp,
                "consume_pairing_code",
                return_value=PairingConsumeResult("paired", "app-user"),
            ),
            patch.object(telegram_bp, "save_user_mapping", return_value=True) as save,
            patch.object(telegram_bp, "finalize_pairing_code") as finalize,
            patch.object(telegram_bp, "_refresh_availability_async"),
            patch.object(telegram_bp, "handle_user_message", new_callable=AsyncMock) as chat,
        ):
            response = asyncio.run(telegram_bp.telegram_webhook(self._request("123456")))
        self.assertEqual(response.status_code, 200)
        self.assertIn("now connected", sent[-1])
        save.assert_called_once_with(7001, "app-user", overwrite=False)
        finalize.assert_called_once_with("123456", "Telegram", 7001)
        chat.assert_not_awaited()

    def test_pairing_route_redacts_bare_pin_from_conversation_log(self):
        from functions import telegram_conversation_log

        with patch.object(telegram_conversation_log, "TELEGRAM_CONVERSATION_STORE_TEXT", True):
            event = telegram_conversation_log.build_conversation_event(
                event_type="user_message",
                role="user",
                direction="inbound",
                event_time_utc=None,
                app_user_id="",
                telegram_chat_id=7001,
                message_text="123456",
                route="pairing",
                command="123456",
                execution_status="received",
            )
        self.assertEqual(event["MessageText"], "[REDACTED_PAIRING_CODE]")
        self.assertEqual(event["Command"], "")
        self.assertNotIn("123456", json.dumps(event))

    def test_pending_login_passcode_takes_precedence_over_pairing(self):
        from functions import telegram_bp

        sent = []
        pending = {"kind": "login", "data": {"stage": "passcode"}, "updated_at": ""}
        with (
            patch.object(telegram_bp, "TELEGRAM_WEBHOOK_SECRET_TOKEN", ""),
            patch.object(telegram_bp, "TELEGRAM_WEBHOOK_REQUIRE_SECRET", False),
            patch.object(telegram_bp, "_is_duplicate_update", return_value=False),
            patch.object(telegram_bp, "_safe_app_user_id", return_value=""),
            patch.object(telegram_bp, "get_register_withings_state", return_value=None),
            patch.object(telegram_bp, "get_pending_input_state", return_value=pending),
            patch.object(telegram_bp, "_stage_inbound_event", return_value="event"),
            patch.object(telegram_bp, "_send_logged", side_effect=lambda _chat, text, **_kw: sent.append(text)),
            patch.object(telegram_bp, "_handle_pending_input", return_value=(True, "login handled", None)),
            patch.object(telegram_bp, "clear_pending_input_state"),
            patch.object(telegram_bp, "consume_pairing_code") as consume,
        ):
            response = asyncio.run(
                telegram_bp.telegram_webhook(self._request("123456", update_id=9002))
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(sent[-1], "login handled")
        consume.assert_not_called()

    def test_existing_telegram_mapping_never_changes_on_bare_six_digits(self):
        from functions import telegram_bp

        result = SimpleNamespace(
            text="ordinary answer",
            route="chat",
            intent="chat",
            payload={},
            execution_status="ok",
        )
        sent = []
        with (
            patch.object(telegram_bp, "TELEGRAM_WEBHOOK_SECRET_TOKEN", ""),
            patch.object(telegram_bp, "TELEGRAM_WEBHOOK_REQUIRE_SECRET", False),
            patch.object(telegram_bp, "_is_duplicate_update", return_value=False),
            patch.object(telegram_bp, "_safe_app_user_id", return_value="existing-user"),
            patch.object(telegram_bp, "get_register_withings_state", return_value=None),
            patch.object(telegram_bp, "get_pending_input_state", return_value=None),
            patch.object(telegram_bp, "_stage_inbound_event", return_value="event"),
            patch.object(telegram_bp, "_send_logged", side_effect=lambda _chat, text, **_kw: sent.append(text)),
            patch.object(telegram_bp, "get_interview_status", return_value="completed"),
            patch.object(telegram_bp, "CHATBOT_AVAILABLE", True),
            patch.object(telegram_bp, "send_telegram_typing"),
            patch.object(telegram_bp, "handle_user_message", new=AsyncMock(return_value=result)) as chat,
            patch.object(telegram_bp, "consume_pairing_code") as consume,
            patch.object(telegram_bp, "save_user_mapping") as save,
        ):
            response = asyncio.run(
                telegram_bp.telegram_webhook(self._request("123456", update_id=9003))
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(sent[-1], "ordinary answer")
        chat.assert_awaited_once()
        consume.assert_not_called()
        save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
