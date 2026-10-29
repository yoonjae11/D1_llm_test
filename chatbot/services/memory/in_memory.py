"""In-memory operational storage for local conversations; no cloud clients."""

from copy import deepcopy
from zoneinfo import ZoneInfo

from chatbot.operational_storage import (
    ChatbotConversationState, ChatbotUserProfile, DailyTriggerState,
)


class InMemoryOperationalStorage:
    """Keep profiles, conversation state and refreshed inventory in one process."""

    last_error = ""

    def __init__(self, app_user_id: str, timezone_iana: str = "Asia/Seoul"):
        ZoneInfo(timezone_iana)
        self.timezone_iana = timezone_iana
        self.profiles = {}
        self.conversations = {}
        self.availability = {}
        self.thresholds = {}
        self.triggers = {}
        self.get_user_profile(app_user_id)

    def get_user_profile(self, app_user_id):
        if app_user_id not in self.profiles:
            self.profiles[app_user_id] = ChatbotUserProfile(
                app_user_id=app_user_id, timezone_iana=self.timezone_iana,
                locale="en", timezone_source="local_test",
                raw={"TimezoneIana": self.timezone_iana, "Locale": "en"},
            )
        return deepcopy(self.profiles[app_user_id])

    def get_registered_user(self, app_user_id):
        profile = self.get_user_profile(app_user_id)
        return {"AppUserId": app_user_id, **profile.raw}

    def save_user_timezone(self, app_user_id, timezone_iana, *, source="user", study_site_code=""):
        ZoneInfo(timezone_iana)
        profile = self.get_user_profile(app_user_id)
        profile.timezone_iana, profile.timezone_source = timezone_iana, source
        profile.study_site_code = study_site_code
        profile.raw["TimezoneIana"] = timezone_iana
        self.profiles[app_user_id] = profile
        return True

    def get_conversation_state(self, app_user_id, thread_id):
        return deepcopy(self.conversations.get(
            (app_user_id, thread_id),
            ChatbotConversationState(app_user_id=app_user_id, thread_id=thread_id),
        ))

    def save_conversation_state(self, state):
        if not state.app_user_id:
            return False
        self.conversations[(state.app_user_id, state.thread_id)] = deepcopy(state)
        return True

    def get_metric_availability_map(self, app_user_id):
        return deepcopy(self.availability.get(app_user_id, {}))

    def save_metric_availability_rows(self, app_user_id, rows):
        items = self.availability.setdefault(app_user_id, {})
        for row in rows:
            items[row.metric_type] = deepcopy(row)
        return len(rows)

    def add_summary_time(self, app_user_id, hour, minute=0, max_count=5):
        profile = self.get_user_profile(app_user_id)
        slot = (int(hour), int(minute))
        times = profile.summary_report_times
        if slot in times:
            return {"ok": False, "reason": "duplicate", "times": times}
        if len(times) >= max_count:
            return {"ok": False, "reason": "limit", "times": times}
        profile.summary_report_times = sorted([*times, slot])
        profile.summary_report_hour, profile.summary_report_minute = profile.summary_report_times[0]
        self.profiles[app_user_id] = profile
        return {"ok": True, "reason": "added", "times": profile.summary_report_times}

    def remove_summary_time(self, app_user_id, hour, minute=0):
        profile = self.get_user_profile(app_user_id)
        slot = (int(hour), int(minute))
        removed = slot in profile.summary_report_times
        profile.summary_report_times = [item for item in profile.summary_report_times if item != slot]
        if profile.summary_report_times:
            profile.summary_report_hour, profile.summary_report_minute = profile.summary_report_times[0]
        else:
            profile.summary_report_hour, profile.summary_report_minute = -1, 0
        self.profiles[app_user_id] = profile
        return {"removed": removed, "times": profile.summary_report_times}

    def clear_summary_times(self, app_user_id):
        profile = self.get_user_profile(app_user_id)
        profile.summary_report_times = []
        profile.summary_report_hour, profile.summary_report_minute = -1, 0
        self.profiles[app_user_id] = profile
        return True

    def list_alert_thresholds(self, app_user_id):
        return deepcopy(list(self.thresholds.get(app_user_id, {}).values()))

    def save_alert_threshold(self, threshold):
        self.thresholds.setdefault(threshold.app_user_id, {})[threshold.metric_type] = deepcopy(threshold)
        return True

    def delete_alert_threshold(self, app_user_id, metric_type):
        self.thresholds.get(app_user_id, {}).pop(metric_type, None)
        return True

    def get_trigger_state(self, app_user_id, local_date):
        return deepcopy(self.triggers.get(
            (app_user_id, local_date), DailyTriggerState(app_user_id=app_user_id, local_date=local_date),
        ))

    def save_trigger_state(self, state):
        self.triggers[(state.app_user_id, state.local_date)] = deepcopy(state)
        return True

    def list_active_user_profiles(self, limit=5000):
        return deepcopy(list(self.profiles.values())[:limit])
