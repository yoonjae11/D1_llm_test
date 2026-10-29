"""Question analysis helpers — structural/syntactic checks only.

Semantic classification (intent, case_type, metrics) is handled by LLM prompts
in domain planning and sensor planning. This module only provides lightweight
structural checks for follow-up detection.
"""

import re


def _is_contextual_followup(question: str) -> bool:
    """Detect follow-up responses that rely on previous conversation context.

    Examples: "Yes. I want to compare.", "Sure, show me", "Do that",
    "Compare them", "Yes please", "Go ahead".
    """
    q = " ".join(str(question or "").lower().split())
    if not q:
        return False
    q = re.sub(r"[?!.,]+$", "", q).strip()

    # Only expressions whose meaning is genuinely supplied by earlier context
    # belong on the deterministic carry-forward path. Sentence length alone is
    # not evidence of a follow-up: "Who are you?", "Weight today?", and
    # "Delete all alerts" are short but complete new requests.
    exact_context_replies = {
        "yes",
        "yes please",
        "yeah",
        "sure",
        "ok",
        "okay",
        "go ahead",
        "do it",
        "do that",
        "show me",
        "tell me more",
        "compare it",
        "compare them",
        "same again",
        "what about that",
        "how about that",
        "what about it",
        "how about it",
        "what time",
        "which time",
        "the time",
        "time please",
        "please say time",
        "please tell me the time",
    }
    if q in exact_context_replies:
        return True

    # Longer acknowledgements are follow-ups only when they contain an explicit
    # anaphoric reference. The model handles all complete topic-bearing turns.
    affirmative_starters = (
        "yes",
        "yeah",
        "sure",
        "ok",
        "okay",
        "please",
        "go ahead",
        "can you",
        "could you",
    )
    context_reference = re.search(
        r"\b(?:that|this|it|them|those|same|previous one|earlier one)\b",
        q,
    )
    return bool(
        len(q.split()) <= 8
        and q.startswith(affirmative_starters)
        and context_reference
    )


def _is_time_only_followup(question: str) -> bool:
    """Return whether a short turn changes only the previous plan's period.

    These turns are structurally dependent on the preceding data question, so
    sending them back through intent classification can incorrectly turn them
    into general chat when the model has little context.
    """

    q = " ".join(str(question or "").lower().split())
    q = re.sub(r"[?!.,]+$", "", q).strip()
    if not q:
        return False

    # Match a period-only replacement, not merely any short sentence that
    # happens to contain a date word (for example, "Weight today?").
    english_prefix = r"(?:(?:and|then|for)\s+|(?:what|how)\s+about\s+|same\s+for\s+)?"
    english_period = (
        r"(?:today|yesterday|the\s+day\s+before\s+yesterday|"
        r"the\s+day\s+before|the\s+previous\s+day|"
        r"(?:this|last|previous|past)\s+(?:day|week|month)|"
        r"(?:the\s+)?(?:last|past|previous)\s+\d+\s+(?:days?|weeks?|months?))"
    )
    if re.fullmatch(english_prefix + english_period, q):
        return True

    return False


_MEASUREMENT_TIME_PHRASES = (
    "what time",
    "which time",
    "at what time",
    "record time",
    "recorded time",
    "measurement time",
    "measured time",
    "sample time",
    "timestamp",
    "when was it recorded",
    "when was that recorded",
    "when was this recorded",
    "say time",
    "tell me the time",
    "time please",
    "time pls",
)

_LATEST_SAMPLE_PHRASES = (
    "current",
    "now",
    "right now",
    "latest",
    "most recent",
    "last recorded",
    "last measured",
)

_AGGREGATE_PHRASES = (
    "average",
    "avg",
    "mean",
    "min",
    "minimum",
    "max",
    "maximum",
    "peak",
    "highest",
    "lowest",
    "trend",
    "range",
)


def _asks_for_measurement_time(question: str) -> bool:
    """Return whether the user explicitly wants an observation's clock time."""

    q = " ".join(str(question or "").lower().split())
    return q in {"time", "the time"} or any(
        phrase in q for phrase in _MEASUREMENT_TIME_PHRASES
    )


def _asks_for_latest_sample(question: str) -> bool:
    """Return whether wording asks for the newest individual measurement."""

    q = " ".join(str(question or "").lower().split())
    return _asks_for_measurement_time(q) or _asks_for_explicit_latest_wording(q)


def _asks_for_explicit_latest_wording(question: str) -> bool:
    """Return whether latest/current wording appears beyond a time-only turn."""

    q = " ".join(str(question or "").lower().split())
    if re.search(r"\b(?:current|now|latest)\b", q):
        return True
    return any(
        phrase in q
        for phrase in (
            "right now",
            "most recent",
            "last recorded",
            "last measured",
        )
    )


def _asks_for_aggregate(question: str) -> bool:
    """Return whether wording explicitly requests an aggregate or trend."""

    q = " ".join(str(question or "").lower().split())
    english_terms = (
        "average",
        "avg",
        "min",
        "minimum",
        "max",
        "maximum",
        "peak",
        "highest",
        "lowest",
        "trend",
        "range",
    )
    if re.search(r"\b(?:" + "|".join(english_terms) + r")\b", q):
        return True
    if re.search(r"\bmean\b", q) and not re.search(
        r"\b(?:what|which)\s+(?:does|do)\b.*\bmean\b",
        q,
    ):
        return True
    return False


# Phrases that request a period-over-period comparison even without the word
# "compare" (e.g. "is my sleep better than last week?", "lower than usual?").
_COMPARISON_KEYWORDS = (
    "compare", "compared", "comparison", "vs", "versus", "difference",
    "better than", "worse than", "higher than", "lower than", "more than",
    "less than", "than last", "than previous", "than the previous",
    "than usual", "than normal", "than before",
)


def _is_comparison_question(question: str) -> bool:
    """True when the user wants a period-over-period (or vs-baseline) comparison."""
    q = (question or "").lower()
    return any(keyword in q for keyword in _COMPARISON_KEYWORDS)


__all__ = [
    "_AGGREGATE_PHRASES",
    "_COMPARISON_KEYWORDS",
    "_LATEST_SAMPLE_PHRASES",
    "_MEASUREMENT_TIME_PHRASES",
    "_asks_for_aggregate",
    "_asks_for_explicit_latest_wording",
    "_asks_for_latest_sample",
    "_asks_for_measurement_time",
    "_is_comparison_question",
    "_is_contextual_followup",
    "_is_time_only_followup",
]
