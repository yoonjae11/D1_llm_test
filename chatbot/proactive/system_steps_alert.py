"""Phone STEPS system-initiated alert detector."""

from __future__ import annotations

import datetime as dt
import json
import logging
import math
import os
import tempfile
import uuid
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

try:
    from azure.kusto.data import DataFormat, KustoConnectionStringBuilder
    from azure.kusto.ingest import FileDescriptor, IngestionProperties, QueuedIngestClient
    from azure.storage.queue import QueueClient
except Exception:  # pragma: no cover - local unit tests may run without Azure SDKs.
    DataFormat = None
    KustoConnectionStringBuilder = None
    FileDescriptor = None
    IngestionProperties = None
    QueuedIngestClient = None
    QueueClient = None

try:
    from chatbot.adx_client import _build_adx_credential
except Exception:  # pragma: no cover - local unit tests may run without Azure SDKs.
    _build_adx_credential = None
from chatbot.config import get_settings
from chatbot.i18n import message
from chatbot.operational_storage import DailyTriggerState, utc_now
from chatbot.policies.semantic_resources import ACTIVITY_DAILY, physical_name, tabular_call
from chatbot.services.adx import ADXQueryPort, default_adx_service
from chatbot.services.memory import OperationalStoragePort, create_operational_storage
from queue_contracts import SYSTEM_PHONE_INGEST_EVENT_QUEUE, SYSTEM_STEPS_RETRY_QUEUE
from azure_storage_resources import create_queue_client

logger = logging.getLogger(__name__)

try:
    adx_manager: Optional[ADXQueryPort] = default_adx_service()
except Exception:  # pragma: no cover - optional local dependency path.
    adx_manager = None


def OperationalTableRepository(
    connection_string: str | None = None,
    *,
    fail_soft: bool = True,
) -> OperationalStoragePort:
    """Compatibility constructor routed through the memory service."""

    return create_operational_storage(
        connection_string=connection_string,
        fail_soft=fail_soft,
    )


def _quote(value: str) -> str:
    return "'" + str(value or "").replace("'", "''") + "'"


def _iso_utc(value: Optional[dt.datetime] = None) -> str:
    value = value or utc_now()
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc).isoformat()


def _date_start(value: dt.date) -> str:
    return f"{value.isoformat()}T00:00:00"


def _parse_date(value: Any) -> Optional[dt.date]:
    if isinstance(value, dt.date) and not isinstance(value, dt.datetime):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return dt.date.fromisoformat(text[:10])
    except Exception:
        return None


