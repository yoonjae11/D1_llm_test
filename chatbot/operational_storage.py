"""Operational Azure Table helpers for the health chatbot."""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

try:
    from azure.core.exceptions import ResourceNotFoundError
    from azure.data.tables import TableClient, UpdateMode
except Exception:  # pragma: no cover - local dry runs may not import Azure SDKs.
    ResourceNotFoundError = Exception
    TableClient = None
    UpdateMode = None

from chatbot.config import get_settings
from azure_storage_resources import create_table_client

logger = logging.getLogger(__name__)

SUPPORTED_IANA_TIMEZONES = {
    "Asia/Seoul",
    "Europe/London",
    "Europe/Madrid",
    "Atlantic/Canary",
    "Africa/Ceuta",
}


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _json_loads(value: Any, default: Any) -> Any:
    if not value:
        return default
    try:
        parsed = json.loads(str(value))
        return parsed
    except Exception:
        return default


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


def _parse_optional_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except Exception:
        return None


def _parse_summary_times(
    raw_value: Any,
    legacy_hour: Optional[int] = None,
    legacy_minute: int = 0,
) -> List[Tuple[int, int]]:
    """Parse the ``SummaryReportTimes`` field ("0900,1645") into a sorted, unique
    list of (hour, minute). Falls back to the legacy single (hour, minute) when the
    list field is absent so existing one-time users are preserved."""
    text = str(raw_value or "").strip()
    times: List[Tuple[int, int]] = []
    if text:
        for tok in text.split(","):
            tok = tok.strip()
            if len(tok) == 4 and tok.isdigit():
                h, m = int(tok[:2]), int(tok[2:])
                if 0 <= h <= 23 and 0 <= m <= 59:
                    times.append((h, m))
    if not times and legacy_hour is not None and int(legacy_hour) >= 0:
        h = int(legacy_hour)
        m = max(0, min(59, int(legacy_minute or 0)))
        if 0 <= h <= 23:
            times.append((h, m))
    return sorted(set(times))


def _format_summary_times(times: List[Tuple[int, int]]) -> str:
    uniq = sorted({
        (int(h), int(m))
        for (h, m) in times
        if 0 <= int(h) <= 23 and 0 <= int(m) <= 59
    })
    return ",".join(f"{h:02d}{m:02d}" for (h, m) in uniq)


def _round_to_5(hour: int, minute: int) -> Tuple[int, int]:
    """Snap a clock time to the nearest 5-minute mark (the daily-summary timer fires
    on a 5-minute grid). Never crosses midnight (23:58 -> 23:55)."""
    total = int(hour) * 60 + int(minute)
    rounded = int(round(total / 5.0)) * 5
    if rounded >= 24 * 60:
        rounded = 23 * 60 + 55
    if rounded < 0:
        rounded = 0
    return rounded // 60, rounded % 60


def _summary_fields_from_entity(entity: Dict[str, Any]) -> Dict[str, Any]:
    """Build the ChatbotUserProfile summary fields from a stored row, migrating
    legacy single-time users (SummaryReportHour/Minute) into the times list."""
    legacy_hour = _parse_optional_int(entity.get("SummaryReportHour"))
    legacy_minute = _parse_optional_int(entity.get("SummaryReportMinute")) or 0
    times = _parse_summary_times(entity.get("SummaryReportTimes"), legacy_hour, legacy_minute)
    if times:
        return {
            "summary_report_hour": times[0][0],
            "summary_report_minute": times[0][1],
            "summary_report_times": times,
        }
    # No scheduled times: keep legacy hour so None (never set) vs -1 (opted out) is preserved.
    return {
        "summary_report_hour": legacy_hour,
        "summary_report_minute": legacy_minute,
        "summary_report_times": [],
    }


def _valid_timezone(value: str, fallback: str) -> str:
    candidate = str(value or "").strip() or fallback
    try:
        ZoneInfo(candidate)
        return candidate
    except Exception:
        if candidate in SUPPORTED_IANA_TIMEZONES:
            return candidate
        logger.warning("Invalid timezone %r; falling back to %s", candidate, fallback)
        return fallback


