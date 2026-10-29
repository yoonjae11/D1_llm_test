# -*- coding: utf-8 -*-
"""Timer blueprint for flushing staged Telegram conversation logs to ADX."""

import logging

import azure.functions as func
import azure.durable_functions as df

from functions.telegram_conversation_log import flush_pending_conversation_events_to_adx

telegram_conversation_log_bp = df.Blueprint()


@telegram_conversation_log_bp.function_name(name="TelegramConversationFlushTimer")
@telegram_conversation_log_bp.timer_trigger(
    schedule="0 */5 * * * *",
    arg_name="timer",
    run_on_startup=False,
    use_monitor=True,
)
def telegram_conversation_flush_timer(timer: func.TimerRequest) -> None:
    try:
        result = flush_pending_conversation_events_to_adx()
        logging.info("telegram_conversation_flush_timer result=%s", result)
    except Exception:
        logging.exception("telegram_conversation_flush_timer failed")
