"""Canonical Azure Storage Queue names shared by producers and triggers.

Queue trigger bindings are deployment contracts, not runtime feature flags.
Keeping the names in one module prevents a producer from writing to a queue
that no Azure Function consumes.
"""

METRIC_AVAILABILITY_QUEUE = "chatbot-metric-availability-refresh"
SYSTEM_PHONE_INGEST_EVENT_QUEUE = "system-initiated-phone-ingest-events"
SYSTEM_STEPS_RETRY_QUEUE = "system-initiated-steps-daily-retry"
WITHINGS_WEBHOOK_QUEUE = "withings-webhook-events"

__all__ = [
    "METRIC_AVAILABILITY_QUEUE",
    "SYSTEM_PHONE_INGEST_EVENT_QUEUE",
    "SYSTEM_STEPS_RETRY_QUEUE",
    "WITHINGS_WEBHOOK_QUEUE",
]