@dataclass
class ChatbotUserProfile:
    app_user_id: str
    timezone_iana: str
    locale: str = "en-US"
    study_site_code: str = ""
    country_code: str = ""
    timezone_source: str = "default"
    default_trend_days: int = 14
    system_steps_alert_enabled: bool = True
    quiet_hours_start_local: str = ""
    quiet_hours_end_local: str = ""
    # Per-user daily summary schedule. summary_report_times holds every chosen
    # local (hour, minute) the user wants a summary at (0..N). summary_report_hour/
    # minute are kept as the FIRST time for backward compatibility: None = never
    # set, a value < 0 means the user opted out ("pass"), and when times exist they
    # mirror times[0].
    summary_report_hour: Optional[int] = None
    summary_report_minute: int = 0
    summary_report_times: List[Tuple[int, int]] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ChatbotConversationState:
    app_user_id: str
    thread_id: str
    recent_turns: List[Dict[str, Any]] = field(default_factory=list)
    rolling_summary: str = ""
    active_context: Dict[str, Any] = field(default_factory=dict)
    turn_count: int = 0
    state_version: int = 0
    last_user_message_utc: Optional[dt.datetime] = None
    last_assistant_message_utc: Optional[dt.datetime] = None
    # Phase 3: Responses API chain + episode interrupt/resume (all optional /
    # backward compatible; ignored when CHATBOT_USE_AZURE_RESPONSES_API is off).
    latest_response_id: str = ""
    response_chain_started_at_utc: Optional[dt.datetime] = None
    active_episode_id: str = ""
    active_episode_type: str = "reactive"
    suspended_episodes: List[Dict[str, Any]] = field(default_factory=list)
    fallback_summary: str = ""
    pending_clarification: Dict[str, Any] = field(default_factory=dict)


@dataclass
class MetricAvailability:
    app_user_id: str
    metric_type: str
    is_available: bool
    preferred_source_table: str = ""
    preferred_source_policy: str = "unified_flat_policy"
    earliest_date_from_local: Any = None
    latest_date_from_local: Any = None
    latest_date_to_local: Any = None
    latest_ingestion_time_utc: Any = None
    last_collected_time_local: Any = None
    freshness_status: str = "unknown"
    refreshed_at_utc: Any = None
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class MetricAlertThreshold:
    app_user_id: str
    metric_type: str
    lower: Optional[float] = None
    upper: Optional[float] = None
    sustain_minutes: int = 10
    enabled: bool = True
    last_alert_at_utc: Any = None
    updated_at_utc: Any = None
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class DailyTriggerState:
    app_user_id: str
    local_date: str
    status: str = "new"
    timezone_iana: str = ""
    first_post_08_step_detected_at_local: str = ""
    evaluation_target_date_local: str = ""
    evaluation_status: str = "new"
    alert_candidate_count: int = 0
    notification_sent: bool = False
    retry_count: int = 0
    last_error: str = ""
    first_event_utc: Optional[dt.datetime] = None
    last_event_utc: Optional[dt.datetime] = None
    detector_attempt_count: int = 0
    last_candidate_id: str = ""
    last_notification_utc: Optional[dt.datetime] = None
    raw: Dict[str, Any] = field(default_factory=dict)


