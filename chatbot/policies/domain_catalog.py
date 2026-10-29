"""Canonical metrics over the backend-neutral Sensor query contract.

Metric definitions contain logical resources only.  The Python planner never
needs to know whether a deployment implements those resources with production
materialized views, imported evaluation data, or another backing layout.
Historical ``phone_*`` names remain accepted as compatibility aliases.
"""

from typing import Dict, List

from .semantic_resources import (
    ACTIVITY_DAILY,
    ACTIVITY_HOURLY,
    ACTIVITY_INTERVAL,
    CONVERSATION_HISTORY,
    CORE_RESOURCE_IDS,
    HEALTH_SAMPLES,
    OPTIONAL_RESOURCE_IDS,
    SENSEI_ADX_V1,
    SLEEP_NIGHTLY,
    SMARTPHONE_RESOURCE_IDS,
    WITHINGS_ACTIVITY,
    WITHINGS_INTRADAY,
    WITHINGS_NIGHT_EVENTS,
    WITHINGS_PHYSICAL_PREFIX,
    WITHINGS_RESOURCE_IDS,
    WITHINGS_SLEEP,
    WITHINGS_SLEEP_DETAIL,
    WITHINGS_WORKOUTS,
    WORKOUT_SESSIONS,
)

VALID_CASE_TYPES = {"monitoring", "factor_analysis", "prescription"}
VALID_TIME_UNITS = {"day", "week", "month", "hour"}
WITHINGS_TABLE_PREFIX = WITHINGS_PHYSICAL_PREFIX
WITHINGS_TABLES = frozenset(
    SENSEI_ADX_V1.physical_name(resource) for resource in WITHINGS_RESOURCE_IDS
)

# Physical names beginning with ``Phone`` are retained in ADX for compatibility,
# but these resources form the source-resolved curated layer derived from
# canonical health-sample contract. Optional direct-provider resources are last.
RESOURCE_PRIORITY = [
    ACTIVITY_DAILY,
    ACTIVITY_HOURLY,
    ACTIVITY_INTERVAL,
    SLEEP_NIGHTLY,
    WORKOUT_SESSIONS,
    HEALTH_SAMPLES,
    WITHINGS_ACTIVITY,
    WITHINGS_SLEEP,
    WITHINGS_INTRADAY,
    WITHINGS_SLEEP_DETAIL,
    WITHINGS_WORKOUTS,
    WITHINGS_NIGHT_EVENTS,
    CONVERSATION_HISTORY,
]
TABLE_PRIORITY = [SENSEI_ADX_V1.physical_name(resource) for resource in RESOURCE_PRIORITY]

HEALTH_DATA_FLAT_DEDUP_KEYS = "Uuid, Type, DateFrom, DateTo, ValueNumber, Unit"

CUMULATIVE_PHONE_TYPES = frozenset({
    "STEPS",
    "DISTANCE_WALKING_RUNNING",
    "DISTANCE_DELTA",
    "ACTIVE_ENERGY_BURNED",
    "TOTAL_CALORIES_BURNED",
    "FLIGHTS_CLIMBED",
    "EXERCISE_TIME",
    "APPLE_MOVE_TIME",
})
# Compatibility expression for consumers that need the catalog's result alias.
# The nightly semantic resource already computes actual sleep; never rebuild it
# from a SLEEP_SESSION window in the metric catalog.
PHONE_SLEEP_DURATION_HR_EXPR = "avg(AsleepMin) / 60.0"


