"""Runtime configuration for the direct ADX chatbot."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from runtime_environment import (
    env_bool as _env_bool,
    env_int as _env_int,
    env_json as _env_json,
    load_local_environment,
)


load_local_environment()


_KQL_GENERATE_METHODS = frozenset({"deterministic", "llm"})


def normalize_kql_generate_method(value: object) -> str:
    """Normalize and validate an explicit KQL generation method."""

    method = str(value).strip().lower()
    if method not in _KQL_GENERATE_METHODS:
        allowed = ", ".join(sorted(_KQL_GENERATE_METHODS))
        raise ValueError(
            f"Invalid KQL_GENERATE_METHOD {method!r}; expected one of: {allowed}."
        )
    return method


def _kql_generate_method() -> str:
    """Return the explicit KQL generation strategy or fail at configuration load."""

    return normalize_kql_generate_method(
        os.environ.get("KQL_GENERATE_METHOD", "llm")
    )


@dataclass(frozen=True)
class ChatbotSettings:
    ai_provider: str = field(
        default_factory=lambda: os.environ.get("AI_PROVIDER", "openai").strip().lower()
    )

    openai_api_key: str = field(default_factory=lambda: os.environ.get("OPENAI_API_KEY", ""))
    openai_chat_model: str = field(
        default_factory=lambda: os.environ.get("OPENAI_CHAT_MODEL", "gpt-5")
    )
    openai_timeout_seconds: int = field(
        # gpt-5 reasoning calls on large prompts (factor_analysis/prescription)
        # regularly exceed 35s; 120s avoids spurious "Request timed out" errors.
        default_factory=lambda: _env_int("OPENAI_TIMEOUT_SECONDS", 120)
    )

    azure_openai_base_url: str = field(
        default_factory=lambda: os.environ.get("AZURE_OPENAI_BASE_URL", "")
    )
    azure_openai_default_deployment: str = field(
        default_factory=lambda: (
            os.environ.get("AZURE_OPENAI_DEFAULT_DEPLOYMENT")
            or os.environ.get("AZURE_OPENAI_CHAT_DEPLOYMENT", "")
        )
    )
    azure_openai_auth_mode: str = field(
        default_factory=lambda: os.environ.get("AZURE_OPENAI_AUTH_MODE", "api_key").strip().lower()
    )
    azure_openai_api_key: str = field(
        default_factory=lambda: os.environ.get("AZURE_OPENAI_API_KEY", "")
    )

    chatbot_debug: bool = field(default_factory=lambda: _env_bool("CHATBOT_DEBUG", False))
    max_result_rows: int = field(default_factory=lambda: _env_int("CHATBOT_MAX_RESULT_ROWS", 40))
    max_kql_repair_attempts: int = field(
        default_factory=lambda: _env_int("CHATBOT_MAX_KQL_REPAIR_ATTEMPTS", 2)
    )
    default_timezone: str = field(
        default_factory=lambda: os.environ.get("CHATBOT_DEFAULT_TIMEZONE", "Asia/Seoul")
    )
    supported_site_timezones: dict[str, str] = field(
        default_factory=lambda: _env_json(
            "CHATBOT_SUPPORTED_SITE_TIMEZONES_JSON",
            {
                "KR_SEOUL": "Asia/Seoul",
                "UK_LONDON": "Europe/London",
                "ES_MAINLAND": "Europe/Madrid",
                "ES_CANARY": "Atlantic/Canary",
                "ES_CEUTA": "Africa/Ceuta",
            },
        )
    )
    strict_timezone_onboarding: bool = field(
        default_factory=lambda: _env_bool("CHATBOT_STRICT_TIMEZONE_ONBOARDING", False)
    )
    # Phase 3: Azure OpenAI Responses API multi-turn + episode interrupt/resume.
    # Default off preserves the existing chat.completions behavior.
    use_azure_responses_api: bool = field(
        default_factory=lambda: _env_bool("CHATBOT_USE_AZURE_RESPONSES_API", False)
    )
    response_store: bool = field(
        default_factory=lambda: _env_bool("CHATBOT_RESPONSE_STORE", True)
    )
    response_chain_max_age_days: int = field(
        default_factory=lambda: _env_int("CHATBOT_RESPONSE_CHAIN_MAX_AGE_DAYS", 25)
    )
    suspended_episode_limit: int = field(
        default_factory=lambda: _env_int("CHATBOT_SUSPENDED_EPISODE_LIMIT", 3)
    )
    fallback_summary_max_chars: int = field(
        default_factory=lambda: _env_int("CHATBOT_FALLBACK_SUMMARY_MAX_CHARS", 2000)
    )
    proactive_episode_interrupt_enabled: bool = field(
        # Default ON: a proactive alert is recorded as the opening assistant turn
        # of a system-initiated episode so the user's reply continues it through
        # the same LangGraph. Set the env to "false" to send alerts without
        # interrupting the user's active conversation.
        default_factory=lambda: _env_bool("CHATBOT_PROACTIVE_EPISODE_INTERRUPT_ENABLED", True)
    )
    conversation_state_table: str = field(
        default_factory=lambda: os.environ.get(
            "CHATBOT_CONVERSATION_STATE_TABLE", "ChatbotConversationState"
        )
    )
    user_profile_table: str = field(
        default_factory=lambda: os.environ.get("CHATBOT_USER_PROFILE_TABLE", "ChatbotUserProfile")
    )
    metric_availability_table: str = field(
        default_factory=lambda: os.environ.get(
            "CHATBOT_METRIC_AVAILABILITY_TABLE", "ChatbotMetricAvailability"
        )
    )
    trigger_state_table: str = field(
        default_factory=lambda: os.environ.get(
            "SYSTEM_STEPS_TRIGGER_STATE_TABLE", "SystemInitiatedDailyTriggerState"
        )
    )
    # User-configured threshold alerts (HR/temperature/etc.) + daily report.
    alert_threshold_table: str = field(
        default_factory=lambda: os.environ.get(
            "CHATBOT_ALERT_THRESHOLD_TABLE", "ChatbotUserAlertThreshold"
        )
    )
    threshold_alert_enabled: bool = field(
        default_factory=lambda: _env_bool("CHATBOT_THRESHOLD_ALERT_ENABLED", True)
    )
    threshold_alert_sustain_minutes_default: int = field(
        default_factory=lambda: _env_int("CHATBOT_THRESHOLD_ALERT_SUSTAIN_MINUTES", 10)
    )
    threshold_alert_lookback_minutes: int = field(
        default_factory=lambda: _env_int("CHATBOT_THRESHOLD_ALERT_LOOKBACK_MINUTES", 60)
    )
    daily_report_enabled: bool = field(
        default_factory=lambda: _env_bool("CHATBOT_DAILY_REPORT_ENABLED", True)
    )
    kql_generate_method: str = field(default_factory=_kql_generate_method)
    generic_kql_fallback_enabled: bool = field(
        default_factory=lambda: _env_bool("CHATBOT_ENABLE_GENERIC_KQL_FALLBACK", False)
    )
    default_trend_days: int = field(
        default_factory=lambda: _env_int("CHATBOT_DEFAULT_TREND_DAYS", 14)
    )
    recent_turn_limit: int = field(
        default_factory=lambda: _env_int("CHATBOT_RECENT_TURN_LIMIT", 12)
    )
    system_steps_alert_enabled: bool = field(
        default_factory=lambda: _env_bool("SYSTEM_STEPS_ALERT_ENABLED", True)
    )
    system_steps_alert_retry_delays_seconds: list[int] = field(
        default_factory=lambda: _env_json(
            "SYSTEM_STEPS_ALERT_RETRY_DELAYS_SECONDS_JSON", [300, 900, 1800]
        )
    )
    system_steps_alert_trigger_hour_local: int = field(
        default_factory=lambda: _env_int("SYSTEM_STEPS_ALERT_TRIGGER_HOUR_LOCAL", 8)
    )
    system_steps_alert_earliest_evaluation_hour_local: int = field(
        default_factory=lambda: _env_int("SYSTEM_STEPS_ALERT_EARLIEST_EVALUATION_HOUR_LOCAL", 8)
    )
    system_steps_alert_earliest_evaluation_minute_local: int = field(
        default_factory=lambda: _env_int("SYSTEM_STEPS_ALERT_EARLIEST_EVALUATION_MINUTE_LOCAL", 30)
    )
    system_steps_alert_fallback_hour_local: int = field(
        default_factory=lambda: _env_int("SYSTEM_STEPS_ALERT_FALLBACK_HOUR_LOCAL", 11)
    )
    system_steps_alert_reconciliation_hour_local: int = field(
        default_factory=lambda: _env_int("SYSTEM_STEPS_ALERT_RECONCILIATION_HOUR_LOCAL", 14)
    )
    system_steps_baseline_days: int = field(
        default_factory=lambda: _env_int("SYSTEM_STEPS_BASELINE_DAYS", 14)
    )
    system_steps_baseline_min_observed_days: int = field(
        default_factory=lambda: _env_int("SYSTEM_STEPS_BASELINE_MIN_OBSERVED_DAYS", 10)
    )
    system_steps_baseline_min_coverage_ratio: float = field(
        default_factory=lambda: float(
            os.environ.get("SYSTEM_STEPS_BASELINE_MIN_COVERAGE_RATIO", "0.70")
        )
    )
    system_steps_baseline_rule_version: str = field(
        default_factory=lambda: os.environ.get(
            "SYSTEM_STEPS_BASELINE_RULE_VERSION", "steps_mad_v1"
        )
    )
    system_steps_alert_zmad_threshold: float = field(
        default_factory=lambda: float(os.environ.get("SYSTEM_STEPS_ALERT_ZMAD_THRESHOLD", "-3.5"))
    )
    adx_alert_baseline_table: str = field(
        default_factory=lambda: os.environ.get(
            "ADX_ALERT_BASELINE_TABLE", "UserMetricMadBaselineSnapshot"
        )
    )
    adx_alert_baseline_mapping: str = field(
        default_factory=lambda: os.environ.get(
            "ADX_ALERT_BASELINE_MAPPING", "UserMetricMadBaselineSnapshotJsonMapping"
        )
    )
    adx_alert_candidate_table: str = field(
        default_factory=lambda: os.environ.get("ADX_ALERT_CANDIDATE_TABLE", "AlertCandidate")
    )
    adx_alert_candidate_mapping: str = field(
        default_factory=lambda: os.environ.get(
            "ADX_ALERT_CANDIDATE_MAPPING", "AlertCandidateJsonMapping"
        )
    )


def get_settings() -> ChatbotSettings:
    return ChatbotSettings()
