# -*- coding: utf-8 -*-
"""
Blueprint: fetch_and_ingest activity trigger (data fetching + dedup + ADX ingestion + cursor update).
"""

import datetime
import time
from typing import Dict, List, Any

import azure.durable_functions as df
from azure_storage_resources import create_table_client

from functions.config import (
    TABLE_CONN_STR,
    FETCH_INTRADAY_ON_WEBHOOK, FETCH_WORKOUTS_ON_WEBHOOK,
    SYNC_ACTIVITY_LOOKBACK_DAYS, SYNC_ACTIVITY_MAX_LOOKBACK_DAYS, SYNC_ACTIVITY_OVERLAP_DAYS,
    SYNC_SLEEP_LOOKBACK_DAYS, SYNC_SLEEP_MAX_LOOKBACK_DAYS, SYNC_SLEEP_OVERLAP_DAYS,
    SYNC_INTRADAY_LOOKBACK_HOURS, SYNC_INTRADAY_MAX_LOOKBACK_HOURS, SYNC_INTRADAY_OVERLAP_MINUTES,
)
from functions.shared import (
    _safe_int,
    ensure_valid_token, try_acquire_webhook_lease,
    _dedup_should_ingest, ingest_raw_to_adx,
    _compute_sync_window_ts, _update_sync_last_ts_max,
)
from functions.fetch_helpers import (
    _fetch_activity_by_range, _fetch_sleep_summary, _fetch_sleep_detail,
    _fetch_intraday_by_range, _fetch_workouts_for_date,
)

bp = df.Blueprint()


