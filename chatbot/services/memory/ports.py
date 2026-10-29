"""Operational memory/storage interface consumed by Sensor Analytics."""

from typing import Any, Mapping, Protocol


class OperationalStoragePort(Protocol):
    last_error: str

    def get_user_profile(self, app_user_id: str) -> Any: ...

    def save_user_timezone(
        self,
        app_user_id: str,
        timezone_iana: str,
        *,
        source: str = "user",
        study_site_code: str = "",
    ) -> bool: ...

    def get_conversation_state(self, app_user_id: str, thread_id: str) -> Any: ...

    def get_metric_availability_map(self, app_user_id: str) -> Mapping[str, Any]: ...

    def save_metric_availability_rows(self, app_user_id: str, rows: list[Any]) -> int: ...

    def save_conversation_state(self, state: Any) -> bool: ...

    def add_summary_time(
        self,
        app_user_id: str,
        hour: int,
        minute: int = 0,
        max_count: int = 5,
    ) -> Mapping[str, Any]: ...

    def remove_summary_time(
        self,
        app_user_id: str,
        hour: int,
        minute: int = 0,
    ) -> Mapping[str, Any]: ...

    def clear_summary_times(self, app_user_id: str) -> bool: ...

    def list_alert_thresholds(self, app_user_id: str) -> list[Any]: ...

    def save_alert_threshold(self, threshold: Any) -> bool: ...

    def delete_alert_threshold(self, app_user_id: str, metric_type: str) -> bool: ...

    def get_trigger_state(self, app_user_id: str, local_date: str) -> Any: ...

    def save_trigger_state(self, state: Any) -> bool: ...

    def list_active_user_profiles(self, limit: int = 5000) -> list[Any]: ...


class RegisteredUserProfilePort(Protocol):
    """Read the existing onboarding profile from the Azure ``Users`` table."""

    def get_registered_user(self, app_user_id: str) -> Mapping[str, Any]: ...


__all__ = ["OperationalStoragePort", "RegisteredUserProfilePort"]