METRIC_DEFINITIONS: Dict[str, Dict[str, str]] = {
    # Source-neutral core metrics use the shared canonical sensor contract.
    "steps": {"resource": ACTIVITY_DAILY, "summary": "steps = sumif(ValueSumExact, Type == 'STEPS')", "trend": "steps = sumif(ValueSumExact, Type == 'STEPS')"},
    "distance": {"resource": ACTIVITY_DAILY, "summary": "distance = sumif(ValueSumExact, Type in ('DISTANCE_WALKING_RUNNING', 'DISTANCE_DELTA'))", "trend": "distance = sumif(ValueSumExact, Type in ('DISTANCE_WALKING_RUNNING', 'DISTANCE_DELTA'))"},
    "calories": {"resource": ACTIVITY_DAILY, "summary": "calories = sumif(ValueSumExact, Type == 'ACTIVE_ENERGY_BURNED')", "trend": "calories = sumif(ValueSumExact, Type == 'ACTIVE_ENERGY_BURNED')"},
    "total_calories": {"resource": ACTIVITY_DAILY, "summary": "total_calories = sumif(ValueSumExact, Type == 'TOTAL_CALORIES_BURNED')", "trend": "total_calories = sumif(ValueSumExact, Type == 'TOTAL_CALORIES_BURNED')"},
    "active": {"resource": ACTIVITY_DAILY, "summary": "active = sumif(ValueSumExact, Type in ('EXERCISE_TIME', 'APPLE_MOVE_TIME'))", "trend": "active = sumif(ValueSumExact, Type in ('EXERCISE_TIME', 'APPLE_MOVE_TIME'))"},
    "heart_rate": {"resource": HEALTH_SAMPLES, "summary": "heart_rate_avg = avgif(todouble(ValueNumber), Type == 'HEART_RATE')", "trend": "heart_rate_avg = avgif(todouble(ValueNumber), Type == 'HEART_RATE')"},
    "total_sleep_time": {"resource": SLEEP_NIGHTLY, "summary": "sleep_duration_hr = avg(AsleepMin) / 60.0", "trend": "sleep_duration_hr = avg(AsleepMin) / 60.0"},
    "sleep_start_local": {"resource": SLEEP_NIGHTLY, "summary": "sleep_start_local = min(OnsetLocal)", "trend": "sleep_start_local = min(OnsetLocal)"},
    "sleep_end_local": {"resource": SLEEP_NIGHTLY, "summary": "sleep_end_local = max(WakeLocal)", "trend": "sleep_end_local = max(WakeLocal)"},
    "elevation": {"resource": ACTIVITY_DAILY, "summary": "elevation = sumif(ValueSumExact, Type == 'ELEVATION_GAINED')", "trend": "elevation = sumif(ValueSumExact, Type == 'ELEVATION_GAINED')"},
    "active_zone_minutes": {"resource": ACTIVITY_DAILY, "summary": "active_zone_minutes = sumif(ValueSumExact, Type == 'ACTIVE_ZONE_MINUTES')", "trend": "active_zone_minutes = sumif(ValueSumExact, Type == 'ACTIVE_ZONE_MINUTES')"},
    "fatburn_active_zone_minutes": {"resource": ACTIVITY_DAILY, "summary": "fatburn_active_zone_minutes = sumif(ValueSumExact, Type == 'FATBURN_ACTIVE_ZONE_MINUTES')", "trend": "fatburn_active_zone_minutes = sumif(ValueSumExact, Type == 'FATBURN_ACTIVE_ZONE_MINUTES')"},
    "cardio_active_zone_minutes": {"resource": ACTIVITY_DAILY, "summary": "cardio_active_zone_minutes = sumif(ValueSumExact, Type == 'CARDIO_ACTIVE_ZONE_MINUTES')", "trend": "cardio_active_zone_minutes = sumif(ValueSumExact, Type == 'CARDIO_ACTIVE_ZONE_MINUTES')"},
    "peak_active_zone_minutes": {"resource": ACTIVITY_DAILY, "summary": "peak_active_zone_minutes = sumif(ValueSumExact, Type == 'PEAK_ACTIVE_ZONE_MINUTES')", "trend": "peak_active_zone_minutes = sumif(ValueSumExact, Type == 'PEAK_ACTIVE_ZONE_MINUTES')"},
    "soft": {"resource": WITHINGS_ACTIVITY, "summary": "soft_seconds = sum(Soft)", "trend": "soft_seconds = sum(Soft)"},
    "moderate": {"resource": WITHINGS_ACTIVITY, "summary": "moderate_seconds = sum(Moderate)", "trend": "moderate_seconds = sum(Moderate)"},
    "intense": {"resource": WITHINGS_ACTIVITY, "summary": "intense_seconds = sum(Intense)", "trend": "intense_seconds = sum(Intense)"},
    "hr_average_activity": {"resource": WITHINGS_ACTIVITY, "summary": "hr_average = avg(HrAverage)", "trend": "hr_average = avg(HrAverage)"},
    "hr_min_activity": {"resource": WITHINGS_ACTIVITY, "summary": "hr_min = min(HrMin)", "trend": "hr_min = min(HrMin)"},
    "hr_max_activity": {"resource": WITHINGS_ACTIVITY, "summary": "hr_max = max(HrMax)", "trend": "hr_max = max(HrMax)"},
    "hr_zone0": {"resource": WITHINGS_ACTIVITY, "summary": "hr_zone0 = sum(HrZone0)", "trend": "hr_zone0 = sum(HrZone0)"},
    "hr_zone1": {"resource": WITHINGS_ACTIVITY, "summary": "hr_zone1 = sum(HrZone1)", "trend": "hr_zone1 = sum(HrZone1)"},
    "hr_zone2": {"resource": WITHINGS_ACTIVITY, "summary": "hr_zone2 = sum(HrZone2)", "trend": "hr_zone2 = sum(HrZone2)"},
    "hr_zone3": {"resource": WITHINGS_ACTIVITY, "summary": "hr_zone3 = sum(HrZone3)", "trend": "hr_zone3 = sum(HrZone3)"},
    "sleep_score": {"resource": WITHINGS_SLEEP, "summary": "sleep_score = avg(SleepScore)", "trend": "sleep_score = avg(SleepScore)"},
    "sleep_efficiency": {"resource": SLEEP_NIGHTLY, "summary": "sleep_efficiency = avg(SleepEfficiencyPct)", "trend": "sleep_efficiency = avg(SleepEfficiencyPct)"},
    "time_in_bed": {"resource": SLEEP_NIGHTLY, "summary": "time_in_bed_hr = avg(TimeInBedMin) / 60.0", "trend": "time_in_bed_hr = avg(TimeInBedMin) / 60.0"},
    "light_sleep": {"resource": SLEEP_NIGHTLY, "summary": "light_sleep_hr = avg(LightMin) / 60.0", "trend": "light_sleep_hr = avg(LightMin) / 60.0"},
    "deep_sleep": {"resource": SLEEP_NIGHTLY, "summary": "deep_sleep_hr = avg(DeepMin) / 60.0", "trend": "deep_sleep_hr = avg(DeepMin) / 60.0"},
    "rem_sleep": {"resource": SLEEP_NIGHTLY, "summary": "rem_sleep_hr = avg(RemMin) / 60.0", "trend": "rem_sleep_hr = avg(RemMin) / 60.0"},
    # Percent metrics are distinct semantic values.  They must never be
    # substituted with the corresponding duration when an objective query asks
    # for a percentage (for example, a sum or median of nightly DeepPct).
    "deep_sleep_percent": {"resource": SLEEP_NIGHTLY, "summary": "deep_sleep_percent = avg(DeepPct)", "trend": "deep_sleep_percent = avg(DeepPct)"},
    "rem_sleep_percent": {"resource": SLEEP_NIGHTLY, "summary": "rem_sleep_percent = avg(RemPct)", "trend": "rem_sleep_percent = avg(RemPct)"},
    "light_sleep_percent": {"resource": SLEEP_NIGHTLY, "summary": "light_sleep_percent = avg(LightPct)", "trend": "light_sleep_percent = avg(LightPct)"},
    "awake_percent": {"resource": SLEEP_NIGHTLY, "summary": "awake_percent = avg(AwakePct)", "trend": "awake_percent = avg(AwakePct)"},
    "awake_duration": {"resource": SLEEP_NIGHTLY, "summary": "awake_duration_min = avg(AwakeMin)", "trend": "awake_duration_min = avg(AwakeMin)"},
    "wakeup_count": {"resource": WITHINGS_SLEEP, "summary": "wakeup_count = avg(WakeupCount)", "trend": "wakeup_count = avg(WakeupCount)"},
    "wakeup_duration": {"resource": WITHINGS_SLEEP, "summary": "wakeup_duration_min = avg(WakeupDuration) / 60.0", "trend": "wakeup_duration_min = avg(WakeupDuration) / 60.0"},
    "out_of_bed_count": {"resource": WITHINGS_SLEEP, "summary": "out_of_bed_count = avg(OutOfBedCount)", "trend": "out_of_bed_count = avg(OutOfBedCount)"},
    "sleep_latency": {"resource": WITHINGS_SLEEP, "summary": "sleep_latency_min = avg(SleepLatency) / 60.0", "trend": "sleep_latency_min = avg(SleepLatency) / 60.0"},
    "wakeup_latency": {"resource": WITHINGS_SLEEP, "summary": "wakeup_latency_min = avg(WakeupLatency) / 60.0", "trend": "wakeup_latency_min = avg(WakeupLatency) / 60.0"},
    "snoring": {"resource": WITHINGS_SLEEP, "summary": "snoring_sec = avg(Snoring)", "trend": "snoring_sec = avg(Snoring)"},
    "snoring_episode_count": {"resource": WITHINGS_SLEEP, "summary": "snoring_episode_count = avg(SnoringEpisodeCount)", "trend": "snoring_episode_count = avg(SnoringEpisodeCount)"},
    "apnea_hypopnea_index": {"resource": WITHINGS_SLEEP, "summary": "apnea_hypopnea_index = avg(ApneaHypopneaIndex)", "trend": "apnea_hypopnea_index = avg(ApneaHypopneaIndex)"},
    "hr_average_sleep": {"resource": WITHINGS_SLEEP, "summary": "sleep_hr_average = avg(HrAverage)", "trend": "sleep_hr_average = avg(HrAverage)"},
    "hr_min_sleep": {"resource": WITHINGS_SLEEP, "summary": "sleep_hr_min = min(HrMin)", "trend": "sleep_hr_min = min(HrMin)"},
    "hr_max_sleep": {"resource": WITHINGS_SLEEP, "summary": "sleep_hr_max = max(HrMax)", "trend": "sleep_hr_max = max(HrMax)"},
    "rr_average": {"resource": WITHINGS_SLEEP, "summary": "rr_average = avg(RrAverage)", "trend": "rr_average = avg(RrAverage)"},
    "rr_min": {"resource": WITHINGS_SLEEP, "summary": "rr_min = min(RrMin)", "trend": "rr_min = min(RrMin)"},
    "rr_max": {"resource": WITHINGS_SLEEP, "summary": "rr_max = max(RrMax)", "trend": "rr_max = max(RrMax)"},
    "heart_rate_max": {"resource": HEALTH_SAMPLES, "summary": "heart_rate_max = maxif(todouble(ValueNumber), Type == 'HEART_RATE')", "trend": "heart_rate_max = maxif(todouble(ValueNumber), Type == 'HEART_RATE')"},
    "heart_rate_min": {"resource": HEALTH_SAMPLES, "summary": "heart_rate_min = minif(todouble(ValueNumber), Type == 'HEART_RATE')", "trend": "heart_rate_min = minif(todouble(ValueNumber), Type == 'HEART_RATE')"},
    "steps_intraday": {"resource": ACTIVITY_HOURLY, "summary": "steps_intraday = sumif(ValueSumExact, Type == 'STEPS')", "trend": "steps_intraday = sumif(ValueSumExact, Type == 'STEPS')"},
    "distance_intraday": {"resource": ACTIVITY_HOURLY, "summary": "distance_intraday = sumif(ValueSumExact, Type in ('DISTANCE_WALKING_RUNNING', 'DISTANCE_DELTA'))", "trend": "distance_intraday = sumif(ValueSumExact, Type in ('DISTANCE_WALKING_RUNNING', 'DISTANCE_DELTA'))"},
    "calories_intraday": {"resource": ACTIVITY_HOURLY, "summary": "calories_intraday = sumif(ValueSumExact, Type == 'ACTIVE_ENERGY_BURNED')", "trend": "calories_intraday = sumif(ValueSumExact, Type == 'ACTIVE_ENERGY_BURNED')"},
    "rmssd": {"resource": HEALTH_SAMPLES, "summary": "rmssd_avg = avgif(todouble(ValueNumber), Type == 'HEART_RATE_VARIABILITY_RMSSD')", "trend": "rmssd_avg = avgif(todouble(ValueNumber), Type == 'HEART_RATE_VARIABILITY_RMSSD')"},
    "sdnn1": {"resource": HEALTH_SAMPLES, "summary": "sdnn1_avg = avgif(todouble(ValueNumber), Type == 'HEART_RATE_VARIABILITY_SDNN')", "trend": "sdnn1_avg = avgif(todouble(ValueNumber), Type == 'HEART_RATE_VARIABILITY_SDNN')"},
    "respiration_rate": {"resource": HEALTH_SAMPLES, "summary": "respiration_rate_avg = avgif(todouble(ValueNumber), Type == 'RESPIRATORY_RATE')", "trend": "respiration_rate_avg = avgif(todouble(ValueNumber), Type == 'RESPIRATORY_RATE')"},
    "chest_movement_rate": {"resource": WITHINGS_SLEEP_DETAIL, "summary": "chest_movement_rate_avg = avg(ChestMovementRate)", "trend": "chest_movement_rate_avg = avg(ChestMovementRate)"},
    "movement_score": {"resource": WITHINGS_SLEEP_DETAIL, "summary": "movement_score_avg = avg(MovementScore)", "trend": "movement_score_avg = avg(MovementScore)"},
    "night_event_count": {"resource": WITHINGS_NIGHT_EVENTS, "summary": "night_event_count = count()", "trend": "night_event_count = count()"},
    "night_event_duration": {"resource": WITHINGS_NIGHT_EVENTS, "summary": "night_event_duration_min = avg(DurationHeuristicSec) / 60.0", "trend": "night_event_duration_min = avg(DurationHeuristicSec) / 60.0"},
    # Session measures are exposed directly by the canonical workout contract.
    # Backing payload parsing and deduplication belong to the ADX adapter.
    "workout_count": {"resource": WORKOUT_SESSIONS, "summary": "workout_count = count()", "trend": "workout_count = count()"},
    "workout_duration": {"resource": WORKOUT_SESSIONS, "summary": "workout_duration_min = sum(DurationMin)", "trend": "workout_duration_min = sum(DurationMin)"},
    "workout_steps": {"resource": WORKOUT_SESSIONS, "summary": "workout_steps = sum(Steps)", "trend": "workout_steps = sum(Steps)"},
    "workout_calories": {"resource": WORKOUT_SESSIONS, "summary": "workout_calories = sum(CaloriesKcal)", "trend": "workout_calories = sum(CaloriesKcal)"},
    "workout_distance": {"resource": WORKOUT_SESSIONS, "summary": "workout_distance = sum(Distance)", "trend": "workout_distance = sum(Distance)"},
    "workout_average_heart_rate": {"resource": WORKOUT_SESSIONS, "summary": "workout_average_heart_rate = avg(AverageHeartRateBpm)", "trend": "workout_average_heart_rate = avg(AverageHeartRateBpm)"},
    "workout_elevation_gain": {"resource": WORKOUT_SESSIONS, "summary": "workout_elevation_gain = sum(ElevationGain)", "trend": "workout_elevation_gain = sum(ElevationGain)"},
    "workout_active_zone_minutes": {"resource": WORKOUT_SESSIONS, "summary": "workout_active_zone_minutes = sum(ActiveZoneMinutes)", "trend": "workout_active_zone_minutes = sum(ActiveZoneMinutes)"},
    "workout_speed": {"resource": WORKOUT_SESSIONS, "summary": "workout_speed = avg(Speed)", "trend": "workout_speed = avg(Speed)"},
    # ``speed`` is meaningful only at workout-session grain. Keeping the
    # generic spelling in the catalog lets the typed compiler apply that
    # context-dependent mapping without teaching the planner physical columns.
    "speed": {"resource": WORKOUT_SESSIONS, "summary": "speed = avg(Speed)", "trend": "speed = avg(Speed)"},
    "workout_intensity": {"resource": WITHINGS_WORKOUTS, "summary": "workout_intensity = avg(Intensity)", "trend": "workout_intensity = avg(Intensity)"},
    # Historical explicit-Phone names retain the same curated resources.
    "phone_steps": {"resource": ACTIVITY_DAILY, "summary": "phone_steps = sum(ValueSumExact)", "trend": "phone_steps = sum(ValueSumExact)"},
    "phone_distance": {"resource": ACTIVITY_DAILY, "summary": "phone_distance = sum(ValueSumExact)", "trend": "phone_distance = sum(ValueSumExact)"},
    "phone_active_calories": {"resource": ACTIVITY_DAILY, "summary": "phone_active_calories = sum(ValueSumExact)", "trend": "phone_active_calories = sum(ValueSumExact)"},
    "phone_total_calories": {"resource": ACTIVITY_DAILY, "summary": "phone_total_calories = sum(ValueSumExact)", "trend": "phone_total_calories = sum(ValueSumExact)"},
    "phone_heart_rate": {"resource": HEALTH_SAMPLES, "summary": "phone_heart_rate = avgif(todouble(ValueNumber), Type == 'HEART_RATE')", "trend": "phone_heart_rate = avgif(todouble(ValueNumber), Type == 'HEART_RATE')"},
    "phone_resting_heart_rate": {"resource": HEALTH_SAMPLES, "summary": "phone_resting_heart_rate = avgif(todouble(ValueNumber), Type == 'RESTING_HEART_RATE')", "trend": "phone_resting_heart_rate = avgif(todouble(ValueNumber), Type == 'RESTING_HEART_RATE')"},
    # RMSSD and SDNN are distinct HRV measures and must never be averaged into
    # one value. Health Connect commonly supplies RMSSD while Apple Health
    # supplies SDNN, so keep separate canonical Phone metrics through KQL.
    "phone_hrv_rmssd": {"resource": HEALTH_SAMPLES, "summary": "phone_hrv_rmssd = avgif(todouble(ValueNumber), Type == 'HEART_RATE_VARIABILITY_RMSSD')", "trend": "phone_hrv_rmssd = avgif(todouble(ValueNumber), Type == 'HEART_RATE_VARIABILITY_RMSSD')"},
    "phone_hrv_sdnn": {"resource": HEALTH_SAMPLES, "summary": "phone_hrv_sdnn = avgif(todouble(ValueNumber), Type == 'HEART_RATE_VARIABILITY_SDNN')", "trend": "phone_hrv_sdnn = avgif(todouble(ValueNumber), Type == 'HEART_RATE_VARIABILITY_SDNN')"},
    "phone_weight": {"resource": HEALTH_SAMPLES, "summary": "phone_weight = avgif(todouble(ValueNumber), Type == 'WEIGHT')", "trend": "phone_weight = avgif(todouble(ValueNumber), Type == 'WEIGHT')"},
    "phone_bmi": {"resource": HEALTH_SAMPLES, "summary": "phone_bmi = avgif(todouble(ValueNumber), Type == 'BODY_MASS_INDEX')", "trend": "phone_bmi = avgif(todouble(ValueNumber), Type == 'BODY_MASS_INDEX')"},
    "phone_blood_pressure_systolic": {"resource": HEALTH_SAMPLES, "summary": "phone_bp_sys = avgif(todouble(ValueNumber), Type == 'BLOOD_PRESSURE_SYSTOLIC')", "trend": "phone_bp_sys = avgif(todouble(ValueNumber), Type == 'BLOOD_PRESSURE_SYSTOLIC')"},
    "phone_blood_pressure_diastolic": {"resource": HEALTH_SAMPLES, "summary": "phone_bp_dia = avgif(todouble(ValueNumber), Type == 'BLOOD_PRESSURE_DIASTOLIC')", "trend": "phone_bp_dia = avgif(todouble(ValueNumber), Type == 'BLOOD_PRESSURE_DIASTOLIC')"},
    "phone_blood_glucose": {"resource": HEALTH_SAMPLES, "summary": "phone_blood_glucose = avgif(todouble(ValueNumber), Type == 'BLOOD_GLUCOSE')", "trend": "phone_blood_glucose = avgif(todouble(ValueNumber), Type == 'BLOOD_GLUCOSE')"},
    "phone_body_temperature": {"resource": HEALTH_SAMPLES, "summary": "phone_body_temperature = avgif(todouble(ValueNumber), Type == 'BODY_TEMPERATURE')", "trend": "phone_body_temperature = avgif(todouble(ValueNumber), Type == 'BODY_TEMPERATURE')"},
    "phone_birth_date": {"resource": HEALTH_SAMPLES, "summary": "phone_birth_date = anyif(Value, Type == 'BIRTH_DATE')", "trend": "phone_birth_date = anyif(Value, Type == 'BIRTH_DATE')"},
    "phone_gender": {"resource": HEALTH_SAMPLES, "summary": "phone_gender = anyif(Value, Type == 'GENDER')", "trend": "phone_gender = anyif(Value, Type == 'GENDER')"},
    "phone_blood_type": {"resource": HEALTH_SAMPLES, "summary": "phone_blood_type = anyif(Value, Type == 'BLOOD_TYPE')", "trend": "phone_blood_type = anyif(Value, Type == 'BLOOD_TYPE')"},
    "phone_sleep_duration": {"resource": SLEEP_NIGHTLY, "summary": f"phone_sleep_duration_hr = {PHONE_SLEEP_DURATION_HR_EXPR}", "trend": f"phone_sleep_duration_hr = {PHONE_SLEEP_DURATION_HR_EXPR}"},
    "phone_sleep_start_local": {"resource": SLEEP_NIGHTLY, "summary": "phone_sleep_start_local = min(OnsetLocal)", "trend": "phone_sleep_start_local = min(OnsetLocal)"},
    "phone_sleep_end_local": {"resource": SLEEP_NIGHTLY, "summary": "phone_sleep_end_local = max(WakeLocal)", "trend": "phone_sleep_end_local = max(WakeLocal)"},
    "phone_sleep_efficiency": {"resource": SLEEP_NIGHTLY, "summary": "phone_sleep_efficiency = avg(SleepEfficiencyPct)", "trend": "phone_sleep_efficiency = avg(SleepEfficiencyPct)"},
    "phone_deep_sleep": {"resource": SLEEP_NIGHTLY, "summary": "phone_deep_sleep_hr = avg(DeepMin) / 60.0", "trend": "phone_deep_sleep_hr = avg(DeepMin) / 60.0"},
    "phone_rem_sleep": {"resource": SLEEP_NIGHTLY, "summary": "phone_rem_sleep_hr = avg(RemMin) / 60.0", "trend": "phone_rem_sleep_hr = avg(RemMin) / 60.0"},
    "phone_light_sleep": {"resource": SLEEP_NIGHTLY, "summary": "phone_light_sleep_hr = avg(LightMin) / 60.0", "trend": "phone_light_sleep_hr = avg(LightMin) / 60.0"},
    "phone_sleep_sessions": {"resource": SLEEP_NIGHTLY, "summary": "phone_sleep_sessions = count()", "trend": "phone_sleep_sessions = count()"},
    "phone_workout_count": {"resource": WORKOUT_SESSIONS, "summary": "phone_workout_count = count()", "trend": "phone_workout_count = count()"},
    "phone_workout_duration": {"resource": WORKOUT_SESSIONS, "summary": "phone_workout_duration_min = sum(DurationMin)", "trend": "phone_workout_duration_min = sum(DurationMin)"},
}

