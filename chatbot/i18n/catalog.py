"""Deterministic user-facing message catalog.

LLM instructions belong in :mod:`chatbot.prompts`; literal application replies
belong here.  English is the fallback locale and every catalog entry must have
an English value. Legacy locale selectors intentionally resolve to the same
English text.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping


_MESSAGES: Dict[str, Dict[str, str]] = {
    "response.clarification": {
        "en": "Which area should I check: activity, sleep, or heart rate?",
        "ko": "Which area should I check: activity, sleep, or heart rate?",
    },
    "response.unsupported.coffee": {
        "en": (
            "I can't check how many cups of coffee you drank today from the currently connected data.\n\n"
            "The current system primarily checks smartphone health metrics such as steps, distance, "
            "calories, sleep, and heart rate.\n\n"
            "In general, both the amount and timing of caffeine can affect sleep."
        ),
        "ko": (
            "I can't check how many cups of coffee you drank today from the currently connected data.\n\n"
            "The current system primarily checks smartphone health metrics such as steps, distance, "
            "calories, sleep, and heart rate.\n\n"
            "In general, both the amount and timing of caffeine can affect sleep."
        ),
    },
    "response.unsupported.generic": {
        "en": (
            "I can't check that personal metric from the currently connected data.\n\n"
            "Ask again about a collected metric such as steps, activity, sleep, or heart rate, "
            "and I'll check the available data."
        ),
        "ko": (
            "I can't check that personal metric from the currently connected data.\n\n"
            "Ask again about a collected metric such as steps, activity, sleep, or heart rate, "
            "and I'll check the available data."
        ),
    },
    "response.error.tls": {
        "en": (
            "I couldn't connect to ADX because TLS certificate verification failed. "
            "Please check your trusted CA/certificate settings and try again."
        ),
        "ko": "I couldn't connect to ADX because TLS certificate verification failed. "
            "Please check your trusted CA/certificate settings and try again.",
    },
    "response.error.query": {
        "en": "I hit an error while querying your data. Please try again.",
        "ko": "I hit an error while querying your data. Please try again.",
    },
    "response.no_data.uncollected": {
        "en": (
            "{names_capitalized} is not currently collected in your synced phone health data, "
            "so I have no readings to report. If you start measuring it in your phone's health app "
            "and sync, I'll be able to check it."
        ),
        "ko": (
            "{names_capitalized} is not currently collected in your synced phone health data, "
            "so I have no readings to report. If you start measuring it in your phone's health app "
            "and sync, I'll be able to check it."
        ),
    },
    "response.no_data.period": {
        "en": (
            "I couldn't find records for that period. Missing records are not treated as zero. "
            "A different time range or metric may have available data."
        ),
        "ko": (
            "I couldn't find records for that period. Missing records are not treated as zero. "
            "A different time range or metric may have available data."
        ),
    },
    "response.no_data.unverified_daily": {
        "en": "I couldn't verify the daily activity result against the available flat records, so I can't give a reliable value yet.",
        "ko": "일별 활동 결과를 원본 기록과 대조하지 못해 지금은 신뢰할 수 있는 값을 안내할 수 없습니다.",
    },
    "capabilities.profile.not_set": {
        "en": "Not set yet: {labels}",
        "ko": "Not set yet: {labels}",
    },
    "capabilities.profile.empty": {
        "en": "I don't have your profile details yet.",
        "ko": "I don't have your profile details yet.",
    },
    "capabilities.profile.update_hint": {
        "en": "You can change these anytime with /update_info.",
        "ko": "You can change these anytime with /update_info.",
    },
    "capabilities.profile.field.name": {"en": "Name", "ko": "Name"},
    "capabilities.profile.field.age": {"en": "Age", "ko": "Age"},
    "capabilities.profile.field.gender": {"en": "Gender", "ko": "Gender"},
    "capabilities.inventory.item": {"en": "- {name}", "ko": "- {name}"},
    "capabilities.inventory.item_dated": {
        "en": "- {name} (through {date})",
        "ko": "- {name} (through {date})",
    },
    "capabilities.inventory.header": {
        "en": "Here is the health data currently collected from your connected sources:\n{lines}",
        "ko": "Here is the health data currently collected from your connected sources:\n{lines}",
    },
    "capabilities.inventory.connected_source": {
        "en": "connected-source health data",
        "ko": "connected-source health data",
    },
    "capabilities.inventory.tail": {
        "en": (
            "Ask about any of these — for example trends, daily values, or period comparisons. "
            "Items not listed are not currently collected in your synced data."
        ),
        "ko": "Ask about any of these — for example trends, daily values, or period comparisons. "
            "Items not listed are not currently collected in your synced data.",
    },
    "capabilities.timezone.current": {
        "en": (
            "Your timezone is currently set to {timezone}. All of your daily data is read against this timezone. "
            "You can change it anytime with /update_timezone."
        ),
        "ko": (
            "Your timezone is currently set to {timezone}. All of your daily data is read against this timezone. "
            "You can change it anytime with /update_timezone."
        ),
    },
    "capabilities.timezone.missing": {
        "en": (
            "Your timezone isn't set yet. Please set it with /update_timezone "
            "(for example, /update_timezone 1 for Asia/Seoul)."
        ),
        "ko": (
            "Your timezone isn't set yet. Please set it with /update_timezone "
            "(for example, /update_timezone 1 for Asia/Seoul)."
        ),
    },
    "alert.unsupported_metric": {
        "en": "I can't set an alert on '{unsupported}'. Alerts are available for: {names}.",
        "ko": "I can't set an alert on '{unsupported}'. Alerts are available for: {names}.",
    },
    "alert.ask.metric": {
        "en": "Which metric should I set the alert on? (available: {names})",
        "ko": "Which metric should I set the alert on? (available: {names})",
    },
    "alert.ask.bound": {
        "en": "At what {label} value should I alert you? e.g. 'above 120' or 'below 40'.",
        "ko": "At what {label} value should I alert you? e.g. 'above 120' or 'below 40'.",
    },
    "alert.ask.invalid_bounds": {
        "en": "The lower bound ({lower:g}) must be smaller than the upper bound ({upper:g}). Please tell me again.",
        "ko": "The lower bound ({lower:g}) must be smaller than the upper bound ({upper:g}). Please tell me again.",
    },
    "alert.ask.duration": {
        "en": "For how many minutes should {label} stay there before I alert you? (e.g. 10 minutes)",
        "ko": "For how many minutes should {label} stay there before I alert you? (e.g. 10 minutes)",
    },
    "alert.storage.unavailable": {
        "en": "I couldn't reach the alert storage. Please try again shortly.",
        "ko": "I couldn't reach the alert storage. Please try again shortly.",
    },
    "alert.save.failed": {
        "en": "Failed to save the alert (storage error). Please try again shortly.",
        "ko": "Failed to save the alert (storage error). Please try again shortly.",
    },
    "alert.duration.rounding_note": {
        "en": "\n(Your data syncs about hourly, so I rounded the duration to minutes; very short durations may not be detectable.)",
        "ko": "\n(Your data syncs about hourly, so I rounded the duration to minutes; very short durations may not be detectable.)",
    },
    "alert.save.header": {
        "en": "I've set your alert:\n",
        "ko": "I've set your alert:\n",
    },
    "alert.none": {"en": "none", "ko": "none"},
    "alert.summary.none": {
        "en": "No daily summary is scheduled.",
        "ko": "No daily summary is scheduled.",
    },
    "alert.summary.cleared_all": {
        "en": "I've turned off your daily summary.",
        "ko": "I've turned off your daily summary.",
    },
    "alert.summary.clear.ask": {
        "en": "Which one should I remove? You have {joined}. Tell me a time, or say 'all'.",
        "ko": "Which one should I remove? You have {joined}. Tell me a time, or say 'all'.",
    },
    "alert.summary.removed": {
        "en": "Removed the {hour:02d}:{minute:02d} summary. Remaining: {left}.",
        "ko": "Removed the {hour:02d}:{minute:02d} summary. Remaining: {left}.",
    },
    "alert.summary.not_found": {
        "en": "No summary set for {hour:02d}:{minute:02d}. Current: {left}.",
        "ko": "No summary set for {hour:02d}:{minute:02d}. Current: {left}.",
    },
    "alert.summary.list": {
        "en": "Your daily summary is scheduled for {joined} every day.",
        "ko": "Your daily summary is scheduled for {joined} every day.",
    },
    "alert.summary.ask_time": {
        "en": "What time should I send your daily summary? (e.g. 09:00, 17:20, 11pm)",
        "ko": "What time should I send your daily summary? (e.g. 09:00, 17:20, 11pm)",
    },
    "alert.summary.rounding_note": {
        "en": " (rounded to {hour:02d}:{minute:02d}, the nearest 5-minute mark)",
        "ko": " (rounded to {hour:02d}:{minute:02d}, the nearest 5-minute mark)",
    },
    "alert.summary.added": {
        "en": "Added a daily summary at {hour:02d}:{minute:02d}{rounding_note}. You now have: {times}.",
        "ko": "Added a daily summary at {hour:02d}:{minute:02d}{rounding_note}. You now have: {times}.",
    },
    "alert.summary.duplicate": {
        "en": "{hour:02d}:{minute:02d} is already set. You have: {times}.",
        "ko": "{hour:02d}:{minute:02d} is already set. You have: {times}.",
    },
    "alert.summary.limit": {
        "en": "You can have at most {maximum} summaries. Remove one first. Current: {times}.",
        "ko": "You can have at most {maximum} summaries. Remove one first. Current: {times}.",
    },
    "alert.summary.save_failed": {
        "en": "I couldn't save that (storage error). Please try again.",
        "ko": "I couldn't save that (storage error). Please try again.",
    },
    "alert.clear.all": {
        "en": "I've removed all your alerts.",
        "ko": "I've removed all your alerts.",
    },
    "alert.clear.unknown": {
        "en": "I'm not sure which alert to remove. Available: {names}.",
        "ko": "I'm not sure which alert to remove. Available: {names}.",
    },
    "alert.clear.metric": {
        "en": "I've removed your {label} alert.",
        "ko": "I've removed your {label} alert.",
    },
    "alert.list.summary_set": {
        "en": "Scheduled daily summary: every day at {joined}",
        "ko": "Scheduled daily summary: every day at {joined}",
    },
    "alert.list.summary_none": {
        "en": "Scheduled daily summary: not set",
        "ko": "Scheduled daily summary: not set",
    },
    "alert.list.header": {"en": "Your alerts:", "ko": "Your alerts:"},
    "alert.list.threshold_none": {
        "en": "You have no threshold alerts set.",
        "ko": "You have no threshold alerts set.",
    },
    "timezone.menu.intro": {
        "en": "To read your health data against the correct dates and times, I first need your region (timezone).",
        "ko": "To read your health data against the correct dates and times, I first need your region (timezone).",
    },
    "timezone.menu.other": {"en": "Other (somewhere else)", "ko": "Other (somewhere else)"},
    "timezone.menu.reply": {
        "en": "Reply with the number, or just type your country/city.",
        "ko": "Reply with the number, or just type your country/city.",
    },
    "timezone.menu.command": {
        "en": "You can reopen this menu anytime with /update_timezone.",
        "ko": "You can reopen this menu anytime with /update_timezone.",
    },
    "timezone.menu.example": {
        "en": "[example] 2 · London · UK · Tokyo",
        "ko": "[example] 2 · London · UK · Tokyo",
    },
    "timezone.menu.not_listed": {
        "en": (
            "If your region isn't listed, choose {other_num} and I'll ask for your country/city. "
            "(An IANA timezone like Asia/Seoul also works.)"
        ),
        "ko": (
            "If your region isn't listed, choose {other_num} and I'll ask for your country/city. "
            "(An IANA timezone like Asia/Seoul also works.)"
        ),
    },
    "timezone.other_prompt": {
        "en": "Which country or city are you in? Please type it. (e.g. London, New York, Tokyo)",
        "ko": "Which country or city are you in? Please type it. (e.g. London, New York, Tokyo)",
    },
    "timezone.unrecognized": {
        "en": "I could not recognize that region or timezone.",
        "ko": "I could not recognize that region or timezone.",
    },
    "timezone.save_error": {
        "en": "A temporary error occurred while saving your timezone. Please try again shortly.",
        "ko": "A temporary error occurred while saving your timezone. Please try again shortly.",
    },
    "timezone.saved": {
        "en": "Your timezone is set to {iana}. You can now ask health-data questions.",
        "ko": "Your timezone is set to {iana}. You can now ask health-data questions.",
    },
    "threshold.bound.lower": {
        "en": "below {value}{unit}",
        "ko": "below {value}{unit}",
    },
    "threshold.bound.upper": {
        "en": "above {value}{unit}",
        "ko": "above {value}{unit}",
    },
    "threshold.bound.none": {
        "en": "(no bound)",
        "ko": "(no bound)",
    },
    "threshold.bound.joiner": {
        "en": " or ",
        "ko": " or ",
    },
    "threshold.line": {
        "en": "- {label}: alert if {condition} for {sustain}+ min",
        "ko": "- {label}: alert if {condition} for {sustain}+ min",
    },
    "system_steps.outlier": {
        "en": (
            "Yesterday's step count differed from your usual range in the recent data.\n\n"
            "You recorded {current} steps on {target_date}. The median for the recent baseline "
            "from {baseline_start} up to {baseline_end} was {median} steps, a difference of "
            "{difference} steps.\n\n"
            "This is a candidate deviation from your usual activity pattern, not a health diagnosis. "
            "Consider some light movement or a short walk today within a comfortable range."
        ),
        "ko": (
            "Yesterday's step count differed from your usual range in the recent data.\n\n"
            "You recorded {current} steps on {target_date}. The median for the recent baseline "
            "from {baseline_start} up to {baseline_end} was {median} steps, a difference of "
            "{difference} steps.\n\n"
            "This is a candidate deviation from your usual activity pattern, not a health diagnosis. "
            "Consider some light movement or a short walk today within a comfortable range."
        ),
    },
}


CAPABILITIES_GENERAL: Mapping[str, str] = {
    "en": """I'm your personal health coach for the data your phone and connected wearables collect.

