# -*- coding: utf-8 -*-
"""
Withings API data fetch functions: Activity, Sleep, SleepDetail, Intraday, Workouts.
"""

import logging
import datetime

from functions.config import INTRADAY_DATA_FIELDS
from functions.shared import withings_post, canonical_json, _safe_int


def _fetch_activity_for_date(headers, app_user_id: str, date_str: str):
    raw_records = []
    try:
        url = "https://wbsapi.withings.net/v2/measure?action=getactivity"
        params = {
            "startdateymd": date_str,
            "enddateymd": date_str,
            "data_fields": (
                "steps,distance,elevation,soft,moderate,intense,active,calories,totalcalories,"
                "hr_average,hr_min,hr_max,hr_zone_0,hr_zone_1,hr_zone_2,hr_zone_3"
            ),
        }
        r = withings_post(url, headers=headers, data=params)
        if r.get("status") == 0:
            for item in r.get("body", {}).get("activities", []):
                rk = str(item.get("date") or date_str)
                try:
                    d = datetime.date.fromisoformat(rk)
                    max_ts = int(datetime.datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=datetime.timezone.utc).timestamp())
                except Exception:
                    max_ts = None

                rec = {"DataType": "Activity", "RecordKey": rk, "RawData": canonical_json(item), "AppUserId": app_user_id}
                if max_ts:
                    rec["_MaxTs"] = max_ts
                raw_records.append(rec)
        else:
            logging.warning(f"[Fetch Activity] Withings status={r.get('status')} date={date_str}")
    except Exception as e:
        logging.warning(f"[Fetch Activity] Error: {e}")
    return raw_records


def _fetch_activity_by_range(headers, app_user_id: str, start_ts: int, end_ts: int):
    raw_records = []
    try:
        start_dt = datetime.datetime.utcfromtimestamp(start_ts).date()
        end_dt = datetime.datetime.utcfromtimestamp(end_ts).date()
        d = start_dt
        while d <= end_dt:
            raw_records.extend(_fetch_activity_for_date(headers, app_user_id, d.isoformat()))
            d = d + datetime.timedelta(days=1)
    except Exception:
        pass
    return raw_records


def _fetch_sleep_summary(headers, app_user_id: str, start_ts: int, end_ts: int):
    raw_records = []
    try:
        url = "https://wbsapi.withings.net/v2/sleep?action=getsummary"
        params = {
            "startdateymd": datetime.datetime.utcfromtimestamp(start_ts).strftime("%Y-%m-%d"),
            "enddateymd": datetime.datetime.utcfromtimestamp(end_ts).strftime("%Y-%m-%d"),
            "data_fields": (
                "breathing_disturbances_intensity,deepsleepduration,durationtosleep,durationtowakeup,hr_average,hr_max,"
                "hr_min,lightsleepduration,mvt_active_duration,mvt_score_avg,night_events,out_of_bed_count,remsleepduration,"
                "rr_average,rr_max,rr_min,sleep_score,snoring,snoringepisodecount,total_sleep_time,total_timeinbed,wakeupcount,"
                "wakeupduration,waso,sleep_efficiency,sleep_latency,wakeup_latency"
            ),
        }
        r = withings_post(url, headers=headers, data=params)
        if r.get("status") == 0:
            for item in r.get("body", {}).get("series", []):
                rk = str(item.get("id") or item.get("date") or f"{start_ts}-{end_ts}")

                max_ts = _safe_int(item.get("enddate")) or _safe_int(item.get("enddateutc"))
                if not max_ts:
                    d = item.get("date")
                    if d:
                        try:
                            dd = datetime.date.fromisoformat(str(d))
                            max_ts = int(datetime.datetime(dd.year, dd.month, dd.day, 23, 59, 59, tzinfo=datetime.timezone.utc).timestamp())
                        except Exception:
                            max_ts = None

                rec = {"DataType": "Sleep", "RecordKey": rk, "RawData": canonical_json(item), "AppUserId": app_user_id}
                if max_ts:
                    rec["_MaxTs"] = max_ts
                raw_records.append(rec)
        else:
            logging.warning(f"[Fetch SleepSummary] Withings status={r.get('status')} range={start_ts}-{end_ts}")
    except Exception as e:
        logging.warning(f"[Fetch SleepSummary] Error: {e}")
    return raw_records