METRIC_ALIASES = {
    # Source-neutral terms resolve to the canonical semantic layer.
    # Historical phone_* names remain accepted as compatibility aliases.
    "sleep_duration": "total_sleep_time",
    "sleep_duration_hr": "total_sleep_time",
    "sleep_hours": "total_sleep_time",
    "sleep_minutes": "total_sleep_time",
    "sleep_start_time": "sleep_start_local",
    "sleep_end_time": "sleep_end_local",
    "wake_time": "sleep_end_local",
    "bedtime": "sleep_start_local",
    "wake_up_time": "sleep_end_local",
    "sleep_efficiency": "sleep_efficiency",
    "sleep_quality": "sleep_efficiency",
    "deep_sleep": "deep_sleep",
    "deep_sleep_minutes": "deep_sleep",
    "deep_sleep_pct": "deep_sleep_percent",
    "deep_sleep_percentage": "deep_sleep_percent",
    "rem_sleep": "rem_sleep",
    "rem_sleep_minutes": "rem_sleep",
    "rem_sleep_pct": "rem_sleep_percent",
    "rem_sleep_percentage": "rem_sleep_percent",
    "light_sleep": "light_sleep",
    "light_sleep_minutes": "light_sleep",
    "light_sleep_pct": "light_sleep_percent",
    "light_sleep_percentage": "light_sleep_percent",
    "awake_sleep_percent": "awake_percent",
    "awake_sleep_percentage": "awake_percent",
    "awake_minutes": "awake_duration",
    "workout_time": "workout_duration",
    "workout_session_duration": "workout_duration",
    "exercise_duration": "workout_duration",
    "exercise_steps": "workout_steps",
    "average_heart_rate": "heart_rate",
    "workout_average_hr": "workout_average_heart_rate",
    "workout_heart_rate": "workout_average_heart_rate",
    "elevation_gain": "elevation",
    "workout_elevation": "workout_elevation_gain",
    "workout_azm": "workout_active_zone_minutes",
    "activity_level": "steps",
    "active_minutes": "active",
    "moderate_minutes": "moderate",
    "intense_minutes": "intense",
    "exercise_time": "active",
    "hr_average": "heart_rate",
    "heart_rate_avg": "heart_rate",
    "hrv": "rmssd",
    "heart_rate_variability": "rmssd",
    "rmssd": "rmssd",
    "sdnn": "sdnn1",
    "hrv_rmssd": "rmssd",
    "hrv_sdnn": "sdnn1",
    # Backward-compatible explicit-Phone names resolve to a typed metric. The
    # unsuffixed form follows the source-neutral HRV default (RMSSD).
    "phone_hrv": "phone_hrv_rmssd",
    "phone_hrv_rmssd": "phone_hrv_rmssd",
    "phone_hrv_sdnn": "phone_hrv_sdnn",
    "phone_rmssd": "phone_hrv_rmssd",
    "phone_sdnn": "phone_hrv_sdnn",

    # These measurements exist only in the current Phone schema.
    "resting_heart_rate": "phone_resting_heart_rate",
    "blood_pressure": "phone_blood_pressure_systolic",
    "systolic": "phone_blood_pressure_systolic",
    "diastolic": "phone_blood_pressure_diastolic",
    "blood_glucose": "phone_blood_glucose",
    "glucose": "phone_blood_glucose",
    "weight": "phone_weight",
    "body_weight": "phone_weight",
    "bmi": "phone_bmi",
    "temperature": "phone_body_temperature",
    "body_temperature": "phone_body_temperature",
    "birthday": "phone_birth_date",
    "birth_date": "phone_birth_date",
    "date_of_birth": "phone_birth_date",
    "dob": "phone_birth_date",
    "gender": "phone_gender",
    "sex": "phone_gender",
    "blood_type": "phone_blood_type",

}