— What I can look at —

I read a unified health stream synced by the Sensei mobile app from Apple Health and Android Health Connect (including Samsung Health and connected wearable data). It can include steps, distance, speed, flights climbed, calories, exercise and workouts; heart rate, resting heart rate, HRV, and blood oxygen; sleep sessions and light / deep / REM / awake stages; weight, BMI, body composition, blood pressure, blood glucose, body temperature; and profile fields such as birth date, gender, and blood type. Curated daily/hourly activity and nightly sleep resources resolve duplicate or mirrored records before analysis.

Some deployments may separately enable direct Withings tables for vendor-only fields such as a Withings sleep score, snoring, or apnea index. Those optional tables are used only for an explicit supported metric; they are not substituted when a value is missing from the unified stream.

The exact set depends on what your own devices actually sync. Ask "What data do I have?" to see your personal list.

— How I can help —

- Daily values: "How many steps did I take yesterday?" · "What was my heart rate today?"
- Trends & breakdowns: "Show my distance for the last 7 days" · "How active was I this morning?"
- Period comparisons: "Compare my steps over the last 7 days with the previous 7 days"
- Coaching / factor analysis: "Why am I so tired today?" — I look at your recent activity, heart, and sleep together and explain the likely reasons.
- Recommendations: "How can I be more active this week?" — concrete, data-based suggestions.
- Data checks: I can tell you when your data was last synced and point out gaps.
- I also send a heads-up if a metric looks unusual (for example a sudden drop in steps), and you can reply "why?" to dig into it.