def _float(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
        if math.isnan(out) or math.isinf(out):
            return default
        return out
    except Exception:
        return default


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _canonical_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _steps_detection_dates(payload: Dict[str, Any]) -> List[dt.date]:
    values = payload.get("stepDatesLocal") or payload.get("stepsDatesLocal") or []
    if isinstance(values, str):
        values = [values]
    out: List[dt.date] = []
    for value in values if isinstance(values, list) else []:
        parsed = _parse_date(value)
        if parsed and parsed not in out:
            out.append(parsed)
    for key in ("minDateFromLocal", "maxDateToLocal"):
        parsed = _parse_date(payload.get(key))
        if parsed and parsed not in out:
            out.append(parsed)
    return out


def _retry_delay_seconds(retry_count: int) -> int:
    settings = get_settings()
    delays = settings.system_steps_alert_retry_delays_seconds
    if not isinstance(delays, list) or not delays:
        delays = [300, 900, 1800]
    idx = min(max(0, retry_count), len(delays) - 1)
    return max(60, _int(delays[idx], 300))


def _queue_client(queue_name: str) -> QueueClient:
    if QueueClient is None:
        raise RuntimeError("azure-storage-queue is unavailable.")
    connection_string = os.environ.get("AzureWebJobsStorage", "")
    if not connection_string:
        raise RuntimeError("AzureWebJobsStorage is missing.")
    return create_queue_client(connection_string, queue_name)


def enqueue_steps_retry(payload: Dict[str, Any], visibility_timeout: int) -> None:
    _queue_client(SYSTEM_STEPS_RETRY_QUEUE).send_message(
        _canonical_json(payload),
        visibility_timeout=max(0, int(visibility_timeout)),
    )


def build_steps_mad_kql(app_user_id: str, target_date: dt.date) -> str:
    settings = get_settings()
    activity_daily = physical_name(ACTIVITY_DAILY)
    return (
        f"let TargetUserId={_quote(app_user_id)};\n"
        f"let TargetDateLocal=datetime({_date_start(target_date)});\n"
        f"let ExpectedDayCount={int(settings.system_steps_baseline_days)};\n"
        f"let MinObservedDayCount={int(settings.system_steps_baseline_min_observed_days)};\n"
        f"let MinCoverageRatio={float(settings.system_steps_baseline_min_coverage_ratio)};\n"
        f"let WindowStartLocal=TargetDateLocal-{int(settings.system_steps_baseline_days)}d;\n"
        "let WindowEndExclusiveLocal=TargetDateLocal;\n"
        "let QueryEndLocal=TargetDateLocal+1d;\n"
        f"let ActivityRows={tabular_call(ACTIVITY_DAILY, 'TargetUserId', 'WindowStartLocal', 'QueryEndLocal')};\n"
        "let CurrentRows=ActivityRows | where UserId == TargetUserId | where Type == \"STEPS\" | where LocalDate >= TargetDateLocal and LocalDate < QueryEndLocal | summarize CurrentValue=sum(todouble(ValueSumExact)), CurrentRowCount=count(), Unit=take_any(Unit), CurrentSources=make_set(strcat(SourcePlatform,\":\",SourceName),10);\n"
        "let BaselineRows=ActivityRows | where UserId == TargetUserId | where Type == \"STEPS\" | where LocalDate >= WindowStartLocal and LocalDate < WindowEndExclusiveLocal | summarize Value=sum(todouble(ValueSumExact)), RowCount=count(), Unit=take_any(Unit) by LocalDate;\n"
        "let MedianValue=toscalar(BaselineRows | summarize percentile(Value,50));\n"
        "let BaselineSummary=BaselineRows | extend AbsDeviation=abs(Value-MedianValue) | summarize ObservedDayCount=dcount(LocalDate), MedianValue=take_any(MedianValue), MadValue=percentile(AbsDeviation,50), Unit=take_any(Unit), MinValue=min(Value), MaxValue=max(Value);\n"
        "CurrentRows"
        " | extend TargetDateLocal=TargetDateLocal, WindowStartLocal=WindowStartLocal, WindowEndExclusiveLocal=WindowEndExclusiveLocal"
        " | extend ExpectedDayCount=ExpectedDayCount, MinObservedDayCount=MinObservedDayCount, MinCoverageRatio=MinCoverageRatio"
        " | extend ObservedDayCount=tolong(toscalar(BaselineSummary | project ObservedDayCount)), MedianValue=todouble(toscalar(BaselineSummary | project MedianValue)), MadValue=todouble(toscalar(BaselineSummary | project MadValue)), BaselineUnit=tostring(toscalar(BaselineSummary | project Unit)), BaselineMinValue=todouble(toscalar(BaselineSummary | project MinValue)), BaselineMaxValue=todouble(toscalar(BaselineSummary | project MaxValue))"
        " | extend Unit=coalesce(Unit,BaselineUnit,\"count\"), CoverageRatio=todouble(ObservedDayCount)/todouble(ExpectedDayCount), ScaledMadValue=1.4826*MadValue"
        " | extend HasCurrentRow=CurrentRowCount > 0, IsBaselineUsable=ObservedDayCount >= MinObservedDayCount and CoverageRatio >= MinCoverageRatio and ScaledMadValue > 0"
        " | extend ZMad=iif(HasCurrentRow and IsBaselineUsable,(todouble(CurrentValue)-MedianValue)/ScaledMadValue,real(null))"
        " | project TargetDateLocal, WindowStartLocal, WindowEndExclusiveLocal, CurrentValue=todouble(CurrentValue), CurrentRowCount, Unit, ObservedDayCount, ExpectedDayCount, CoverageRatio, MedianValue, MadValue, ScaledMadValue, IsBaselineUsable, ZMad, BaselineMinValue, BaselineMaxValue, CurrentSources"
    )


def _adx_query_single(query: str) -> Dict[str, Any]:
    if adx_manager is None:
        raise RuntimeError("ADX client is unavailable.")
    result = adx_manager.execute_query(query, suppress_error_log=True)
    if isinstance(result, str) and result.lower().startswith("error"):
        raise RuntimeError(result)
    if isinstance(result, list) and result:
        return dict(result[0])
    return {}


def _ingest_json_records(table: str, mapping: str, records: List[Dict[str, Any]]) -> None:
    if not records:
        return
    if not all([DataFormat, KustoConnectionStringBuilder, FileDescriptor, IngestionProperties, QueuedIngestClient, _build_adx_credential]):
        raise RuntimeError("azure-kusto-ingest is unavailable.")
    ingest_uri = os.environ.get("ADX_INGEST_URI", "")
    database = os.environ.get("ADX_DATABASE", "")
    if not ingest_uri or not database:
        raise RuntimeError("ADX_INGEST_URI or ADX_DATABASE is missing.")

    kcsb = KustoConnectionStringBuilder.with_azure_token_credential(ingest_uri, _build_adx_credential())
    client = QueuedIngestClient(kcsb)
    with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".json", encoding="utf-8") as tf:
        for record in records:
            tf.write(_canonical_json(record) + "\n")
        temp_path = tf.name
    try:
        size = os.path.getsize(temp_path)
        client.ingest_from_file(
            FileDescriptor(temp_path, size=size),
            ingestion_properties=IngestionProperties(
                database=database,
                table=table,
                data_format=DataFormat.MULTIJSON,
                ingestion_mapping_reference=mapping,
            ),
        )
    finally:
        try:
            os.remove(temp_path)
        except Exception:
            pass


