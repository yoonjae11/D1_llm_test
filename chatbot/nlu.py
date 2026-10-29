# -*- coding: utf-8 -*-
"""Hybrid natural-language understanding for onboarding/alert answers.

Strategy is rules first, LLM fallback only when the rules are unsure:
  1. exact / alias / fuzzy (deterministic, fast, no cost)
  2. a short LLM call (best-effort; any failure — e.g. no LLM configured in a
     local dry run — is swallowed so the caller falls back to the rule result)

This lets a user type "twenty" for 30, "heart rate" / "Heart Rste" for HEART_RATE,
or "yes"/"set it up" for a numbered choice, instead of an exact token.
"""

from __future__ import annotations

import difflib
import logging
import re
from typing import List, Optional, Sequence, Set, Tuple

from chatbot.alert_thresholds import (
    _ALERT_METRICS,
    _ALIASES,
    eligible_alert_metrics,
    metric_label,
    parse_age_words,
    resolve_metric,
)
from chatbot.prompts import load_prompt, split_role_prompt
from chatbot.services.llm import complete_chat


# ==========================================================================
# Low-level LLM helper (best-effort; never raises)
# ==========================================================================
def _llm_pick(prompt: str, *, max_tokens: int = 8) -> str:
    """Run a tiny classification prompt and return the raw text, or "" on any
    failure (no LLM configured, network error, etc.)."""
    try:
        system_prompt, user_prompt = split_role_prompt(prompt)
        out = complete_chat(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0,
            max_tokens=max_tokens,
        )
        return str(out or "").strip()
    except Exception as exc:  # offline/dry-run or transient error -> rules win
        logging.debug("nlu llm fallback unavailable: %s", exc)
        return ""


# ==========================================================================
# Metric resolution (exact -> fuzzy -> LLM)
# ==========================================================================
def _candidate_metric_map() -> dict:
    """Normalized phrase -> ADX metric token, drawn from tokens, aliases, labels."""
    out: dict = {}

    def _norm(s: str) -> str:
        return str(s or "").strip().lower().replace("-", " ").replace("_", " ")

    for token in _ALERT_METRICS:
        out[_norm(token)] = token
        out[_norm(metric_label(token, False))] = token
        out[_norm(metric_label(token, True))] = token
    for alias, token in _ALIASES.items():
        out[_norm(alias)] = token
    out.pop("", None)
    return out


def _fuzzy_metric(token: str, allowed: Optional[Set[str]]) -> Optional[str]:
    cand = _candidate_metric_map()
    key = str(token or "").strip().lower().replace("-", " ").replace("_", " ")
    if not key:
        return None
    match = difflib.get_close_matches(key, list(cand.keys()), n=1, cutoff=0.72)
    if not match:
        return None
    metric = cand.get(match[0])
    if metric and (allowed is None or metric in allowed):
        return metric
    return None


def _llm_metric(token: str, allowed: Optional[Set[str]]) -> Optional[str]:
    codes = sorted(allowed) if allowed else eligible_alert_metrics()
    if not codes:
        return None
    prompt = load_prompt("operational/guided_input/metric.txt").rstrip("\n").format(
        codes=", ".join(codes),
        text=str(token or "").strip(),
    )
    out = _llm_pick(prompt, max_tokens=12).upper().replace("-", "_").replace(" ", "_")
    out = re.sub(r"[^A-Z_]", "", out)
    if out in (allowed if allowed else set(codes)):
        return out
    return None


def resolve_metric_nlu(token: str, allowed: Optional[Sequence[str]] = None) -> Optional[str]:
    """Resolve a user-typed metric to an ADX token. ``allowed`` (if given) restricts
    the result to that set (e.g. the user's collected vitals)."""
    allow: Optional[Set[str]] = {str(x).upper() for x in allowed} if allowed is not None else None
    direct = resolve_metric(token)
    if direct and (allow is None or direct in allow):
        return direct
    fuzzy = _fuzzy_metric(token, allow)
    if fuzzy:
        return fuzzy
    return _llm_metric(token, allow)