— Honesty & limits —

- If data is missing for a period, I'll say so plainly — I never treat missing records as zero.
- I can't access medical records, DNA, imaging (X-ray / MRI / CT), lab results, medication records, or anything you haven't tracked (such as cups of coffee).
- I'm not a medical professional and don't provide diagnoses.
""",
    "ko": """I'm your personal health coach for the data your phone and connected wearables collect.

— What I can look at —

I read a unified health stream synced by the Sensei mobile app from Apple Health and Android Health Connect (including Samsung Health and connected wearable data). It can include steps, distance, speed, flights climbed, calories, exercise and workouts; heart rate, resting heart rate, HRV, and blood oxygen; sleep sessions and light / deep / REM / awake stages; weight, BMI, body composition, blood pressure, blood glucose, body temperature; and profile fields such as birth date, gender, and blood type. Curated daily/hourly activity and nightly sleep resources resolve duplicate or mirrored records before analysis.

Some deployments may separately enable direct Withings tables for vendor-only fields such as a Withings sleep score, snoring, or apnea index. Those optional tables are used only for an explicit supported metric; they are not substituted when a value is missing from the unified stream.

The exact set depends on what your own devices actually sync. Ask "What data do I have?" to see your personal list.

— How I can help —

- Daily values: "How many steps did I take yesterday?" · "What was my heart rate today?"
- Trends & breakdowns: "Show my distance for the last 7 days" · "How active was I this morning?"
- Period comparisons: "Compare my steps over the last 7 days with the previous 7 days"
- Coaching / factor analysis: "Why am I so tired today?" — I look at your recent activity, heart, and sleep together and explain the likely reasons.
- Recommendations: "How can I be more active this week?" — concrete, data-based suggestions.
- Data checks: I can tell you when your data was last synced and point out gaps.
- I also send a heads-up if a metric looks unusual (for example a sudden drop in steps), and you can reply "why?" to dig into it.