def _baseline_record(app_user_id: str, target_date: dt.date, row: Dict[str, Any]) -> Dict[str, Any]:
    settings = get_settings()
    return {
        "ComputedAtUtc": _iso_utc(),
        "BaselineForDateLocal": _date_start(target_date),
        "UserId": app_user_id,
        "SourcePlatform": "phone_mv",
        "SourceName": physical_name(ACTIVITY_DAILY),
        "MetricType": "STEPS",
        "MetricContext": "daily_total",
        "BaselineGroup": f"previous_{int(settings.system_steps_baseline_days)}d",
        "Unit": str(row.get("Unit") or "count"),
        "WindowStartLocal": str(row.get("WindowStartLocal") or _date_start(target_date - dt.timedelta(days=settings.system_steps_baseline_days))),
        "WindowEndExclusiveLocal": str(row.get("WindowEndExclusiveLocal") or _date_start(target_date)),
        "ExpectedDayCount": int(settings.system_steps_baseline_days),
        "ObservedDayCount": _int(row.get("ObservedDayCount")),
        "CoverageRatio": _float(row.get("CoverageRatio")),
        "MedianValue": _float(row.get("MedianValue")),
        "MadValue": _float(row.get("MadValue")),
        "ScaledMadValue": _float(row.get("ScaledMadValue")),
        "IsUsable": bool(row.get("IsBaselineUsable")),
        "RuleVersion": settings.system_steps_baseline_rule_version,
    }


def compose_steps_alert_message(row: Dict[str, Any], target_date: dt.date) -> str:
    current = _float(row.get("CurrentValue"))
    median = _float(row.get("MedianValue"))
    diff = current - median
    baseline_start = str(row.get("WindowStartLocal") or "")[:10]
    baseline_end = str(row.get("WindowEndExclusiveLocal") or "")[:10]
    return message(
        "system_steps.outlier",
        is_korean=False,
        target_date=target_date.isoformat(),
        current=f"{current:,.0f}",
        baseline_start=baseline_start,
        baseline_end=baseline_end,
        median=f"{median:,.0f}",
        difference=f"{diff:,.0f}",
    )