# Planner-facing source sets use logical IDs.  The legacy physical table set is
# intentionally kept byte-for-byte compatible with its previous membership;
# the two internal sleep materialized views are not direct planner targets.
SMARTPHONE_RESOURCES = frozenset(
    {
        HEALTH_SAMPLES,
        ACTIVITY_DAILY,
        ACTIVITY_HOURLY,
        ACTIVITY_INTERVAL,
        SLEEP_NIGHTLY,
        WORKOUT_SESSIONS,
    }
)
WITHINGS_RESOURCES = WITHINGS_RESOURCE_IDS
CORE_RESOURCES = CORE_RESOURCE_IDS
OPTIONAL_RESOURCES = OPTIONAL_RESOURCE_IDS
SMARTPHONE_TABLES = {
    SENSEI_ADX_V1.physical_name(resource) for resource in SMARTPHONE_RESOURCES
}
SMARTPHONE_SOURCE_KEYWORDS = [
    "smartphone", "mobile", "phone", "healthkit", "health connect", "ios", "android",
]
BOTH_SOURCE_KEYWORDS: List[str] = [
    "both", "together", "combined", "combine", "union", "withings and", "and smartphone",
    "withings and phone", "phone and withings",
]

# Compatibility map between source-neutral metrics and the historical phone_*
# names used by older KQL helpers. It is an alias map, not a source-fallback
# priority: generic values are resolved from the curated layer directly.
WITHINGS_TO_PHONE_METRIC_PRIORITY = {
    "steps": "phone_steps",
    "distance": "phone_distance",
    "calories": "phone_active_calories",
    "total_calories": "phone_total_calories",
    "active": "phone_workout_duration",
    "heart_rate": "phone_heart_rate",
    "rmssd": "phone_hrv_rmssd",
    "sdnn1": "phone_hrv_sdnn",
    "total_sleep_time": "phone_sleep_duration",
    "sleep_start_local": "phone_sleep_start_local",
    "sleep_end_local": "phone_sleep_end_local",
    "sleep_efficiency": "phone_sleep_efficiency",
    "deep_sleep": "phone_deep_sleep",
    "rem_sleep": "phone_rem_sleep",
    "light_sleep": "phone_light_sleep",
    "workout_count": "phone_workout_count",
}
PHONE_TO_WITHINGS_METRIC_PRIORITY: Dict[str, str] = {}
for _withings_metric, _phone_metric in WITHINGS_TO_PHONE_METRIC_PRIORITY.items():
    # Keep the first generic metric as the canonical reverse pair if future
    # source-specific metrics intentionally share one Phone representation.
    PHONE_TO_WITHINGS_METRIC_PRIORITY.setdefault(_phone_metric, _withings_metric)