@bp.activity_trigger(input_name="args")
def fetch_and_ingest(args):
    user_id = args["user_id"]
    appli = args.get("appli")
    mode = args.get("mode", "webhook")

    result = {
        "status": "ok",
        "mode": mode,
        "withings_user_id": str(user_id),
        "appli": None,
        "app_user_id": None,
        "raw_records": 0,
        "ingested": 0,
        "skipped_dup": 0,
        "windows": {},
        "counts_by_type": {},
        "max_ts_by_type": {},
    }

    try:
        appli_int = int(appli) if appli is not None else None
    except Exception:
        appli_int = None
    result["appli"] = appli_int

    tc = create_table_client(TABLE_CONN_STR, "WithingsTokens")
    try:
        entity = tc.get_entity(partition_key="withings", row_key=str(user_id))
        token = ensure_valid_token(entity, tc)
        app_user_id = entity.get("AppUserId")
        result["app_user_id"] = app_user_id
    except Exception:
        result["status"] = "user_not_found"
        return result

    if not token:
        result["status"] = "token_missing"
        return result

    if mode == "webhook":
        if not try_acquire_webhook_lease(app_user_id):
            result["status"] = "lease_busy"
            return result

    headers = {"Authorization": f"Bearer {token}"}
    raw_records: List[Dict[str, Any]] = []

    now_ts = int(time.time())
    now_plus = now_ts + 300

    date_hint = args.get("date")
    start_hint = _safe_int(args.get("start_ts"))
    end_hint = _safe_int(args.get("end_ts"))
    if end_hint and end_hint > now_plus:
        now_plus = end_hint + 60

    if mode == "webhook":
        act_start, act_end = _compute_sync_window_ts(
            app_user_id, "Activity", now_ts,
            default_lookback_sec=SYNC_ACTIVITY_LOOKBACK_DAYS * 86400,
            max_lookback_sec=SYNC_ACTIVITY_MAX_LOOKBACK_DAYS * 86400,
            overlap_sec=SYNC_ACTIVITY_OVERLAP_DAYS * 86400,
        )
        sleep_start, sleep_end = _compute_sync_window_ts(
            app_user_id, "Sleep", now_plus,
            default_lookback_sec=SYNC_SLEEP_LOOKBACK_DAYS * 86400,
            max_lookback_sec=SYNC_SLEEP_MAX_LOOKBACK_DAYS * 86400,
            overlap_sec=SYNC_SLEEP_OVERLAP_DAYS * 86400,
        )
        intra_start, intra_end = _compute_sync_window_ts(
            app_user_id, "Intraday", now_plus,
            default_lookback_sec=SYNC_INTRADAY_LOOKBACK_HOURS * 3600,
            max_lookback_sec=SYNC_INTRADAY_MAX_LOOKBACK_HOURS * 3600,
            overlap_sec=SYNC_INTRADAY_OVERLAP_MINUTES * 60,
        )

        if date_hint:
            try:
                d = datetime.date.fromisoformat(str(date_hint))
                d_start = int(datetime.datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=datetime.timezone.utc).timestamp())
                d_end = int(datetime.datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=datetime.timezone.utc).timestamp())
                act_start = min(act_start, d_start)
                act_end = max(act_end, d_end)
                sleep_start = min(sleep_start, d_start)
                sleep_end = max(sleep_end, d_end)
                intra_start = min(intra_start, d_start)
                intra_end = max(intra_end, d_end)
            except Exception:
                pass

        if start_hint and end_hint and start_hint <= end_hint:
            sleep_start = min(sleep_start, start_hint)
            sleep_end = max(sleep_end, end_hint)

        result["windows"] = {
            "activity": {"start_ts": act_start, "end_ts": act_end},
            "sleep": {"start_ts": sleep_start, "end_ts": sleep_end},
            "intraday": {"start_ts": intra_start, "end_ts": intra_end} if FETCH_INTRADAY_ON_WEBHOOK else None,
        }

        if appli_int == 44:
            raw_records.extend(_fetch_sleep_summary(headers, app_user_id, sleep_start, sleep_end))
            raw_records.extend(_fetch_sleep_detail(headers, app_user_id, sleep_start, sleep_end))
        elif appli_int == 16:
            raw_records.extend(_fetch_activity_by_range(headers, app_user_id, act_start, act_end))

            if FETCH_WORKOUTS_ON_WEBHOOK:
                try:
                    start_dt = datetime.datetime.utcfromtimestamp(act_start).date()
                    end_dt = datetime.datetime.utcfromtimestamp(act_end).date()
                    d = start_dt
                    while d <= end_dt:
                        raw_records.extend(_fetch_workouts_for_date(headers, app_user_id, d.isoformat()))
                        d = d + datetime.timedelta(days=1)
                except Exception:
                    pass

            if FETCH_INTRADAY_ON_WEBHOOK:
                raw_records.extend(_fetch_intraday_by_range(headers, app_user_id, intra_start, intra_end))
        else:
            raw_records.extend(_fetch_activity_by_range(headers, app_user_id, act_start, act_end))
            raw_records.extend(_fetch_sleep_summary(headers, app_user_id, sleep_start, sleep_end))
            raw_records.extend(_fetch_sleep_detail(headers, app_user_id, sleep_start, sleep_end))
            if FETCH_INTRADAY_ON_WEBHOOK:
                raw_records.extend(_fetch_intraday_by_range(headers, app_user_id, intra_start, intra_end))

    else:
        start_ts = int(args.get("start_ts"))
        end_ts = int(args.get("end_ts"))
        result["windows"] = {"range": {"start_ts": start_ts, "end_ts": end_ts}}

        raw_records.extend(_fetch_activity_by_range(headers, app_user_id, start_ts, end_ts))
        raw_records.extend(_fetch_sleep_summary(headers, app_user_id, start_ts, end_ts))
        raw_records.extend(_fetch_sleep_detail(headers, app_user_id, start_ts, end_ts))
        if FETCH_INTRADAY_ON_WEBHOOK:
            raw_records.extend(_fetch_intraday_by_range(headers, app_user_id, start_ts, end_ts))

    filtered = []
    skipped = 0
    counts_by_type: Dict[str, int] = {}
    max_ts_by_type: Dict[str, int] = {}

    for rec in raw_records:
        dt = rec.get("DataType") or "Unknown"
        counts_by_type[dt] = counts_by_type.get(dt, 0) + 1

        ts = _safe_int(rec.get("_MaxTs"))
        if ts:
            prev = max_ts_by_type.get(dt, 0)
            if ts > prev:
                max_ts_by_type[dt] = ts

        if _dedup_should_ingest(app_user_id, dt, rec.get("RecordKey") or "", rec.get("RawData") or ""):
            filtered.append(rec)
        else:
            skipped += 1

    if filtered:
        ingest_raw_to_adx(filtered)

    result["raw_records"] = len(raw_records)
    result["ingested"] = len(filtered)
    result["skipped_dup"] = skipped
    result["counts_by_type"] = counts_by_type
    result["max_ts_by_type"] = max_ts_by_type

    _apply_cursor_updates(app_user_id, max_ts_by_type)

    return result


def _apply_cursor_updates(app_user_id: str, max_ts_by_type: Dict[str, int]) -> None:
    if not app_user_id:
        return
    if not max_ts_by_type:
        return

    for dt, ts in max_ts_by_type.items():
        if dt == "SleepDetail":
            _update_sync_last_ts_max(app_user_id, "SleepDetail", ts)
            _update_sync_last_ts_max(app_user_id, "Sleep", ts)
        elif dt == "Sleep":
            _update_sync_last_ts_max(app_user_id, "Sleep", ts)
        elif dt == "Activity":
            _update_sync_last_ts_max(app_user_id, "Activity", ts)
        elif dt == "Intraday":
            _update_sync_last_ts_max(app_user_id, "Intraday", ts)
        elif dt == "Workouts":
            _update_sync_last_ts_max(app_user_id, "Workouts", ts)