def _candidate_record(app_user_id: str, target_date: dt.date, row: Dict[str, Any], message: str, eligible: bool) -> Dict[str, Any]:
    settings = get_settings()
    evidence = {
        "messageText": message,
        "targetDateLocal": target_date.isoformat(),
        "windowStartLocal": str(row.get("WindowStartLocal") or "")[:10],
        "windowEndExclusiveLocal": str(row.get("WindowEndExclusiveLocal") or "")[:10],
        "observedDayCount": _int(row.get("ObservedDayCount")),
        "expectedDayCount": int(settings.system_steps_baseline_days),
        "coverageRatio": _float(row.get("CoverageRatio")),
        "sourceTable": physical_name(ACTIVITY_DAILY),
        "currentRowCount": _int(row.get("CurrentRowCount")),
        "currentSources": row.get("CurrentSources") or [],
    }
    return {
        "CreatedAtUtc": _iso_utc(),
        "TargetDateLocal": _date_start(target_date),
        "UserId": app_user_id,
        "MetricType": "STEPS",
        "ReasonCode": "low_daily_steps_vs_personal_baseline",
        "CurrentValue": _float(row.get("CurrentValue")),
        "BaselineMedianValue": _float(row.get("MedianValue")),
        "BaselineMadValue": _float(row.get("MadValue")),
        "BaselineScaledMadValue": _float(row.get("ScaledMadValue")),
        "ZMad": _float(row.get("ZMad")),
        "Severity": "candidate",
        "EvidenceJson": evidence,
        "IsEligibleForNotification": bool(eligible),
        "RuleVersion": settings.system_steps_baseline_rule_version,
    }


def _is_quiet_time(profile: Any, local_now: dt.datetime) -> bool:
    start = str(getattr(profile, "quiet_hours_start_local", "") or "").strip()
    end = str(getattr(profile, "quiet_hours_end_local", "") or "").strip()
    if not start or not end:
        return False
    try:
        start_time = dt.time.fromisoformat(start[:5])
        end_time = dt.time.fromisoformat(end[:5])
    except Exception:
        return False
    now_time = local_now.time().replace(second=0, microsecond=0)
    if start_time <= end_time:
        return start_time <= now_time < end_time
    return now_time >= start_time or now_time < end_time


def _send_notification_if_possible(app_user_id: str, message: str, payload: Dict[str, Any]) -> bool:
    try:
        from functions.shared import get_chat_id_by_user, send_telegram_message
        from functions.telegram_conversation_log import build_conversation_event, stage_conversation_event

        chat_id = get_chat_id_by_user(app_user_id)
        if not chat_id:
            return False
        send_telegram_message(chat_id, message)

        # Phase 3 (flag-gated): suspend the user's active reactive episode so the
        # alert becomes the active episode and /resume can restore the prior one.
        try:
            if get_settings().proactive_episode_interrupt_enabled:
                from chatbot.episode_service import suspend_active_episode_for_alert, thread_id_for_chat

                suspend_active_episode_for_alert(
                    app_user_id,
                    thread_id_for_chat(chat_id),
                    alert_context=payload,
                    alert_message_text=message,
                )
        except Exception as exc:
            logger.warning("proactive episode interrupt skipped app_user_id=%s: %s", app_user_id, exc)

        event = build_conversation_event(
            event_type="proactive_alert",
            role="assistant",
            direction="outbound",
            event_time_utc=utc_now(),
            app_user_id=app_user_id,
            telegram_chat_id=chat_id,
            message_text=message,
            route="system_initiated_steps_alert",
            intent="proactive_alert",
            execution_status="sent",
        )
        stage_conversation_event(event)
        return True
    except Exception as exc:
        logger.warning("system steps alert notification failed app_user_id=%s: %s", app_user_id, exc)
        return False