del _withings_metric, _phone_metric

# KQL generation uses the current canonical function helpers. These expressions
# remain a stable catalog-level description for compatibility consumers.
WITHINGS_TO_PHONE_FALLBACK_EXPR = {
    "steps": "sumif(ValueSumExact, Type == 'STEPS')",
    "distance": "sumif(ValueSumExact, Type in ('DISTANCE_WALKING_RUNNING', 'DISTANCE_DELTA'))",
    "calories": "sumif(ValueSumExact, Type == 'ACTIVE_ENERGY_BURNED')",
    "total_calories": "sumif(ValueSumExact, Type == 'TOTAL_CALORIES_BURNED')",
    "active": "sumif(ValueSumExact, Type in ('EXERCISE_TIME', 'APPLE_MOVE_TIME'))",
    "heart_rate": "avgif(todouble(ValueNumber), Type == 'HEART_RATE')",
    "total_sleep_time": "avg(phone_sleep_duration_hr)",
    "workout_count": "count()",
}
WITHINGS_PHONE_SLEEP_FALLBACK_METRICS = frozenset(
    {
        "total_sleep_time",
        "sleep_start_local",
        "sleep_end_local",
        "sleep_efficiency",
        "deep_sleep",
        "rem_sleep",
        "light_sleep",
    }
)