# ==========================================================================
# Age (digits/words -> LLM)
# ==========================================================================
def _fuzzy_age_words(text: str) -> Optional[int]:
    """Deterministic typo tolerance for English age words ("tirty" -> 30,
    "twnety five" -> 25). Each token is fuzzy-matched against the number-word
    vocabulary; tens+ones combine the same way as the exact parser. Every
    non-filler word must resolve — a partial match is NOT returned (free text
    like "hello five" falls through to the LLM instead)."""
    from chatbot.alert_thresholds import _AGE_FILLER_WORDS, _AGE_ONES, _AGE_TENS

    vocab = {**_AGE_TENS, **_AGE_ONES}
    total: Optional[int] = None
    for tok in re.split(r"[\s\-]+", str(text or "").strip().lower()):
        if not tok or not tok.isalpha() or tok in _AGE_FILLER_WORDS:
            continue
        match = difflib.get_close_matches(tok, list(vocab.keys()), n=1, cutoff=0.75)
        if not match:
            return None
        total = (total or 0) + vocab[match[0]]
    if total is not None and 1 <= total <= 150:
        return total
    return None


def parse_age_with_source(text: str) -> Tuple[Optional[int], str]:
    """Parse an age (1..150) and report HOW it was resolved.

    Returns ``(age, source)`` where source is:
      - "exact":  a BARE integer ("30") — the designated format, stored directly;
      - "words":  parsed but not bare ("thirty one" -> 31, "i'm 30") — an
                  interpretation, so callers confirm it with the user first;
      - "fuzzy":  a typo'd number word was corrected ("tirty" -> 30) -> confirm;
      - "llm":    the value came from the LLM reading free/typo'd text -> confirm;
      - "none":   nothing usable was found.
    """
    s = str(text or "").strip()
    if re.fullmatch(r"\d{1,3}", s):  # bare integer -> the designated format
        try:
            n = int(s)
        except Exception:
            return None, "none"
        return (n, "exact") if 1 <= n <= 150 else (None, "none")
    v = parse_age_words(text)  # digits inside text ("i'm 30") OR number words
    if v is not None:
        return v, "words"
    v = _fuzzy_age_words(text)
    if v is not None:
        return v, "fuzzy"
    out = _llm_pick(
        load_prompt("operational/guided_input/age.txt").rstrip("\n").format(text=str(text or "").strip()),
        max_tokens=6,
    )
    m = re.search(r"\d{1,3}", out)
    if m:
        try:
            n = int(m.group(0))
            return (n, "llm") if 1 <= n <= 150 else (None, "none")
        except Exception:
            return None, "none"
    return None, "none"


# ==========================================================================
# Gender (exact -> fuzzy -> LLM)
# ==========================================================================
_GENDER_CANON = ("male", "female", "other", "prefer not to say")
_GENDER_SYNONYMS = {
    "male": "male", "m": "male", "man": "male", "boy": "male",
    "female": "female", "f": "female", "woman": "female", "girl": "female",
    "other": "other",
    "prefer not to say": "prefer not to say", "prefer_not_to_say": "prefer not to say",
    "no answer": "prefer not to say", "skip": "prefer not to say", "pass": "prefer not to say",
}


def parse_gender_with_source(text: str) -> Tuple[Optional[str], str]:
    """Resolve a gender answer and report HOW it resolved. Returns
    ``(gender, source)`` where source is:
      - "exact": an exact English synonym (such as "male" or "f");
      - "fuzzy": a typo corrected by fuzzy matching ("mele" -> male) -> confirm;
      - "llm":   the LLM read free text -> confirm;
      - "none":  unresolvable.
    """
    t = str(text or "").strip().lower().replace("-", " ").replace("_", " ")
    if not t:
        return None, "none"
    if t in _GENDER_SYNONYMS:
        return _GENDER_SYNONYMS[t], "exact"
    match = difflib.get_close_matches(t, list(_GENDER_SYNONYMS.keys()), n=1, cutoff=0.75)
    if match:
        return _GENDER_SYNONYMS[match[0]], "fuzzy"
    out = _llm_pick(
        load_prompt("operational/guided_input/gender.txt").rstrip("\n").format(text=str(text or "").strip()),
        max_tokens=8,
    ).upper().replace("-", "_").replace(" ", "_")
    mapping = {"MALE": "male", "FEMALE": "female", "OTHER": "other",
               "PREFER_NOT_TO_SAY": "prefer not to say"}
    resolved = mapping.get(out)
    return (resolved, "llm") if resolved else (None, "none")


