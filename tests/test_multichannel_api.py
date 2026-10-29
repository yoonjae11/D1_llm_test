from __future__ import annotations

import json
import logging
import os
import unittest
import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import azure.functions as func

from chatbot.services.sensor_analytics import build_initial_state
from chatbot.proactive import system_steps_alert
from functions import sensei_api_bp
from functions import shared
from functions import threshold_monitor_bp


OPAQUE_ID = "cc_0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"


def response_json(response: func.HttpResponse) -> dict:
    return json.loads(response.get_body().decode("utf-8"))


class UserMappingTests(unittest.TestCase):
    def test_conversational_care_uses_existing_user_mapping_table(self):
        table = Mock()
        with patch.object(shared, "get_mapping_client", return_value=table):
            saved = shared.save_channel_user_mapping(
                "ConversationalCare", OPAQUE_ID, "kist010"
            )

        self.assertTrue(saved)
        entity = table.upsert_entity.call_args.kwargs["entity"]
        self.assertEqual(entity["PartitionKey"], "ConversationalCare")
        self.assertEqual(entity["RowKey"], OPAQUE_ID)
        self.assertEqual(entity["AppUserId"], "kist010")

    def test_telegram_helpers_keep_original_partition_and_row_key(self):
        table = Mock()
        with patch.object(shared, "get_mapping_client", return_value=table):
            self.assertTrue(shared.save_user_mapping(12345, "kist010"))

        entity = table.upsert_entity.call_args.kwargs["entity"]
        self.assertEqual(entity["PartitionKey"], "Telegram")
        self.assertEqual(entity["RowKey"], "12345")

    def test_reverse_telegram_lookup_escapes_odata_quote(self):
        table = Mock()
        table.query_entities.return_value = []
        with patch.object(shared, "get_mapping_client", return_value=table):
            self.assertIsNone(shared.get_chat_id_by_user("patient'o"))

        table.query_entities.assert_called_once_with(
            "PartitionKey eq 'Telegram' and AppUserId eq 'patient''o'"
        )


class ThreadIsolationTests(unittest.TestCase):
    def test_explicit_conversational_care_thread_overrides_transport_default(self):
        state = build_initial_state(
            app_user_id="kist010",
            telegram_chat_id="",
            message_text="hello",
            inbound_payload={},
            thread_id="conversational_care:thread-abc",
        )
        self.assertEqual(state["thread_id"], "conversational_care:thread-abc")

    def test_telegram_thread_remains_backward_compatible(self):
        state = build_initial_state(
            app_user_id="kist010",
            telegram_chat_id="12345",
            message_text="hello",
            inbound_payload={},
        )
        self.assertEqual(state["thread_id"], "telegram:12345")