# The nightly sleep contract can construct timing from a session, in-bed window, or asleep
# stages, but actual duration requires asleep-stage evidence. SLEEP_SESSION is a
# whole session window in the mobile ingestion contract and must not advertise
# total-sleep availability by itself.
_PHONE_SLEEP_NIGHTS_ASLEEP_TYPES = frozenset(
    {"SLEEP_ASLEEP", "SLEEP_LIGHT", "SLEEP_DEEP", "SLEEP_REM"}
)
_PHONE_SLEEP_NIGHTS_SESSION_TYPES = _PHONE_SLEEP_NIGHTS_ASLEEP_TYPES | frozenset(
    {"SLEEP_SESSION", "SLEEP_IN_BED"}
)
PHONE_SLEEP_NIGHTS_AVAILABILITY_TYPES = {
    "total_sleep_time": _PHONE_SLEEP_NIGHTS_ASLEEP_TYPES,
    "phone_sleep_duration": _PHONE_SLEEP_NIGHTS_ASLEEP_TYPES,
    "sleep_start_local": _PHONE_SLEEP_NIGHTS_SESSION_TYPES,
    "phone_sleep_start_local": _PHONE_SLEEP_NIGHTS_SESSION_TYPES,
    "sleep_end_local": _PHONE_SLEEP_NIGHTS_SESSION_TYPES,
    "phone_sleep_end_local": _PHONE_SLEEP_NIGHTS_SESSION_TYPES,
    "sleep_efficiency": _PHONE_SLEEP_NIGHTS_ASLEEP_TYPES,
    "phone_sleep_efficiency": _PHONE_SLEEP_NIGHTS_ASLEEP_TYPES,
    "time_in_bed": _PHONE_SLEEP_NIGHTS_SESSION_TYPES,
    "awake_duration": frozenset({"SLEEP_AWAKE"}),
    "deep_sleep": frozenset({"SLEEP_DEEP"}),
    "deep_sleep_percent": frozenset({"SLEEP_DEEP"}),
    "phone_deep_sleep": frozenset({"SLEEP_DEEP"}),
    "rem_sleep": frozenset({"SLEEP_REM"}),
    "rem_sleep_percent": frozenset({"SLEEP_REM"}),
    "phone_rem_sleep": frozenset({"SLEEP_REM"}),
    "light_sleep": frozenset({"SLEEP_LIGHT"}),
    "light_sleep_percent": frozenset({"SLEEP_LIGHT"}),
    "awake_percent": frozenset({"SLEEP_AWAKE"}),
    "phone_light_sleep": frozenset({"SLEEP_LIGHT"}),
    "phone_sleep_sessions": _PHONE_SLEEP_NIGHTS_SESSION_TYPES,
}
PHONE_SLEEP_NIGHTS_METRICS = frozenset(
    {
        "total_sleep_time",
        "sleep_start_local",
        "sleep_end_local",
        "sleep_efficiency",
        "time_in_bed",
        "awake_duration",
        "deep_sleep",
        "deep_sleep_percent",
        "rem_sleep",
        "rem_sleep_percent",
        "light_sleep",
        "light_sleep_percent",
        "awake_percent",
        "phone_sleep_duration",
        "phone_sleep_start_local",
        "phone_sleep_end_local",
        "phone_sleep_efficiency",
        "phone_deep_sleep",
        "phone_rem_sleep",
        "phone_light_sleep",
        "phone_sleep_sessions",
    }
)
PHONE_PROFILE_METRICS = {"phone_birth_date", "phone_gender", "phone_blood_type"}