def _save_state(
    repository: OperationalTableRepository,
    state: DailyTriggerState,
    *,
    status: str,
    timezone_iana: str,
    first_detected_local: str,
    target_date: dt.date,
    retry_count: int,
    alert_candidate_count: int = 0,
    notification_sent: bool = False,
    last_candidate_id: str = "",
    last_error: str = "",
) -> None:
    state.status = status
    state.evaluation_status = status
    state.timezone_iana = timezone_iana
    state.first_post_08_step_detected_at_local = state.first_post_08_step_detected_at_local or first_detected_local
    state.evaluation_target_date_local = target_date.isoformat()
    state.retry_count = retry_count
    state.detector_attempt_count = retry_count
    state.alert_candidate_count = alert_candidate_count
    state.notification_sent = notification_sent
    state.last_candidate_id = last_candidate_id
    state.last_error = last_error
    state.first_event_utc = state.first_event_utc or utc_now()
    state.last_event_utc = utc_now()
    state.last_notification_utc = utc_now() if notification_sent else state.last_notification_utc
    repository.save_trigger_state(state)


def _retry_payload(app_user_id: str, trigger_date: dt.date, target_date: dt.date, timezone: str, reason: str, retry_count: int) -> Dict[str, Any]:
    return {
        "appUserId": app_user_id,
        "triggerDateLocal": trigger_date.isoformat(),
        "evaluationTargetDateLocal": target_date.isoformat(),
        "timezoneIana": timezone,
        "reason": reason,
        "retryCount": retry_count,
    }