class SenseiOperationTests(unittest.IsolatedAsyncioTestCase):
    async def test_help_is_available_before_account_mapping(self):
        with patch.object(
            sensei_api_bp, "get_channel_app_user_id"
        ) as mapping_lookup:
            response = await sensei_api_bp.handle_operation(
                {
                    "operation": "message",
                    "external_user_id": OPAQUE_ID,
                    "message": "/help",
                }
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response_json(response)["status"], "ok")
        mapping_lookup.assert_not_called()

    async def test_login_reuses_shared_verifier_and_writes_cc_mapping(self):
        with (
            patch.object(
                sensei_api_bp,
                "verify_registered_user_passcode",
                return_value=(
                    True,
                    "ok",
                    {"allowed": True, "attempts": 0, "retry_after_seconds": 0},
                ),
            ) as verify,
            patch.object(
                sensei_api_bp, "save_channel_user_mapping", return_value=True
            ) as save,
            patch.object(sensei_api_bp, "_refresh_availability"),
        ):
            response = await sensei_api_bp.handle_operation(
                {
                    "operation": "login",
                    "external_user_id": OPAQUE_ID,
                    "app_user_id": "kist010",
                    "passcode": "UNIQUE-LOGIN-SECRET-9417",
                }
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response_json(response)["status"], "ok")
        verify.assert_called_once_with(
            f"ConversationalCare:{OPAQUE_ID}",
            "kist010",
            "UNIQUE-LOGIN-SECRET-9417",
        )
        save.assert_called_once_with("ConversationalCare", OPAQUE_ID, "kist010")

    async def test_register_reuses_users_table_and_creates_same_mapping(self):
        with (
            patch.object(sensei_api_bp, "get_channel_app_user_id", return_value=None),
            patch.object(sensei_api_bp, "get_registered_user", return_value=None),
            patch.object(
                sensei_api_bp, "upsert_registered_user", return_value=(True, "ok")
            ) as register,
            patch.object(
                sensei_api_bp, "save_channel_user_mapping", return_value=True
            ) as save,
            patch.object(sensei_api_bp, "_refresh_availability"),
        ):
            response = await sensei_api_bp.handle_operation(
                {
                    "operation": "register",
                    "external_user_id": OPAQUE_ID,
                    "app_user_id": "kist020",
                    "passcode": "UNIQUE-REGISTER-SECRET-5723",
                }
            )

        self.assertEqual(response.status_code, 200)
        register.assert_called_once_with("kist020", "UNIQUE-REGISTER-SECRET-5723")
        save.assert_called_once_with("ConversationalCare", OPAQUE_ID, "kist020")

    async def test_message_uses_explicit_cc_thread_and_returns_only_public_contract(self):
        chatbot_result = SimpleNamespace(
            text="You took 7,123 steps.", route="data", intent="health_data"
        )
        with (
            patch.object(
                sensei_api_bp, "get_channel_app_user_id", return_value="kist010"
            ),
            patch.object(
                sensei_api_bp, "get_interview_status", return_value="completed"
            ),
            patch.object(
                sensei_api_bp,
                "handle_user_message",
                new=AsyncMock(return_value=chatbot_result),
            ) as handle,
        ):
            response = await sensei_api_bp.handle_operation(
                {
                    "operation": "message",
                    "external_user_id": OPAQUE_ID,
                    "conversation_id": "4ab18fd0-73db-4d4d-bd62-2f41325cbb91",
                    "message": "How many steps did I take yesterday?",
                }
            )

        body = response_json(response)
        self.assertEqual(body, {"status": "ok", "response": "You took 7,123 steps."})
        self.assertEqual(
            handle.await_args.kwargs["thread_id"],
            "conversational_care:4ab18fd0-73db-4d4d-bd62-2f41325cbb91",
        )
        self.assertNotIn("kql", json.dumps(body).lower())
        self.assertNotIn("final_state", body)

    async def test_credential_command_never_enters_langgraph(self):
        secret = "UNIQUE-NO-LANGGRAPH-SECRET-7351"
        graph = AsyncMock()
        with (
            patch.object(
                sensei_api_bp, "get_channel_app_user_id", return_value="kist010"
            ),
            patch.object(sensei_api_bp, "handle_user_message", new=graph),
        ):
            response = await sensei_api_bp.handle_operation(
                {
                    "operation": "message",
                    "external_user_id": OPAQUE_ID,
                    "conversation_id": "thread-a",
                    "message": f"/login kist010 {secret}",
                }
            )

        self.assertEqual(response.status_code, 400)
        graph.assert_not_awaited()
        self.assertNotIn(secret, response.get_body().decode("utf-8"))

    async def test_exception_log_and_response_do_not_expose_passcode(self):
        secret = "UNIQUE-LOG-LEAK-CHECK-8391"
        request = func.HttpRequest(
            method="POST",
            url="https://sensei.example/api/send_message",
            headers={"content-type": "application/json"},
            params={},
            route_params={},
            body=json.dumps(
                {
                    "operation": "login",
                    "external_user_id": OPAQUE_ID,
                    "app_user_id": "kist010",
                    "passcode": secret,
                }
            ).encode("utf-8"),
        )
        with (
            patch.object(
                sensei_api_bp,
                "handle_operation",
                new=AsyncMock(side_effect=RuntimeError(secret)),
            ),
            self.assertLogs(level=logging.ERROR) as captured,
        ):
            response = await sensei_api_bp.send_message(request)

        combined = "\n".join(captured.output) + response.get_body().decode("utf-8")
        self.assertNotIn(secret, combined)
        self.assertEqual(response.status_code, 500)