def _fetch_sleep_detail(headers, app_user_id: str, start_ts: int, end_ts: int):
    raw_records = []
    try:
        url = "https://wbsapi.withings.net/v2/sleep?action=get"
        params = {"startdate": start_ts, "enddate": end_ts, "data_fields": "hr,rr,snoring,sdnn_1,rmssd,mvt_score"}
        r = withings_post(url, headers=headers, data=params)
        if r.get("status") == 0:
            for item in r.get("body", {}).get("series", []):
                rk = f"{item.get('startdate')}-{item.get('enddate')}"
                max_ts = _safe_int(item.get("enddate")) or _safe_int(item.get("enddateutc"))

                rec = {"DataType": "SleepDetail", "RecordKey": rk, "RawData": canonical_json(item), "AppUserId": app_user_id}
                if max_ts:
                    rec["_MaxTs"] = max_ts
                raw_records.append(rec)
        else:
            logging.warning(f"[Fetch SleepDetail] Withings status={r.get('status')} range={start_ts}-{end_ts}")
    except Exception as e:
        logging.warning(f"[Fetch SleepDetail] Error: {e}")
    return raw_records


def _fetch_intraday_by_range(headers, app_user_id: str, start_ts: int, end_ts: int):
    raw_records = []
    try:
        url = "https://wbsapi.withings.net/v2/measure?action=getintradayactivity"
        s = int(start_ts)
        e = int(end_ts)
        chunk_start = s
        while chunk_start <= e:
            chunk_end = min(e, chunk_start + 86399)
            params = {"startdate": chunk_start, "enddate": chunk_end, "data_fields": INTRADAY_DATA_FIELDS}
            r = withings_post(url, headers=headers, data=params)
            if r.get("status") == 0:
                series = r.get("body", {}).get("series", {}) or {}
                for ts, val in series.items():
                    try:
                        ts_i = int(ts)
                    except Exception:
                        continue
                    if not isinstance(val, dict):
                        val = {"value": val}
                    val["timestamp"] = ts_i
                    rk = str(ts_i)

                    rec = {"DataType": "Intraday", "RecordKey": rk, "RawData": canonical_json(val), "AppUserId": app_user_id}
                    rec["_MaxTs"] = ts_i
                    raw_records.append(rec)
            else:
                logging.warning(f"[Fetch Intraday] Withings status={r.get('status')} range={chunk_start}-{chunk_end}")

            chunk_start = chunk_end + 1
    except Exception as e:
        logging.warning(f"[Fetch Intraday] Error: {e}")
    return raw_records


def _fetch_workouts_for_date(headers, app_user_id: str, date_str: str):
    raw_records = []
    try:
        url = "https://wbsapi.withings.net/v2/measure?action=getworkouts"
        params = {"startdateymd": date_str, "enddateymd": date_str}
        r = withings_post(url, headers=headers, data=params)
        if r.get("status") == 0:
            for item in r.get("body", {}).get("series", []):
                rk = str(item.get("id") or item.get("date") or date_str)

                max_ts = _safe_int(item.get("enddate")) or _safe_int(item.get("enddateutc"))
                if not max_ts:
                    d = item.get("date") or date_str
                    try:
                        dd = datetime.date.fromisoformat(str(d))
                        max_ts = int(datetime.datetime(dd.year, dd.month, dd.day, 23, 59, 59, tzinfo=datetime.timezone.utc).timestamp())
                    except Exception:
                        max_ts = None

                rec = {"DataType": "Workouts", "RecordKey": rk, "RawData": canonical_json(item), "AppUserId": app_user_id}
                if max_ts:
                    rec["_MaxTs"] = max_ts
                raw_records.append(rec)
        else:
            logging.warning(f"[Fetch Workouts] Withings status={r.get('status')} date={date_str}")
    except Exception as e:
        logging.warning(f"[Fetch Workouts] Error: {e}")
    return raw_records