def process_steps_event(
    payload: Dict[str, Any],
    *,
    repository: Optional[OperationalTableRepository] = None,
    now_utc: Optional[dt.datetime] = None,
    allow_missing_step_date: bool = False,
) -> Dict[str, Any]:
    settings = get_settings()
    repository = repository or OperationalTableRepository(fail_soft=False)
    now_utc = now_utc or utc_now()
    app_user_id = str(payload.get("appUserId") or payload.get("userId") or "").strip()
    if not app_user_id:
        return {"status": "skipped", "reason": "missing_app_user_id"}

    profile = repository.get_user_profile(app_user_id)
    timezone = str(payload.get("timezoneIana") or profile.timezone_iana or settings.default_timezone)
    local_now = now_utc.astimezone(ZoneInfo(timezone))
    trigger_date = _parse_date(payload.get("triggerDateLocal")) or local_now.date()
    target_date = _parse_date(payload.get("evaluationTargetDateLocal")) or (trigger_date - dt.timedelta(days=1))
    retry_count = _int(payload.get("retryCount"))
    step_dates = _steps_detection_dates(payload)

    if not settings.system_steps_alert_enabled or not profile.system_steps_alert_enabled:
        return {"status": "skipped", "reason": "disabled", "appUserId": app_user_id}
    try:
        from functions.shared import get_chat_id_by_user

        if not get_chat_id_by_user(app_user_id):
            return {
                "status": "skipped",
                "reason": "no_telegram_delivery_target",
                "appUserId": app_user_id,
            }
    except Exception:
        return {
            "status": "skipped",
            "reason": "no_telegram_delivery_target",
            "appUserId": app_user_id,
        }
    if local_now.hour < settings.system_steps_alert_trigger_hour_local:
        return {"status": "skipped", "reason": "before_trigger_hour", "appUserId": app_user_id, "localNow": local_now.isoformat()}
    if not allow_missing_step_date and trigger_date not in step_dates:
        return {"status": "skipped", "reason": "no_today_steps_in_event", "appUserId": app_user_id, "triggerDateLocal": trigger_date.isoformat()}

    state = repository.get_trigger_state(app_user_id, trigger_date.isoformat())
    if state.status in {"completed", "skipped_baseline_unusable", "skipped_no_candidate"}:
        return {"status": "skipped", "reason": "already_final", "appUserId": app_user_id, "state": state.status}

    first_detected_local = local_now.replace(microsecond=0).isoformat()
    earliest = local_now.replace(
        hour=settings.system_steps_alert_earliest_evaluation_hour_local,
        minute=settings.system_steps_alert_earliest_evaluation_minute_local,
        second=0,
        microsecond=0,
    )
    if local_now < earliest:
        next_retry_count = retry_count + 1
        delay = max(60, int((earliest - local_now).total_seconds()))
        enqueue_steps_retry(
            _retry_payload(app_user_id, trigger_date, target_date, timezone, "before_earliest_evaluation_time", next_retry_count),
            delay,
        )
        _save_state(
            repository,
            state,
            status="pending",
            timezone_iana=timezone,
            first_detected_local=first_detected_local,
            target_date=target_date,
            retry_count=next_retry_count,
        )
        return {"status": "retry_enqueued", "reason": "before_earliest_evaluation_time", "delaySeconds": delay}

    query = build_steps_mad_kql(app_user_id, target_date)
    row = _adx_query_single(query)
    baseline = _baseline_record(app_user_id, target_date, row)
    _ingest_json_records(settings.adx_alert_baseline_table, settings.adx_alert_baseline_mapping, [baseline])

    if _int(row.get("CurrentRowCount")) <= 0:
        next_retry_count = retry_count + 1
        if next_retry_count <= len(settings.system_steps_alert_retry_delays_seconds):
            delay = _retry_delay_seconds(next_retry_count - 1)
            enqueue_steps_retry(
                _retry_payload(app_user_id, trigger_date, target_date, timezone, "missing_target_daily_mv_row", next_retry_count),
                delay,
            )
            status = "pending"
            reason = "missing_target_daily_mv_row"
        else:
            delay = 0
            status = "skipped_missing_target_row"
            reason = "missing_target_daily_mv_row_after_retries"
        _save_state(
            repository,
            state,
            status=status,
            timezone_iana=timezone,
            first_detected_local=first_detected_local,
            target_date=target_date,
            retry_count=next_retry_count,
            last_error=reason,
        )
        return {"status": status, "reason": reason, "delaySeconds": delay}

    if not bool(row.get("IsBaselineUsable")):
        _save_state(
            repository,
            state,
            status="skipped_baseline_unusable",
            timezone_iana=timezone,
            first_detected_local=first_detected_local,
            target_date=target_date,
            retry_count=retry_count,
            last_error="baseline_unusable",
        )
        return {
            "status": "skipped_baseline_unusable",
            "observedDayCount": _int(row.get("ObservedDayCount")),
            "coverageRatio": _float(row.get("CoverageRatio")),
            "scaledMadValue": _float(row.get("ScaledMadValue")),
        }

    z_mad = _float(row.get("ZMad"), default=999.0)
    if z_mad > float(settings.system_steps_alert_zmad_threshold):
        _save_state(
            repository,
            state,
            status="skipped_no_candidate",
            timezone_iana=timezone,
            first_detected_local=first_detected_local,
            target_date=target_date,
            retry_count=retry_count,
        )
        return {"status": "skipped_no_candidate", "zMad": z_mad}

    message = compose_steps_alert_message(row, target_date)
    eligible = bool(settings.system_steps_alert_enabled and profile.system_steps_alert_enabled and not _is_quiet_time(profile, local_now))
    candidate_id = uuid.uuid4().hex
    candidate = _candidate_record(app_user_id, target_date, row, message, eligible)
    candidate["EvidenceJson"]["candidateId"] = candidate_id
    _ingest_json_records(settings.adx_alert_candidate_table, settings.adx_alert_candidate_mapping, [candidate])
    notification_sent = _send_notification_if_possible(app_user_id, message, candidate["EvidenceJson"]) if eligible else False
    _save_state(
        repository,
        state,
        status="completed",
        timezone_iana=timezone,
        first_detected_local=first_detected_local,
        target_date=target_date,
        retry_count=retry_count,
        alert_candidate_count=1,
        notification_sent=notification_sent,
        last_candidate_id=candidate_id,
    )
    return {
        "status": "completed",
        "candidateCreated": True,
        "notificationSent": notification_sent,
        "eligibleForNotification": eligible,
        "zMad": z_mad,
        "candidateId": candidate_id,
    }