def parse_gender_nlu(text: str) -> Optional[str]:
    """Resolve a gender answer to one of ``_GENDER_CANON`` (typo-tolerant), or None.
    See ``parse_gender_with_source`` for how it resolved."""
    gender, _source = parse_gender_with_source(text)
    return gender


# ==========================================================================
# Yes/No opt-in and auto/manual choices (rules -> LLM)
# ==========================================================================
# Option 1 = yes/set-up, option 2 = skip. Number words are included.
_YES_TOKENS = {"1", "one", "first", "yes", "y", "yeah", "yep", "yup", "sure",
               "ok", "okay", "set", "setup", "set up", "do", "please"}
_NO_TOKENS = {"2", "two", "second", "no", "n", "nope", "nah", "skip", "pass",
              "don't", "dont", "later", "none"}


def _fuzzy_in(token: str, vocab, cutoff: float = 0.8) -> bool:
    """True when ``token`` fuzzy-matches any word in ``vocab`` (typo tolerance)."""
    return bool(difflib.get_close_matches(token, list(vocab), n=1, cutoff=cutoff))


def interpret_optin(text: str) -> Optional[bool]:
    """Interpret a set/skip answer. True = set up, False = skip, None = unclear.
    Accepts the numbered choice (1/2), number words (one/two), and yes/no synonyms
    in English, tolerating typos ("yse", "noo") deterministically before any LLM call."""
    t = str(text or "").strip().lower()
    if not t:
        return None
    if t in _YES_TOKENS:
        return True
    if t in _NO_TOKENS:
        return False
    # word-contains heuristics before paying for an LLM call
    if t.startswith("set") or any(w in t for w in ("yes", "sure", "set up")):
        return True
    if any(w in t for w in ("no", "skip", "pass", "later", "not needed")):
        return False
    # single-token typo tolerance (e.g. "yse" -> yes, "noo" -> no)
    if " " not in t:
        if _fuzzy_in(t, _YES_TOKENS):
            return True
        if _fuzzy_in(t, _NO_TOKENS):
            return False
    out = _llm_pick(
        load_prompt("operational/guided_input/opt_in.txt").rstrip("\n").format(text=str(text or "").strip()),
        max_tokens=4,
    ).upper()
    if "YES" in out:
        return True
    if "NO" in out:
        return False
    return None


# Option 1 = auto, option 2 = manual. Number words are included.
_AUTO_TOKENS = {"1", "one", "first", "auto", "automatic", "automatically",
                "recommend", "recommended", "default"}
_MANUAL_TOKENS = {"2", "two", "second", "manual", "manually", "myself", "custom"}


def interpret_auto_manual(text: str) -> Optional[str]:
    """Interpret an auto/manual choice. Returns "auto", "manual", or None. Accepts
    1/2, number words (one/two/first/second), English auto/manual synonyms, and
    tolerates single-token typos before falling back to the LLM."""
    t = str(text or "").strip().lower()
    if not t:
        return None
    if t in _AUTO_TOKENS:
        return "auto"
    if t in _MANUAL_TOKENS:
        return "manual"
    if any(w in t for w in ("auto", "recommend")):
        return "auto"
    if any(w in t for w in ("manual", "myself")):
        return "manual"
    if " " not in t:
        if _fuzzy_in(t, _AUTO_TOKENS):
            return "auto"
        if _fuzzy_in(t, _MANUAL_TOKENS):
            return "manual"
    out = _llm_pick(
        load_prompt("operational/guided_input/auto_manual.txt").rstrip("\n").format(text=str(text or "").strip()),
        max_tokens=4,
    ).upper()
    if "AUTO" in out:
        return "auto"
    if "MANUAL" in out:
        return "manual"
    return None


