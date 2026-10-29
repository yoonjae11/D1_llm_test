# -*- coding: utf-8 -*-
"""User-configured threshold alerts: metric aliases, parsing, formatting.

Shared by the Telegram command layer (telegram_bp), the threshold-crossing
detector, and the daily report so they all agree on metric names and units.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from chatbot.i18n import message

# User-typed metric token -> (ADX Type, friendly label, compatibility label, unit).
# Only metrics that are sampled frequently enough to evaluate a "sustained for N
# minutes" condition are eligible for threshold alerts.
_ALERT_METRICS: Dict[str, Tuple[str, str, str, str]] = {
    "HEART_RATE": ("HEART_RATE", "heart rate", "heart rate", "bpm"),
    "RESTING_HEART_RATE": ("RESTING_HEART_RATE", "resting heart rate", "resting heart rate", "bpm"),
    "BODY_TEMPERATURE": ("BODY_TEMPERATURE", "body temperature", "body temperature", "°C"),
    "BLOOD_GLUCOSE": ("BLOOD_GLUCOSE", "blood glucose", "blood glucose", "mg/dL"),
    "BLOOD_PRESSURE_SYSTOLIC": ("BLOOD_PRESSURE_SYSTOLIC", "systolic blood pressure", "systolic blood pressure", "mmHg"),
    "BLOOD_PRESSURE_DIASTOLIC": ("BLOOD_PRESSURE_DIASTOLIC", "diastolic blood pressure", "diastolic blood pressure", "mmHg"),
    "SPEED": ("SPEED", "speed", "speed", "m/s"),
}

# Accepted user input aliases -> ADX Type.
_ALIASES: Dict[str, str] = {
    "hr": "HEART_RATE", "heart_rate": "HEART_RATE", "heartrate": "HEART_RATE",
    "heart": "HEART_RATE", "bpm": "HEART_RATE",
    "resting_hr": "RESTING_HEART_RATE", "rhr": "RESTING_HEART_RATE",
    "resting_heart_rate": "RESTING_HEART_RATE",
    "temperature": "BODY_TEMPERATURE", "temp": "BODY_TEMPERATURE",
    "body_temperature": "BODY_TEMPERATURE",
    "glucose": "BLOOD_GLUCOSE", "blood_glucose": "BLOOD_GLUCOSE",
    "systolic": "BLOOD_PRESSURE_SYSTOLIC", "bp_systolic": "BLOOD_PRESSURE_SYSTOLIC",
    "diastolic": "BLOOD_PRESSURE_DIASTOLIC", "bp_diastolic": "BLOOD_PRESSURE_DIASTOLIC",
    "speed": "SPEED",
}


def resolve_metric(token: str) -> Optional[str]:
    """Map a user-typed metric token to its ADX Type, or None if unsupported."""
    key = str(token or "").strip().lower().replace("-", "_").replace(" ", "_")
    if not key:
        return None
    upper = key.upper()
    if upper in _ALERT_METRICS:
        return upper
    return _ALIASES.get(key)


def metric_label(metric_type: str, is_korean: bool = False) -> str:
    info = _ALERT_METRICS.get(str(metric_type or "").upper())
    if not info:
        return str(metric_type or "").replace("_", " ").lower()
    return info[2] if is_korean else info[1]


def metric_unit(metric_type: str) -> str:
    info = _ALERT_METRICS.get(str(metric_type or "").upper())
    return info[3] if info else ""


def recommend_threshold(metric_type: str, age=None, gender=None) -> Optional[str]:
    """A general, age/sex-aware reference-range note to help the user pick alert
    bounds during onboarding. Returns None when no medical reference applies (e.g.
    SPEED). These are general references, not medical advice."""
    m = str(metric_type or "").upper()
    try:
        a = int(str(age).strip()) if str(age or "").strip() != "" else None
    except Exception:
        a = None
    g = str(gender or "").strip().lower()
    sex = "male" if g.startswith("m") else ("female" if g.startswith("f") else "")
    who = ", ".join([p for p in [str(a) if a is not None else "", sex] if p])
    lead = f"Based on your profile ({who}), " if who else ""
    note = " These are general references, not medical advice — adjust to your own baseline."

    if m == "HEART_RATE":
        maxhr = f" Your age-predicted max heart rate is about {220 - a} bpm." if a is not None else ""
        return (f"{lead}a common reference for heart rate is about 40–120 bpm "
                f"(resting is usually 60–100 bpm).{maxhr}{note}")
    if m == "RESTING_HEART_RATE":
        return (f"{lead}resting heart rate is usually 60–100 bpm (well-trained people can be 40–60). "
                f"A common alert range is about 45–100 bpm.{note}")
    if m == "BODY_TEMPERATURE":
        return (f"{lead}normal body temperature is about 36.1–37.2°C; fever is ≥38°C and hypothermia <35°C. "
                f"A common alert range is about 35.5–38.0°C.{note}")
    if m == "BLOOD_GLUCOSE":
        return (f"{lead}fasting glucose is usually 70–99 mg/dL (after meals typically <140); "
                f"below 70 is low and above ~180 is high. A common alert range is about 70–180 mg/dL.{note}")
    if m == "BLOOD_PRESSURE_SYSTOLIC":
        age_note = (" Blood pressure tends to rise with age, so adjust the upper bound to your baseline."
                    if (a is not None and a >= 50) else "")
        return (f"{lead}systolic blood pressure is normally below 120 mmHg; below ~90 is low and above ~140 is high. "
                f"A common alert range is about 90–140 mmHg.{age_note}{note}")
    if m == "BLOOD_PRESSURE_DIASTOLIC":
        return (f"{lead}diastolic blood pressure is normally below 80 mmHg; below ~60 is low and above ~90 is high. "
                f"A common alert range is about 60–90 mmHg.{note}")
    return None  # SPEED and anything else: no medical reference


def round_to_5(hour: int, minute: int) -> Tuple[int, int]:
    """Round a clock time to the nearest 5-minute mark. Scheduled summaries fire on
    a 5-minute timer grid, so off-grid minutes (e.g. 14:46) are snapped to 14:45.
    Never crosses midnight (23:58 -> 23:55)."""
    total = int(hour) * 60 + int(minute)
    rounded = int(round(total / 5.0)) * 5
    if rounded >= 24 * 60:
        rounded = 23 * 60 + 55
    if rounded < 0:
        rounded = 0
    return rounded // 60, rounded % 60


def supported_metrics_help(is_korean: bool = False) -> str:
    names = sorted({v[0] for v in _ALERT_METRICS.values()})
    return ", ".join(metric_label(n, is_korean) for n in names)


def eligible_alert_metrics() -> List[str]:
    """ADX Type tokens that support threshold alerts (sorted), e.g. HEART_RATE.

    Used by onboarding to intersect with the user's collected metrics so we only
    offer alerts on vitals that are both eligible AND actually being collected.
    """
    return sorted(_ALERT_METRICS.keys())


def _spelled_time(s: str) -> Optional[Tuple[int, int]]:
    """Parse a spelled-out clock time ("eleven" -> 11:00, "eleven thirty" -> 11:30,
    "nine forty five" -> 9:45) into ``(hour, minute)``, or None. am/pm is applied by
    the caller. Conservative: returns None on anything it can't cleanly segment, so
    non-time text still falls through."""
    s = s.replace("o'clock", " ").replace("o’clock", " ").replace("oclock", " ")
    nums: List[int] = []
    current: Optional[int] = None
    for tok in re.split(r"[\s:]+", s):
        if not tok or tok in ("clock", "sharp", "and", "at"):
            continue
        if tok in _AGE_TENS:
            if current is not None:
                nums.append(current)
            current = _AGE_TENS[tok]
        elif tok in _AGE_ONES:
            val = _AGE_ONES[tok]
            if current is not None and current in (20, 30, 40, 50) and val < 10:
                current += val  # "forty five" -> 45
            else:
                if current is not None:
                    nums.append(current)
                current = val
        else:
            return None  # a non-number word -> not a spelled-out time
    if current is not None:
        nums.append(current)
    if not nums:
        return None
    hour = nums[0]
    minute = nums[1] if len(nums) > 1 else 0
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return hour, minute
    return None


def parse_clock_time(text: str) -> Optional[Tuple[int, int]]:
    """Parse a local time into ``(hour, minute)`` (0..23, 0..59), or None.

    Accepts "17:20", "9pm", "5:20pm", "23", "23:00", and spelled-out
    times ("eleven", "eleven thirty", "nine pm").
    Used by the daily-summary schedule (onboarding + /set_scheduled_alert + chat).
    """
    s = str(text or "").strip().lower()
    if not s:
        return None
    ampm = None
    if "pm" in s:
        ampm = "pm"
    elif "am" in s:
        ampm = "am"
    for token in ("p.m.", "a.m.", "pm", "am"):
        s = s.replace(token, " ")
    s = s.strip()

    match = re.search(r"(\d{1,2}):(\d{1,2})", s)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
    else:
        m2 = re.search(r"\d{1,2}", s)
        if m2:
            hour, minute = int(m2.group(0)), 0
        else:
            spelled = _spelled_time(s)  # "eleven" / "eleven thirty"
            if spelled is None:
                return None
            hour, minute = spelled
    if ampm == "pm" and hour < 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return hour, minute
    return None


def parse_clock_hour(text: str) -> Optional[int]:
    """Back-compat helper: the hour only (minutes ignored). Prefer parse_clock_time."""
    parsed = parse_clock_time(text)
    return parsed[0] if parsed else None


def parse_set_threshold(args: List[str], default_sustain: int) -> Tuple[Optional[dict], str]:
    """Parse `/set_threshold <metric> <lower> <upper> [sustain_min]`.

    Returns (parsed_dict, error_message). On success error_message is "".
    Either lower or upper may be "-"/"none"/"_" to leave that bound unset (but at
    least one bound must be a number).
    """
    if len(args) < 3:
        return None, "usage"
    metric_type = resolve_metric(args[0])
    if not metric_type:
        return None, f"unknown_metric:{args[0]}"

    def _bound(token: str) -> Tuple[bool, Optional[float]]:
        t = str(token).strip().lower()
        if t in {"-", "none", "_", "null", "x", "na"}:
            return True, None
        try:
            return True, float(token)
        except Exception:
            return False, None

    ok_lo, lower = _bound(args[1])
    ok_hi, upper = _bound(args[2])
    if not ok_lo or not ok_hi:
        return None, "bad_number"
    if lower is None and upper is None:
        return None, "no_bound"
    if lower is not None and upper is not None and lower >= upper:
        return None, "lower_ge_upper"

    sustain = default_sustain
    if len(args) >= 4:
        try:
            sustain = max(1, int(float(args[3])))
        except Exception:
            return None, "bad_sustain"

    return {
        "metric_type": metric_type,
        "lower": lower,
        "upper": upper,
        "sustain_minutes": sustain,
    }, ""


def format_threshold_line(metric_type: str, lower, upper, sustain: int, is_korean: bool = False, use_token: bool = False) -> str:
    unit = metric_unit(metric_type)
    u = f" {unit}" if unit else ""
    # use_token: show the exact ADX metric token (e.g. HEART_RATE) instead of the
    # friendly label, so command listings show the name the user must type back.
    label = str(metric_type or "").upper() if use_token else metric_label(metric_type, is_korean)
    lo = (
        message("threshold.bound.lower", is_korean=is_korean, value=f"{lower:g}", unit=u)
        if lower is not None
        else None
    )
    hi = (
        message("threshold.bound.upper", is_korean=is_korean, value=f"{upper:g}", unit=u)
        if upper is not None
        else None
    )
    joiner = message("threshold.bound.joiner", is_korean=is_korean)
    condition = joiner.join(item for item in (lo, hi) if item) or message(
        "threshold.bound.none", is_korean=is_korean
    )
    return message(
        "threshold.line",
        is_korean=is_korean,
        label=label,
        condition=condition,
        sustain=sustain,
    )


# ==========================================================================
# Step-by-step input parsers (one value per question)
# ==========================================================================
# The redesigned onboarding/command flows ask for a metric, then each bound and
# the duration as SEPARATE questions (instead of "40 100 10" on one line). These
# parse a single answer each. Free-text/typo tolerance beyond these rules is added
# on top by chatbot.nlu (rules -> LLM fallback).

_NONE_BOUND_TOKENS = {"-", "none", "_", "x", "na", "skip", "no", "pass"}

# English number words for the age step (handles "twenty", "thirty five", ...).
_AGE_ONES: Dict[str, int] = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
    "eighteen": 18, "nineteen": 19,
}
_AGE_TENS: Dict[str, int] = {
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}

# Filler words allowed around number words ("thirty years old", "im thirty").
_AGE_FILLER_WORDS = {"i", "im", "am", "is", "my", "age", "aged", "year", "years", "old", "about", "around", "almost", "just"}


def parse_age_words(text: str) -> Optional[int]:
    """Parse an age from digits OR English number words. Returns 1..150 or None.

    Examples: "30" -> 30, "i'm 25" -> 25, "twenty" -> 20, "thirty five" -> 35.
    """
    s = str(text or "").strip().lower()
    if not s:
        return None
    m = re.search(r"\d{1,3}", s)
    if m:
        try:
            v = int(m.group(0))
        except Exception:
            return None
        return v if 1 <= v <= 150 else None
    total: Optional[int] = None
    for tok in re.split(r"[\s\-]+", s):
        if tok in _AGE_TENS:
            total = (total or 0) + _AGE_TENS[tok]
        elif tok in _AGE_ONES:
            total = (total or 0) + _AGE_ONES[tok]
        elif tok.isalpha() and tok not in _AGE_FILLER_WORDS:
            # An unrecognized word (e.g. the typo in "twnety five") — do NOT
            # return a partial sum as an exact parse; let the fuzzy/LLM layers
            # read the whole phrase instead.
            return None
    if total is not None and 1 <= total <= 150:
        return total
    return None


# Filler/unit words to ignore inside a spelled-out number ("one hundred bpm").
_NUMBER_FILLER_WORDS = {"and", "a", "about", "around", "approximately", "roughly",
                        "bpm", "beats", "minute", "minutes", "min", "mins", "m", "mmhg",
                        "mgdl", "mg", "dl", "celsius", "c", "degrees", "degree", "ms"}


def parse_number_words(text: str) -> Optional[float]:
    """Parse a non-negative number spelled out in English words, or None.

    Examples: "one hundred" -> 100, "one hundred twenty" -> 120, "forty five" -> 45,
    "ten" -> 10, "two thousand" -> 2000. A bare digit string also parses. Any token
    that is neither a number word, "hundred"/"thousand", nor a known filler/unit word
    makes the whole parse fail (returns None) so free text falls through to the LLM.
    """
    s = str(text or "").strip().lower().replace("-", " ")
    if not s:
        return None
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*", s)
    if m:
        try:
            return float(m.group(1))
        except Exception:
            return None
    total = 0.0
    current = 0.0
    found = False
    for tok in re.split(r"[\s,]+", s):
        if not tok:
            continue
        if tok in _AGE_ONES:
            current += _AGE_ONES[tok]; found = True
        elif tok in _AGE_TENS:
            current += _AGE_TENS[tok]; found = True
        elif tok == "hundred":
            current = (current or 1) * 100; found = True
        elif tok in ("thousand", "k"):
            total += (current or 1) * 1000; current = 0.0; found = True
        elif tok in _NUMBER_FILLER_WORDS:
            continue
        else:
            return None  # an unrecognized token -> not a clean spelled-out number
    return (total + current) if found else None


def parse_one_bound(text: str) -> Tuple[str, Optional[float]]:
    """Parse a single bound answer. Returns ``(status, value)`` where status is
    "value" (value set), "none" (intentionally unbounded), or "error" (unparseable).
    """
    t = str(text or "").strip().lower()
    if t in _NONE_BOUND_TOKENS:
        return "none", None
    try:
        return "value", float(str(text).strip())
    except Exception:
        return "error", None


def parse_duration_minutes(text: str) -> Optional[int]:
    """Parse a sustain-duration answer in minutes (>=1), or None. Accepts "10",
    "10 min", and "10 minutes"."""
    m = re.search(r"\d+", str(text or ""))
    if not m:
        return None
    try:
        return max(1, int(m.group(0)))
    except Exception:
        return None


# ==========================================================================
# Auto thresholds (medical reference ranges WITH cited sources)
# ==========================================================================
def _normalize_sex(gender) -> str:
    """Map a stored gender value to 'male' | 'female' | '' (unknown)."""
    g = str(gender or "").strip().lower()
    if g.startswith("f"):
        return "female"
    if g.startswith("m"):
        return "male"
    return ""


def predicted_max_hr(age, sex: str = "") -> Optional[int]:
    """Age/sex-predicted maximum heart rate (bpm), or None when age is unknown.

    Uses sex-specific published regressions:
      - female: 206 − 0.88 × age  (Gulati et al., 2010, Circulation — women)
      - other/unknown: 208 − 0.7 × age  (Tanaka, Monahan & Seals, 2001, J Am Coll Cardiol)
    Both are more accurate across the adult age range than the traditional 220 − age.
    """
    try:
        a = int(str(age).strip()) if str(age or "").strip() != "" else None
    except Exception:
        a = None
    if a is None:
        return None
    if str(sex).lower() == "female":
        return int(round(206 - 0.88 * a))
    return int(round(208 - 0.7 * a))


# Used by the "auto" branch of the alert setup. Each entry carries concrete alert
# bounds, a sustain default, and a rationale that NAMES its source (mandatory).
# Age/sex genuinely change the numbers only where a cited guideline supports it
# (heart-rate upper bound via the age/sex-predicted maximum); for metrics whose
# standard guidelines are NOT age/sex-banded (BP, glucose, temperature) the bounds
# are the same general reference for everyone. These are references, not a diagnosis.
def auto_threshold(metric_type: str, age=None, gender=None) -> Optional[Dict]:
    """Return an auto alert config for a metric, or None when there is no medical
    basis to auto-set it (e.g. SPEED). The returned dict matches parse_set_threshold's
    output plus a ``rationale`` string that includes the source citation.
    """
    m = str(metric_type or "").upper()
    try:
        a = int(str(age).strip()) if str(age or "").strip() != "" else None
    except Exception:
        a = None
    sex = _normalize_sex(gender)

    def _pack(lower, upper, sustain, rationale):
        return {
            "metric_type": m,
            "lower": (float(lower) if lower is not None else None),
            "upper": (float(upper) if upper is not None else None),
            "sustain_minutes": int(sustain),
            "rationale": rationale,
        }

    if m == "HEART_RATE":
        mhr = predicted_max_hr(a, sex)
        if mhr is not None:
            # Upper bound = age/sex-predicted maximum: a heart rate sustained above
            # one's own max is physiologically implausible (error or a serious event),
            # so it makes an age/sex-personalised alert ceiling. Lower stays at the
            # bradycardia margin below the normal 60–100 bpm resting range.
            formula = ("206 − 0.88×age (Gulati et al., 2010, women)"
                       if sex == "female"
                       else "208 − 0.7×age (Tanaka et al., 2001)")
            who = f"a {a}-year-old " + (sex or "adult")
            return _pack(50, mhr, 10,
                         f"For {who}, the age/sex-predicted maximum heart rate is about {mhr} bpm "
                         f"[{formula}]; a rate sustained above that is implausible and likely an error "
                         "or a serious event, while sustained below ~50 bpm is bradycardia (the normal "
                         "resting range is 60–100 bpm). "
                         "Sources: Tanaka, Monahan & Seals 2001 (J Am Coll Cardiol); Gulati et al. 2010 "
                         "(Circulation); American Heart Association — Target Heart Rates.")
        # Age unknown → fall back to a general fixed reference range.
        return _pack(50, 120, 10,
                     "Resting heart rate is normally 60–100 bpm; sustained below ~50 (bradycardia) "
                     "or above ~120 at rest is uncommon. Add your age (and sex) for an age/sex-predicted "
                     "maximum-based ceiling. Source: American Heart Association — All About Heart Rate.")
    if m == "RESTING_HEART_RATE":
        sex_note = (" On average women's resting heart rate runs a few bpm higher than men's, but the "
                    "normal band and alert range are the same." if sex else "")
        return _pack(45, 100, 30,
                     "A normal resting heart rate is 60–100 bpm (well-trained adults can be 40–60)." + sex_note +
                     " Sources: American Heart Association — All About Heart Rate (Pulse); "
                     "Nanchen 2018 (Heart) — Resting heart rate: what is normal?")
    if m == "BODY_TEMPERATURE":
        return _pack(35.0, 38.0, 10,
                     "Normal body temperature is about 36.1–37.2°C; fever is ≥38°C and hypothermia is <35°C. "
                     "Source: U.S. CDC / Mayo Clinic — Fever & Hypothermia references.")
    if m == "BLOOD_GLUCOSE":
        return _pack(70, 180, 5,
                     "Fasting glucose is normally 70–99 mg/dL and below 70 is hypoglycemia; a common "
                     "post-meal alert ceiling is ~180 mg/dL. "
                     "Source: American Diabetes Association — Standards of Care in Diabetes.")
    if m == "BLOOD_PRESSURE_SYSTOLIC":
        age_note = (" Blood pressure tends to rise with age, so raise the upper bound if your baseline is higher."
                    if (a is not None and a >= 50) else "")
        return _pack(90, 140, 10,
                     "Systolic blood pressure is normally <120 mmHg; <90 is low and ≥140 is stage-2 hypertension."
                     + age_note +
                     " Source: 2017 ACC/AHA High Blood Pressure Clinical Practice Guideline.")
    if m == "BLOOD_PRESSURE_DIASTOLIC":
        return _pack(60, 90, 10,
                     "Diastolic blood pressure is normally <80 mmHg; <60 is low and ≥90 is stage-2 hypertension. "
                     "Source: 2017 ACC/AHA High Blood Pressure Clinical Practice Guideline.")
    return None  # SPEED and anything else: no medical basis -> manual only


def has_auto_threshold(metric_type: str) -> bool:
    """True when the metric can be auto-configured from a cited medical reference."""
    return auto_threshold(metric_type) is not None
