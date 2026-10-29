# -*- coding: utf-8 -*-
"""
Blueprint: Backfill orchestrator (past N days).
"""

import datetime

import azure.durable_functions as df

from functions.config import BACKFILL_DAYS, BACKFILL_PARALLEL

bp = df.Blueprint()


@bp.orchestration_trigger(context_name="context")
def backfill_orchestrator(context: df.DurableOrchestrationContext):
    data = context.get_input()
    end_dt = context.current_utc_datetime

    tasks = []
    for i in range(BACKFILL_DAYS):
        target = end_dt - datetime.timedelta(days=i)

        start_dt = datetime.datetime(target.year, target.month, target.day, 0, 0, 0, tzinfo=datetime.timezone.utc)
        end_dt_day = datetime.datetime(target.year, target.month, target.day, 23, 59, 59, tzinfo=datetime.timezone.utc)
        start_ts = int(start_dt.timestamp())
        end_ts = int(end_dt_day.timestamp())

        tasks.append(
            context.call_activity_with_retry(
                "fetch_and_ingest",
                df.RetryOptions(5000, 3),
                {"mode": "backfill", "user_id": data["user_id"], "appli": None, "start_ts": start_ts, "end_ts": end_ts},
            )
        )

        if len(tasks) >= max(1, BACKFILL_PARALLEL):
            yield context.task_all(tasks)
            tasks = []

    if tasks:
        yield context.task_all(tasks)

    return f"Backfill Done ({BACKFILL_DAYS} days)"