— Honesty & limits —

- If data is missing for a period, I'll say so plainly — I never treat missing records as zero.
- I can't access medical records, DNA, imaging (X-ray / MRI / CT), lab results, medication records, or anything you haven't tracked (such as cups of coffee).
- I'm not a medical professional and don't provide diagnoses.
""",
}


METRIC_LABELS: Mapping[str, tuple[str, str]] = {
    # Internal table-scoped availability keys are translated here so the
    # persistence contract never appears in user-facing inventory text.
    "SOURCE_TABLE:WITHINGSACTIVITY": (
        "optional direct Withings daily activity",
        "optional direct Withings daily activity",
    ),
    "SOURCE_TABLE:WITHINGSSLEEP": (
        "optional direct Withings sleep summaries",
        "optional direct Withings sleep summaries",
    ),
    "SOURCE_TABLE:WITHINGSINTRADAY": (
        "optional direct Withings intraday activity",
        "optional direct Withings intraday activity",
    ),
    "SOURCE_TABLE:WITHINGSSLEEPDETAILMINUTE": (
        "optional direct Withings minute-level sleep physiology",
        "optional direct Withings minute-level sleep physiology",
    ),
    "SOURCE_TABLE:WITHINGSWORKOUTS": (
        "optional direct Withings workouts",
        "optional direct Withings workouts",
    ),
    "SOURCE_TABLE:WITHINGSSLEEPNIGHTEVENTSEGMENT": (
        "optional direct Withings sleep disturbance events",
        "optional direct Withings sleep disturbance events",
    ),
    "STEPS": ("steps", "steps"),
    "DISTANCE_DELTA": ("distance", "distance"),
    "DISTANCE_WALKING_RUNNING": ("walking/running distance", "walking/running distance"),
    "SPEED": ("speed", "speed"),
    "ACTIVE_ENERGY_BURNED": ("active calories", "active calories"),
    "TOTAL_CALORIES_BURNED": ("total calories", "total calories"),
    "EXERCISE_TIME": ("exercise time", "exercise time"),
    "WORKOUT": ("workouts", "workouts"),
    "HEART_RATE": ("heart rate", "heart rate"),
    "RESTING_HEART_RATE": ("resting heart rate", "resting heart rate"),
    "HEART_RATE_VARIABILITY_RMSSD": ("heart rate variability (RMSSD)", "heart rate variability (RMSSD)"),
    "HEART_RATE_VARIABILITY_SDNN": ("heart rate variability (SDNN)", "heart rate variability (SDNN)"),
    "WALKING_HEART_RATE": ("walking heart rate", "walking heart rate"),
    "BLOOD_OXYGEN": ("blood oxygen (SpO2)", "blood oxygen (SpO2)"),
    "RESPIRATORY_RATE": ("respiratory rate", "respiratory rate"),
    "WALKING_SPEED": ("walking speed", "walking speed"),
    "FLIGHTS_CLIMBED": ("flights climbed", "flights climbed"),
    "BASAL_ENERGY_BURNED": ("basal (resting) calories", "basal (resting) calories"),
    "APPLE_STAND_TIME": ("stand time", "stand time"),
    "APPLE_STAND_HOUR": ("stand hours", "stand hours"),
    "DISTANCE_CYCLING": ("cycling distance", "cycling distance"),
    "SLEEP_IN_BED": ("time in bed", "time in bed"),
    "BODY_MASS_INDEX": ("BMI", "BMI"),
    "BODY_FAT_PERCENTAGE": ("body fat percentage", "body fat percentage"),
    "LEAN_BODY_MASS": ("lean body mass", "lean body mass"),
    "HEIGHT": ("height", "height"),
    "SLEEP_SESSION": ("sleep sessions", "sleep sessions"),
    "SLEEP_LIGHT": ("light sleep", "light sleep"),
    "SLEEP_DEEP": ("deep sleep", "deep sleep"),
    "SLEEP_REM": ("REM sleep", "REM sleep"),
    "SLEEP_AWAKE": ("awake time during sleep", "awake time during sleep"),
    "WEIGHT": ("weight", "weight"),
    "BMI": ("BMI", "BMI"),
    "BLOOD_PRESSURE_SYSTOLIC": ("systolic blood pressure", "systolic blood pressure"),
    "BLOOD_PRESSURE_DIASTOLIC": ("diastolic blood pressure", "diastolic blood pressure"),
    "BLOOD_GLUCOSE": ("blood glucose", "blood glucose"),
    "BODY_TEMPERATURE": ("body temperature", "body temperature"),
}


TIMEZONE_SITE_LABELS: Mapping[str, Mapping[str, str]] = {
    "en": {
        "KR_SEOUL": "South Korea (Seoul)",
        "UK_LONDON": "United Kingdom (London)",
        "ES_MAINLAND": "Spain mainland",
        "ES_CANARY": "Spain Canary Islands",
        "ES_CEUTA": "Spain Ceuta",
    },
    "ko": {
        "KR_SEOUL": "South Korea (Seoul)",
        "UK_LONDON": "United Kingdom (London)",
        "ES_MAINLAND": "Spain mainland",
        "ES_CANARY": "Spain Canary Islands",
        "ES_CEUTA": "Spain Ceuta",
    },
}


def message(key: str, *, is_korean: bool = False, **values: Any) -> str:
    """Return a formatted deterministic message, falling back to English."""

    pair = _MESSAGES.get(str(key))
    if pair is None:
        raise KeyError(f"Unknown i18n message key: {key}")
    locale = "ko" if is_korean else "en"
    template = pair.get(locale) or pair["en"]
    return template.format(**values)


def validate_catalog() -> None:
    """Raise when a deterministic message is missing a non-empty locale value."""

    invalid = sorted(key for key, pair in _MESSAGES.items() if set(pair) != {"en", "ko"} or not all(pair.values()))
    if invalid:
        raise ValueError(f"Incomplete i18n entries: {invalid}")


validate_catalog()


__all__ = [
    "CAPABILITIES_GENERAL",
    "METRIC_LABELS",
    "TIMEZONE_SITE_LABELS",
    "message",
    "validate_catalog",
]