def process_due_user_profiles(mode: str, limit: int = 500) -> Dict[str, Any]:
    repository = OperationalTableRepository(fail_soft=False)
    settings = get_settings()
    profiles = repository.list_active_user_profiles(limit=limit)
    processed = []
    for profile in profiles:
        if not profile.system_steps_alert_enabled:
            continue
        now_local = utc_now().astimezone(ZoneInfo(profile.timezone_iana))
        if mode == "fallback" and now_local.hour < settings.system_steps_alert_fallback_hour_local:
            continue
        if mode == "reconciliation" and now_local.hour < settings.system_steps_alert_reconciliation_hour_local:
            continue
        trigger_date = now_local.date()
        target_date = trigger_date - dt.timedelta(days=1)
        state = repository.get_trigger_state(profile.app_user_id, trigger_date.isoformat())
        if mode == "fallback" and state.status not in {"new"}:
            continue
        if mode == "reconciliation" and state.status not in {"pending", "failed", "skipped_missing_target_row"}:
            continue
        result = process_steps_event(
            {
                "appUserId": profile.app_user_id,
                "triggerDateLocal": trigger_date.isoformat(),
                "evaluationTargetDateLocal": target_date.isoformat(),
                "timezoneIana": profile.timezone_iana,
                "reason": mode,
                "retryCount": state.retry_count,
            },
            repository=repository,
            allow_missing_step_date=True,
        )
        processed.append({"appUserId": profile.app_user_id, "result": result})
    return {"mode": mode, "profileCount": len(profiles), "processedCount": len(processed), "processed": processed}


def _telegram_chat_mapped(app_user_id: str) -> Dict[str, Any]:
    """Best-effort check whether the user has a Telegram chat mapping (alert delivery target)."""
    try:
        from functions.shared import get_chat_id_by_user

        chat_id = get_chat_id_by_user(app_user_id)
        return {"telegramChatMapped": bool(chat_id)}
    except Exception as exc:  # pragma: no cover - depends on storage availability.
        return {"telegramChatMapped": None, "telegramMappingError": str(exc)}


def _baseline_diagnostics(app_user_id: str, target_date: dt.date) -> Dict[str, Any]:
    """Run the read-only MAD KQL and report each end-to-end evaluation stage.

    This never mutates state or ingests; it only explains what the detector would
    decide for ``target_date`` given current ADX data. Any failure is reported as a
    field rather than raised, so the diagnostics endpoint stays usable when ADX is
    unreachable.
    """
    settings = get_settings()
    try:
        row = _adx_query_single(build_steps_mad_kql(app_user_id, target_date))
    except Exception as exc:
        return {"available": False, "error": str(exc)}

    if not row:
        return {
            "available": True,
            "targetDailyMvRowExists": False,
            "expectedOutcome": "skipped_missing_target_row",
        }

    current_row_count = _int(row.get("CurrentRowCount"))
    is_usable = bool(row.get("IsBaselineUsable"))
    raw_zmad = row.get("ZMad")
    z_mad = _float(raw_zmad, default=None) if raw_zmad is not None else None
    threshold = float(settings.system_steps_alert_zmad_threshold)

    if current_row_count <= 0:
        expected = "skipped_missing_target_row"
    elif not is_usable:
        expected = "skipped_baseline_unusable"
    elif z_mad is None:
        expected = "skipped_no_candidate"
    elif z_mad <= threshold:
        expected = "candidate_eligible"
    else:
        expected = "skipped_no_candidate"

    return {
        "available": True,
        "targetDailyMvRowExists": current_row_count > 0,
        "currentValue": _float(row.get("CurrentValue")),
        "currentRowCount": current_row_count,
        "observedDayCount": _int(row.get("ObservedDayCount")),
        "expectedDayCount": int(settings.system_steps_baseline_days),
        "coverageRatio": _float(row.get("CoverageRatio")),
        "medianValue": _float(row.get("MedianValue")),
        "madValue": _float(row.get("MadValue")),
        "scaledMadValue": _float(row.get("ScaledMadValue")),
        "isBaselineUsable": is_usable,
        "zMad": z_mad,
        "zMadThreshold": threshold,
        "expectedOutcome": expected,
    }