class ProactiveDeliverySkipTests(unittest.TestCase):
    def test_threshold_timer_skips_expensive_evaluation_without_telegram(self):
        repo = Mock()
        repo.list_users_with_alert_thresholds.return_value = ["kist010"]
        with (
            patch.object(
                threshold_monitor_bp,
                "get_settings",
                return_value=SimpleNamespace(threshold_alert_enabled=True),
            ),
            patch.object(
                threshold_monitor_bp,
                "OperationalTableRepository",
                return_value=repo,
            ),
            patch.object(
                threshold_monitor_bp,
                "_has_telegram_delivery_target",
                return_value=False,
            ),
            patch.object(threshold_monitor_bp, "evaluate_user_thresholds") as evaluate,
        ):
            threshold_monitor_bp.threshold_alert_timer(Mock())

        evaluate.assert_not_called()

    def test_daily_timer_skips_report_generation_without_telegram(self):
        repo = Mock()
        repo.list_active_user_profiles.return_value = [
            SimpleNamespace(
                app_user_id="kist010",
                timezone_iana="Asia/Seoul",
                summary_report_times=[(9, 0)],
            )
        ]
        repo.daily_report_already_sent.return_value = False
        with (
            patch.object(
                threshold_monitor_bp,
                "get_settings",
                return_value=SimpleNamespace(
                    daily_report_enabled=True,
                    default_timezone="Asia/Seoul",
                ),
            ),
            patch.object(
                threshold_monitor_bp,
                "OperationalTableRepository",
                return_value=repo,
            ),
            patch.object(
                threshold_monitor_bp,
                "_local_now",
                return_value=dt.datetime(2026, 9, 4, 9, 0),
            ),
            patch.object(
                threshold_monitor_bp,
                "_has_telegram_delivery_target",
                return_value=False,
            ),
            patch.object(threshold_monitor_bp, "send_daily_report") as send_report,
        ):
            threshold_monitor_bp.daily_report_timer(Mock())

        send_report.assert_not_called()

    def test_system_steps_detector_stops_before_adx_without_telegram(self):
        repo = Mock()
        repo.get_user_profile.return_value = SimpleNamespace(
            timezone_iana="Asia/Seoul",
            system_steps_alert_enabled=True,
        )
        settings = SimpleNamespace(
            default_timezone="Asia/Seoul",
            system_steps_alert_enabled=True,
            system_steps_alert_trigger_hour_local=0,
        )
        with (
            patch.object(system_steps_alert, "get_settings", return_value=settings),
            patch.object(shared, "get_chat_id_by_user", return_value=None),
            patch.object(system_steps_alert, "_adx_query_single") as adx_query,
        ):
            result = system_steps_alert.process_steps_event(
                {"appUserId": "kist010"},
                repository=repo,
                now_utc=dt.datetime(2026, 9, 4, tzinfo=dt.timezone.utc),
                allow_missing_step_date=True,
            )

        self.assertEqual(result["reason"], "no_telegram_delivery_target")
        adx_query.assert_not_called()
        repo.get_trigger_state.assert_not_called()


class AzureFunctionAuthenticationTests(unittest.TestCase):
    def test_send_message_route_requires_function_key(self):
        os.environ.setdefault("ADX_DATABASE", "healthdb")
        os.environ.setdefault("KQL_GENERATE_METHOD", "deterministic")
        import function_app

        function = next(
            item
            for item in function_app.app.get_functions()
            if item.get_function_name() == "SenseiSendMessage"
        )
        trigger = next(
            binding for binding in function.get_bindings() if binding.type == "httpTrigger"
        )
        self.assertEqual(trigger.route, "send_message")
        self.assertEqual(trigger.auth_level, func.AuthLevel.FUNCTION)
        self.assertEqual([method.value for method in trigger.methods], ["POST"])


if __name__ == "__main__":
    unittest.main()