# ==========================================================================
# Numeric threshold bounds and durations (exact -> number words -> fuzzy -> LLM)
# ==========================================================================
def _fuzzy_number_words(text: str) -> Optional[float]:
    """Typo-tolerant spelled-out number ("one hudred" -> 100, "fify" -> 50). Each
    non-filler token must fuzzy-match a number word / hundred / thousand, else the
    whole parse fails (returns None) so free text still falls through to the LLM."""
    from chatbot.alert_thresholds import _AGE_ONES, _AGE_TENS, _NUMBER_FILLER_WORDS

    vocab_val = {**_AGE_ONES, **_AGE_TENS}
    scale_words = {"hundred", "thousand"}
    all_words = list(vocab_val.keys()) + list(scale_words)
    total = 0.0
    current = 0.0
    found = False
    for tok in re.split(r"[\s,\-]+", str(text or "").strip().lower()):
        if not tok:
            continue
        if not tok.isalpha():
            return None  # a stray digit/symbol -> not a clean spelled-out number
        if tok in _NUMBER_FILLER_WORDS:
            continue
        match = difflib.get_close_matches(tok, all_words, n=1, cutoff=0.75)
        if not match:
            return None
        w = match[0]
        if w in vocab_val:
            current += vocab_val[w]; found = True
        elif w == "hundred":
            current = (current or 1) * 100; found = True
        elif w == "thousand":
            total += (current or 1) * 1000; current = 0.0; found = True
    return (total + current) if found else None


def interpret_bound(text: str) -> Tuple[str, Optional[float], str]:
    """Resolve a threshold bound answer. Returns ``(status, value, source)``:
      - status: "none" (intentionally unbounded), "value", or "error";
      - source: "exact" (a digit or none-token), "words" ("one hundred" -> 100),
        or "llm" (free text the LLM read) — callers confirm "words"/"llm" values.
    """
    from chatbot.alert_thresholds import parse_one_bound, parse_number_words

    status, value = parse_one_bound(text)
    if status == "none":
        return "none", None, "exact"
    if status == "value":
        return "value", value, "exact"
    v = parse_number_words(text)
    if v is None:
        v = _fuzzy_number_words(text)  # typo tolerance ("one hudred" -> 100)
    if v is not None:
        return "value", float(v), "words"
    out = _llm_pick(
        load_prompt("operational/guided_input/threshold_bound.txt").rstrip("\n").format(text=str(text or "").strip()),
        max_tokens=8,
    )
    m = re.search(r"-?\d+(?:\.\d+)?", out)
    if m:
        try:
            return "value", float(m.group(0)), "llm"
        except Exception:
            return "error", None, ""
    return "error", None, ""


def interpret_duration(text: str) -> Tuple[Optional[int], str]:
    """Resolve a sustain-duration answer to whole minutes (>=1). Returns
    ``(minutes, source)`` where source is "exact", "words", or "llm" (or "" when
    unparseable). Callers confirm "words"/"llm" values before saving."""
    from chatbot.alert_thresholds import parse_duration_minutes, parse_number_words

    minutes = parse_duration_minutes(text)
    if minutes is not None:
        return minutes, "exact"
    v = parse_number_words(text)
    if v is None:
        v = _fuzzy_number_words(text)  # typo tolerance ("tem" -> 10, "fiften" -> 15)
    if v is not None and v >= 1:
        return max(1, int(round(v))), "words"
    out = _llm_pick(
        load_prompt("operational/guided_input/duration.txt").rstrip("\n").format(text=str(text or "").strip()),
        max_tokens=6,
    )
    m = re.search(r"\d+", out)
    if m:
        try:
            return max(1, int(m.group(0))), "llm"
        except Exception:
            return None, ""
    return None, ""


# ==========================================================================
# Vital multi-select (numbers and/or names)
# ==========================================================================
def interpret_metric_selection(text: str, options: Sequence[str]) -> Tuple[List[str], List[str]]:
    """Parse a vital-selection answer against an ordered ``options`` list.

    Returns ``(selected, invalid)``. Accepts 1-based indices ("1", "1,3"), metric
    names/typos (resolved against ``options``), and "all". Order follows the user's
    answer; duplicates are dropped.
    """
    opts = [str(o).upper() for o in options]
    opt_set = set(opts)
    text = str(text or "").strip()
    selected: List[str] = []
    invalid: List[str] = []
    if text.lower() in {"all", "everything"}:
        return list(opts), []

    def _take(metric: Optional[str], raw: str) -> None:
        if metric and metric in opt_set:
            if metric not in selected:
                selected.append(metric)
        else:
            invalid.append(raw.strip())

    # Split on commas first so multi-word names ("heart rate") stay intact; a
    # purely numeric part ("1 3") is further split into individual indices.
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if re.fullmatch(r"[\d\s]+", part):
            for num in part.split():
                idx = int(num)
                _take(opts[idx - 1] if 1 <= idx <= len(opts) else None, num)
        else:
            _take(resolve_metric_nlu(part, allowed=opt_set), part)
    return selected, invalid