def diagnostics_for_user(app_user_id: str) -> Dict[str, Any]:
    """Read-only end-to-end diagnostics for the STEPS proactive alert path.

    Reports each wiring stage so an operator can tell *why* an alert did or did not
    fire: config flags, timezone gating, the target Daily MV row, baseline usability,
    the MAD/ZMad computation, Telegram delivery mapping, ingestion config, and the
    persisted trigger state. It does not mutate state or ingest anything.
    """
    settings = get_settings()
    repository = OperationalTableRepository(fail_soft=True)
    profile = repository.get_user_profile(app_user_id)

    timezone = profile.timezone_iana or settings.default_timezone
    try:
        now_local = utc_now().astimezone(ZoneInfo(timezone))
    except Exception:
        try:
            timezone = settings.default_timezone
            now_local = utc_now().astimezone(ZoneInfo(timezone))
        except Exception:
            # Last-resort fallback so the read-only diagnostic never raises purely
            # because timezone data is unavailable in the runtime.
            timezone = "UTC"
            now_local = utc_now()

    trigger_date = now_local.date()
    target_date = trigger_date - dt.timedelta(days=1)
    state = repository.get_trigger_state(app_user_id, trigger_date.isoformat())

    result: Dict[str, Any] = {
        "appUserId": app_user_id,
        "timezoneIana": timezone,
        "timezoneSource": getattr(profile, "timezone_source", ""),
        "localNow": now_local.isoformat(),
        "triggerDateLocal": trigger_date.isoformat(),
        "evaluationTargetDateLocal": target_date.isoformat(),
        "config": {
            "systemAlertEnabledGlobal": bool(settings.system_steps_alert_enabled),
            "profileAlertEnabled": bool(profile.system_steps_alert_enabled),
            "triggerHourLocal": settings.system_steps_alert_trigger_hour_local,
            "earliestEvaluationLocal": (
                f"{settings.system_steps_alert_earliest_evaluation_hour_local:02d}:"
                f"{settings.system_steps_alert_earliest_evaluation_minute_local:02d}"
            ),
            "fallbackHourLocal": settings.system_steps_alert_fallback_hour_local,
            "reconciliationHourLocal": settings.system_steps_alert_reconciliation_hour_local,
            "zmadThreshold": settings.system_steps_alert_zmad_threshold,
            "baselineDays": settings.system_steps_baseline_days,
            "baselineMinObservedDays": settings.system_steps_baseline_min_observed_days,
            "baselineMinCoverageRatio": settings.system_steps_baseline_min_coverage_ratio,
        },
        "queues": {
            "ingestEventQueue": SYSTEM_PHONE_INGEST_EVENT_QUEUE,
            "retryQueue": SYSTEM_STEPS_RETRY_QUEUE,
        },
        "ingestion": {
            "baselineTable": settings.adx_alert_baseline_table,
            "baselineMapping": settings.adx_alert_baseline_mapping,
            "candidateTable": settings.adx_alert_candidate_table,
            "candidateMapping": settings.adx_alert_candidate_mapping,
        },
        "stateStatus": state.status,
        "notificationSent": bool(state.notification_sent),
        "alertCandidateCount": int(state.alert_candidate_count or 0),
        "retryCount": state.retry_count,
        "lastError": state.last_error,
        "state": state.raw or {
            "status": state.status,
            "retryCount": state.retry_count,
            "evaluationTargetDateLocal": state.evaluation_target_date_local,
        },
    }
    result.update(_telegram_chat_mapped(app_user_id))
    result["baseline"] = _baseline_diagnostics(app_user_id, target_date)
    return result