def _env_bool(value: Any, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


class OperationalTableRepository:
    def __init__(self, connection_string: Optional[str] = None, *, fail_soft: bool = True) -> None:
        settings = get_settings()
        self.connection_string = connection_string or os.environ.get("AzureWebJobsStorage", "")
        self.fail_soft = fail_soft
        self.settings = settings
        self._clients: Dict[str, Any] = {}
        self.last_error = ""

    @property
    def available(self) -> bool:
        return bool(self.connection_string and TableClient is not None)

    def _client(self, table_name: str):
        if not self.available:
            raise RuntimeError("AzureWebJobsStorage is missing or azure-data-tables is unavailable")
        if table_name not in self._clients:
            self._clients[table_name] = create_table_client(self.connection_string, table_name)
        return self._clients[table_name]

    def _handle_error(self, operation: str, exc: Exception) -> None:
        self.last_error = f"{operation}: {exc}"
        if self.fail_soft:
            logger.warning("chatbot operational storage %s failed: %s", operation, exc)
            return
        raise exc

    def get_user_profile(self, app_user_id: str) -> ChatbotUserProfile:
        fallback_tz = _valid_timezone(self.settings.default_timezone, "Asia/Seoul")
        if not app_user_id:
            return ChatbotUserProfile(app_user_id="", timezone_iana=fallback_tz)
        try:
            entity = self._client(self.settings.user_profile_table).get_entity(
                partition_key="user",
                row_key=str(app_user_id),
            )
            timezone = _valid_timezone(str(entity.get("TimezoneIana") or ""), fallback_tz)
            default_days = int(entity.get("DefaultTrendDays") or self.settings.default_trend_days)
            return ChatbotUserProfile(
                app_user_id=str(entity.get("AppUserId") or app_user_id),
                timezone_iana=timezone,
                locale=str(entity.get("Locale") or "en-US"),
                study_site_code=str(entity.get("StudySiteCode") or ""),
                country_code=str(entity.get("CountryCode") or ""),
                timezone_source=str(entity.get("TimezoneSource") or "profile"),
                default_trend_days=max(1, default_days),
                system_steps_alert_enabled=_env_bool(entity.get("SystemStepsAlertEnabled"), True),
                quiet_hours_start_local=str(entity.get("QuietHoursStartLocal") or ""),
                quiet_hours_end_local=str(entity.get("QuietHoursEndLocal") or ""),
                **_summary_fields_from_entity(entity),
                raw=dict(entity),
            )
        except ResourceNotFoundError:
            logger.warning("ChatbotUserProfile row is missing for AppUserId=%s", app_user_id)
            return ChatbotUserProfile(app_user_id=str(app_user_id), timezone_iana=fallback_tz)
        except Exception as exc:
            self._handle_error("get_user_profile", exc)
            return ChatbotUserProfile(app_user_id=str(app_user_id), timezone_iana=fallback_tz)

    def save_user_timezone(
        self,
        app_user_id: str,
        timezone_iana: str,
        *,
        source: str = "user",
        study_site_code: str = "",
    ) -> bool:
        """MERGE-upsert the explicit timezone onto the ChatbotUserProfile row.

        Only the timezone-related fields are written, so any other profile
        properties (locale, alert preferences, etc.) are preserved. The table is
        expected to pre-exist; this never creates it.
        """
        if not app_user_id or not timezone_iana:
            return False
        entity = {
            "PartitionKey": "user",
            "RowKey": str(app_user_id),
            "AppUserId": str(app_user_id),
            "TimezoneIana": str(timezone_iana),
            "TimezoneSource": str(source or "user"),
            "UpdatedAtUtc": utc_now(),
        }
        if study_site_code:
            entity["StudySiteCode"] = str(study_site_code)
        try:
            self._client(self.settings.user_profile_table).upsert_entity(
                mode=UpdateMode.MERGE,
                entity=entity,
            )
            return True
        except Exception as exc:
            self._handle_error("save_user_timezone", exc)
            return False

    SUMMARY_TIMES_MAX = 5

    def set_summary_times(self, app_user_id: str, times: List[Tuple[int, int]]) -> bool:
        """MERGE-upsert the full daily-summary schedule onto the ChatbotUserProfile
        row. ``times`` is a list of (hour, minute); empty clears the schedule.
        SummaryReportHour/Minute are kept as the first time (or -1/0) so legacy
        readers stay consistent. Only summary fields are written."""
        if not app_user_id:
            return False
        norm = sorted({
            _round_to_5(int(h), int(m))
            for (h, m) in (times or [])
            if 0 <= int(h) <= 23
        })
        entity = {
            "PartitionKey": "user",
            "RowKey": str(app_user_id),
            "AppUserId": str(app_user_id),
            "SummaryReportTimes": _format_summary_times(norm),
            "SummaryReportHour": (norm[0][0] if norm else -1),
            "SummaryReportMinute": (norm[0][1] if norm else 0),
            "UpdatedAtUtc": utc_now(),
        }
        try:
            self._client(self.settings.user_profile_table).upsert_entity(
                mode=UpdateMode.MERGE,
                entity=entity,
            )
            return True
        except Exception as exc:
            self._handle_error("set_summary_times", exc)
            return False

    def save_user_summary_hour(self, app_user_id: str, hour: Optional[int], minute: int = 0) -> bool:
        """Set the schedule to a SINGLE time (replace), or clear it when ``hour``
        is None. Used by onboarding and settings reset."""
        if hour is None:
            return self.set_summary_times(app_user_id, [])
        return self.set_summary_times(app_user_id, [(int(hour), max(0, min(59, int(minute or 0))))])

    def add_summary_time(
        self, app_user_id: str, hour: int, minute: int = 0, max_count: int = SUMMARY_TIMES_MAX
    ) -> Dict[str, Any]:
        """Add one (hour, minute) to the schedule (keeping existing). Returns
        {ok, reason, times}; reason is added|duplicate|limit|error|no_user."""
        if not app_user_id:
            return {"ok": False, "reason": "no_user", "times": []}
        current = self.get_user_profile(app_user_id).summary_report_times
        slot = _round_to_5(int(hour), int(minute or 0))
        if slot in current:
            return {"ok": False, "reason": "duplicate", "times": current}
        if len(current) >= max_count:
            return {"ok": False, "reason": "limit", "times": current}
        new_times = sorted(set(current) | {slot})
        if self.set_summary_times(app_user_id, new_times):
            return {"ok": True, "reason": "added", "times": new_times}
        return {"ok": False, "reason": "error", "times": current}

    def remove_summary_time(self, app_user_id: str, hour: int, minute: int = 0) -> Dict[str, Any]:
        """Remove one (hour, minute) from the schedule. Returns {removed, times}."""
        if not app_user_id:
            return {"removed": False, "times": []}
        current = self.get_user_profile(app_user_id).summary_report_times
        slot = _round_to_5(int(hour), int(minute or 0))
        if slot not in current:
            return {"removed": False, "times": current}
        new_times = [t for t in current if t != slot]
        if self.set_summary_times(app_user_id, new_times):
            return {"removed": True, "times": new_times}
        return {"removed": False, "times": current}

    def clear_summary_times(self, app_user_id: str) -> bool:
        """Remove all scheduled summary times."""
        return self.set_summary_times(app_user_id, [])

    def get_conversation_state(self, app_user_id: str, thread_id: str) -> ChatbotConversationState:
        safe_thread_id = str(thread_id or "local")
        if not app_user_id:
            return ChatbotConversationState(app_user_id="", thread_id=safe_thread_id)
        try:
            entity = self._client(self.settings.conversation_state_table).get_entity(
                partition_key=f"user:{app_user_id}",
                row_key=safe_thread_id,
            )
            return ChatbotConversationState(
                app_user_id=str(entity.get("AppUserId") or app_user_id),
                thread_id=str(entity.get("ThreadId") or safe_thread_id),
                recent_turns=_json_loads(entity.get("RecentTurnsJson"), []),
                rolling_summary=str(entity.get("RollingSummary") or ""),
                active_context=_json_loads(entity.get("ActiveContextJson"), {}),
                turn_count=int(entity.get("TurnCount") or 0),
                state_version=int(entity.get("StateVersion") or 0),
                last_user_message_utc=entity.get("LastUserMessageUtc"),
                last_assistant_message_utc=entity.get("LastAssistantMessageUtc"),
                latest_response_id=str(entity.get("LatestResponseId") or ""),
                response_chain_started_at_utc=entity.get("ResponseChainStartedAtUtc"),
                active_episode_id=str(entity.get("ActiveEpisodeId") or ""),
                active_episode_type=str(entity.get("ActiveEpisodeType") or "reactive"),
                suspended_episodes=_json_loads(entity.get("SuspendedEpisodesJson"), []),
                fallback_summary=str(entity.get("FallbackSummary") or ""),
                pending_clarification=_json_loads(entity.get("PendingClarificationJson"), {}),
            )
        except ResourceNotFoundError:
            return ChatbotConversationState(app_user_id=str(app_user_id), thread_id=safe_thread_id)
        except Exception as exc:
            self._handle_error("get_conversation_state", exc)
            return ChatbotConversationState(app_user_id=str(app_user_id), thread_id=safe_thread_id)

    def save_conversation_state(self, state: ChatbotConversationState) -> bool:
        if not state.app_user_id:
            return False
        settings = self.settings
        recent_turns = list(state.recent_turns or [])[-settings.recent_turn_limit :]
        next_version = int(state.state_version or 0) + 1
        entity = {
            "PartitionKey": f"user:{state.app_user_id}",
            "RowKey": str(state.thread_id or "local"),
            "AppUserId": str(state.app_user_id),
            "ThreadId": str(state.thread_id or "local"),
            "RecentTurnsJson": _json_dumps(recent_turns),
            "RollingSummary": str(state.rolling_summary or "")[:4000],
            "ActiveContextJson": _json_dumps(state.active_context or {}),
            "LastUserMessageUtc": state.last_user_message_utc or utc_now(),
            "LastAssistantMessageUtc": state.last_assistant_message_utc or utc_now(),
            "TurnCount": int(state.turn_count or len(recent_turns)),
            "StateVersion": next_version,
            "LatestResponseId": str(state.latest_response_id or ""),
            "ActiveEpisodeId": str(state.active_episode_id or ""),
            "ActiveEpisodeType": str(state.active_episode_type or "reactive"),
            "SuspendedEpisodesJson": _json_dumps(
                list(state.suspended_episodes or [])[-int(settings.suspended_episode_limit or 3):]
            ),
            "FallbackSummary": str(state.fallback_summary or "")[: int(settings.fallback_summary_max_chars or 2000)],
            "PendingClarificationJson": _json_dumps(state.pending_clarification or {}),
            "UpdatedAtUtc": utc_now(),
        }
        if state.response_chain_started_at_utc is not None:
            entity["ResponseChainStartedAtUtc"] = state.response_chain_started_at_utc
        try:
            self._client(settings.conversation_state_table).upsert_entity(
                mode=UpdateMode.REPLACE,
                entity=entity,
            )
            state.state_version = next_version
            state.recent_turns = recent_turns
            return True
        except Exception as exc:
            self._handle_error("save_conversation_state", exc)
            return False

    def list_metric_availability(self, app_user_id: str) -> List[Dict[str, Any]]:
        if not app_user_id:
            return []
        try:
            escaped = str(app_user_id).replace("'", "''")
            entities = self._client(self.settings.metric_availability_table).query_entities(
                query_filter=f"PartitionKey eq 'user:{escaped}'"
            )
            return [dict(entity) for entity in entities]
        except Exception as exc:
            self._handle_error("list_metric_availability", exc)
            return []

    def get_metric_availability_map(self, app_user_id: str) -> Dict[str, MetricAvailability]:
        rows = self.list_metric_availability(app_user_id)
        result: Dict[str, MetricAvailability] = {}
        for row in rows:
            metric_type = str(row.get("MetricType") or row.get("RowKey") or "").replace("metric:", "")
            if not metric_type:
                continue
            result[metric_type.upper()] = MetricAvailability(
                app_user_id=str(row.get("AppUserId") or app_user_id),
                metric_type=metric_type.upper(),
                is_available=_env_bool(row.get("IsAvailable"), False),
                preferred_source_table=str(row.get("PreferredSourceTable") or ""),
                preferred_source_policy=str(row.get("PreferredSourcePolicy") or "unified_flat_policy"),
                earliest_date_from_local=row.get("EarliestDateFromLocal"),
                latest_date_from_local=row.get("LatestDateFromLocal"),
                latest_date_to_local=row.get("LatestDateToLocal"),
                latest_ingestion_time_utc=row.get("LatestIngestionTimeUtc"),
                last_collected_time_local=row.get("LastCollectedTimeLocal"),
                freshness_status=str(row.get("FreshnessStatus") or "unknown"),
                refreshed_at_utc=row.get("RefreshedAtUtc"),
                raw=row,
            )
        return result

    def save_metric_availability_rows(self, app_user_id: str, rows: List[MetricAvailability]) -> int:
        if not app_user_id or not rows:
            return 0
        saved = 0
        refreshed_at = utc_now()
        try:
            client = self._client(self.settings.metric_availability_table)
            for item in rows:
                metric_type = str(item.metric_type or "").upper()
                if not metric_type:
                    continue
                entity = {
                    "PartitionKey": f"user:{app_user_id}",
                    "RowKey": f"metric:{metric_type}",
                    "AppUserId": str(app_user_id),
                    "MetricType": metric_type,
                    "IsAvailable": bool(item.is_available),
                    "PreferredSourceTable": str(item.preferred_source_table or ""),
                    "PreferredSourcePolicy": str(item.preferred_source_policy or "unified_flat_policy"),
                    "EarliestDateFromLocal": item.earliest_date_from_local,
                    "LatestDateFromLocal": item.latest_date_from_local,
                    "LatestDateToLocal": item.latest_date_to_local,
                    "LatestIngestionTimeUtc": item.latest_ingestion_time_utc,
                    "LastCollectedTimeLocal": item.last_collected_time_local,
                    "FreshnessStatus": str(item.freshness_status or "unknown"),
                    "RefreshedAtUtc": refreshed_at,
                }
                client.upsert_entity(mode=UpdateMode.MERGE, entity=entity)
                saved += 1
            return saved
        except Exception as exc:
            self._handle_error("save_metric_availability_rows", exc)
            return saved

    def get_trigger_state(self, app_user_id: str, local_date: str) -> DailyTriggerState:
        if not app_user_id or not local_date:
            return DailyTriggerState(app_user_id=str(app_user_id or ""), local_date=str(local_date or ""))
        try:
            entity = self._client(self.settings.trigger_state_table).get_entity(
                partition_key=f"user:{app_user_id}",
                row_key=f"steps_daily:{local_date}",
            )
            status = str(entity.get("EvaluationStatus") or entity.get("Status") or "new")
            return DailyTriggerState(
                app_user_id=str(entity.get("AppUserId") or app_user_id),
                local_date=str(entity.get("TriggerDateLocal") or entity.get("LocalDate") or local_date),
                status=status,
                timezone_iana=str(entity.get("TimezoneIana") or ""),
                first_post_08_step_detected_at_local=str(entity.get("FirstPost08StepDetectedAtLocal") or ""),
                evaluation_target_date_local=str(entity.get("EvaluationTargetDateLocal") or ""),
                evaluation_status=status,
                alert_candidate_count=int(entity.get("AlertCandidateCount") or 0),
                notification_sent=_env_bool(entity.get("NotificationSent"), False),
                retry_count=int(entity.get("RetryCount") or entity.get("DetectorAttemptCount") or 0),
                last_error=str(entity.get("LastError") or ""),
                first_event_utc=entity.get("FirstEventUtc"),
                last_event_utc=entity.get("LastEventUtc"),
                detector_attempt_count=int(entity.get("DetectorAttemptCount") or entity.get("RetryCount") or 0),
                last_candidate_id=str(entity.get("LastCandidateId") or ""),
                last_notification_utc=entity.get("LastNotificationUtc"),
                raw=dict(entity),
            )
        except ResourceNotFoundError:
            return DailyTriggerState(app_user_id=str(app_user_id), local_date=str(local_date))
        except Exception as exc:
            self._handle_error("get_trigger_state", exc)
            return DailyTriggerState(app_user_id=str(app_user_id), local_date=str(local_date))

    def save_trigger_state(self, state: DailyTriggerState) -> bool:
        if not state.app_user_id or not state.local_date:
            return False
        entity = {
            "PartitionKey": f"user:{state.app_user_id}",
            "RowKey": f"steps_daily:{state.local_date}",
            "AppUserId": state.app_user_id,
            "LocalDate": state.local_date,
            "TriggerDateLocal": state.local_date,
            "TimezoneIana": state.timezone_iana,
            "FirstPost08StepDetectedAtLocal": state.first_post_08_step_detected_at_local,
            "EvaluationTargetDateLocal": state.evaluation_target_date_local,
            "EvaluationStatus": state.evaluation_status or state.status,
            "AlertCandidateCount": int(state.alert_candidate_count or 0),
            "NotificationSent": bool(state.notification_sent),
            "RetryCount": int(state.retry_count or state.detector_attempt_count or 0),
            "LastError": state.last_error,
            "Status": state.status,
            "FirstEventUtc": state.first_event_utc,
            "LastEventUtc": state.last_event_utc or utc_now(),
            "DetectorAttemptCount": int(state.detector_attempt_count or 0),
            "LastCandidateId": state.last_candidate_id,
            "LastNotificationUtc": state.last_notification_utc,
            "UpdatedAtUtc": utc_now(),
        }
        try:
            self._client(self.settings.trigger_state_table).upsert_entity(
                mode=UpdateMode.MERGE,
                entity=entity,
            )
            return True
        except Exception as exc:
            self._handle_error("save_trigger_state", exc)
            return False

    # ------------------------------------------------------------------
    # User-configured threshold alerts (ChatbotUserAlertThreshold)
    # ------------------------------------------------------------------
    def _parse_threshold_entity(self, entity: Dict[str, Any], app_user_id: str) -> "MetricAlertThreshold":
        metric_type = str(entity.get("MetricType") or entity.get("RowKey") or "").replace("metric:", "").upper()

        def _num(value):
            try:
                return None if value is None or value == "" else float(value)
            except Exception:
                return None

        return MetricAlertThreshold(
            app_user_id=str(entity.get("AppUserId") or app_user_id),
            metric_type=metric_type,
            lower=_num(entity.get("LowerThreshold")),
            upper=_num(entity.get("UpperThreshold")),
            sustain_minutes=int(entity.get("SustainMinutes") or 10),
            enabled=_env_bool(entity.get("Enabled"), True),
            last_alert_at_utc=entity.get("LastAlertAtUtc"),
            updated_at_utc=entity.get("UpdatedAtUtc"),
            raw=dict(entity),
        )

    def list_alert_thresholds(self, app_user_id: str) -> List["MetricAlertThreshold"]:
        if not app_user_id:
            return []
        try:
            escaped = str(app_user_id).replace("'", "''")
            entities = self._client(self.settings.alert_threshold_table).query_entities(
                query_filter=f"PartitionKey eq 'user:{escaped}'"
            )
            return [self._parse_threshold_entity(dict(e), app_user_id) for e in entities]
        except Exception as exc:
            self._handle_error("list_alert_thresholds", exc)
            return []

    def save_alert_threshold(self, threshold: "MetricAlertThreshold") -> bool:
        if not threshold.app_user_id or not threshold.metric_type:
            return False
        metric_type = str(threshold.metric_type).upper()
        entity = {
            "PartitionKey": f"user:{threshold.app_user_id}",
            "RowKey": f"metric:{metric_type}",
            "AppUserId": str(threshold.app_user_id),
            "MetricType": metric_type,
            "LowerThreshold": threshold.lower,
            "UpperThreshold": threshold.upper,
            "SustainMinutes": int(threshold.sustain_minutes or 10),
            "Enabled": bool(threshold.enabled),
            "LastAlertAtUtc": threshold.last_alert_at_utc,
            "UpdatedAtUtc": utc_now(),
        }
        try:
            self._client(self.settings.alert_threshold_table).upsert_entity(
                mode=UpdateMode.REPLACE, entity=entity
            )
            return True
        except Exception as exc:
            self._handle_error("save_alert_threshold", exc)
            return False

    def delete_alert_threshold(self, app_user_id: str, metric_type: str) -> bool:
        if not app_user_id or not metric_type:
            return False
        try:
            self._client(self.settings.alert_threshold_table).delete_entity(
                partition_key=f"user:{app_user_id}",
                row_key=f"metric:{str(metric_type).upper()}",
            )
            return True
        except ResourceNotFoundError:
            return True
        except Exception as exc:
            self._handle_error("delete_alert_threshold", exc)
            return False

    def daily_report_already_sent(self, app_user_id: str, local_date: str) -> bool:
        if not app_user_id or not local_date:
            return False
        try:
            entity = self._client(self.settings.trigger_state_table).get_entity(
                partition_key=f"user:{app_user_id}", row_key=f"daily_report:{local_date}"
            )
            return _env_bool(entity.get("Sent"), False)
        except ResourceNotFoundError:
            return False
        except Exception as exc:
            self._handle_error("daily_report_already_sent", exc)
            return False

    def mark_daily_report_sent(self, app_user_id: str, local_date: str) -> bool:
        if not app_user_id or not local_date:
            return False
        try:
            self._client(self.settings.trigger_state_table).upsert_entity(
                mode=UpdateMode.MERGE,
                entity={
                    "PartitionKey": f"user:{app_user_id}",
                    "RowKey": f"daily_report:{local_date}",
                    "AppUserId": str(app_user_id),
                    "LocalDate": str(local_date),
                    "Sent": True,
                    "UpdatedAtUtc": utc_now(),
                },
            )
            return True
        except Exception as exc:
            self._handle_error("mark_daily_report_sent", exc)
            return False

    def list_users_with_alert_thresholds(self, limit: int = 5000) -> List[str]:
        """Distinct AppUserIds that have at least one threshold row (for the timer)."""
        try:
            entities = self._client(self.settings.alert_threshold_table).query_entities(
                query_filter="Enabled eq true"
            )
            seen: List[str] = []
            uniq = set()
            for entity in entities:
                uid = str(entity.get("AppUserId") or "").strip()
                if uid and uid not in uniq:
                    uniq.add(uid)
                    seen.append(uid)
                    if len(seen) >= max(1, int(limit or 5000)):
                        break
            return seen
        except Exception as exc:
            self._handle_error("list_users_with_alert_thresholds", exc)
            return []

    def list_active_user_profiles(self, limit: int = 5000) -> List[ChatbotUserProfile]:
        try:
            entities = self._client(self.settings.user_profile_table).query_entities(
                query_filter="PartitionKey eq 'user'"
            )
            profiles = []
            for entity in entities:
                if len(profiles) >= max(1, int(limit or 5000)):
                    break
                app_user_id = str(entity.get("AppUserId") or entity.get("RowKey") or "")
                if not app_user_id:
                    continue
                timezone = _valid_timezone(
                    str(entity.get("TimezoneIana") or ""), self.settings.default_timezone
                )
                profiles.append(
                    ChatbotUserProfile(
                        app_user_id=app_user_id,
                        timezone_iana=timezone,
                        locale=str(entity.get("Locale") or "en-US"),
                        study_site_code=str(entity.get("StudySiteCode") or ""),
                        country_code=str(entity.get("CountryCode") or ""),
                        timezone_source=str(entity.get("TimezoneSource") or "profile"),
                        default_trend_days=int(entity.get("DefaultTrendDays") or self.settings.default_trend_days),
                        system_steps_alert_enabled=_env_bool(entity.get("SystemStepsAlertEnabled"), True),
                        **_summary_fields_from_entity(entity),
                        quiet_hours_start_local=str(entity.get("QuietHoursStartLocal") or ""),
                        quiet_hours_end_local=str(entity.get("QuietHoursEndLocal") or ""),
                        raw=dict(entity),
                    )
                )
            return profiles
        except Exception as exc:
            self._handle_error("list_active_user_profiles", exc)
            return []


def get_user_profile(app_user_id: str) -> ChatbotUserProfile:
    return OperationalTableRepository().get_user_profile(app_user_id)