__all__ = [
    "BOTH_SOURCE_KEYWORDS",
    "CORE_RESOURCES",
    "CUMULATIVE_PHONE_TYPES",
    "HEALTH_DATA_FLAT_DEDUP_KEYS",
    "METRIC_ALIASES",
    "METRIC_DEFINITIONS",
    "OPTIONAL_RESOURCES",
    "PHONE_PROFILE_METRICS",
    "PHONE_SLEEP_NIGHTS_METRICS",
    "PHONE_SLEEP_NIGHTS_AVAILABILITY_TYPES",
    "PHONE_SLEEP_DURATION_HR_EXPR",
    "PHONE_TO_WITHINGS_METRIC_PRIORITY",
    "SMARTPHONE_SOURCE_KEYWORDS",
    "SMARTPHONE_RESOURCES",
    "SMARTPHONE_TABLES",
    "RESOURCE_PRIORITY",
    "TABLE_PRIORITY",
    "VALID_CASE_TYPES",
    "VALID_TIME_UNITS",
    "WITHINGS_PHONE_SLEEP_FALLBACK_METRICS",
    "WITHINGS_TABLE_PREFIX",
    "WITHINGS_RESOURCES",
    "WITHINGS_TABLES",
    "WITHINGS_TO_PHONE_FALLBACK_EXPR",
    "WITHINGS_TO_PHONE_METRIC_PRIORITY",
]
