"""Timezone onboarding helpers for the unified health-data chatbot.

The chatbot resolves "today / now / yesterday" against the user's timezone, so a
health-data question is only answerable once a timezone is known. Per the work
spec we never auto-detect timezone from the Telegram client; the timezone must be
explicitly resolved from one of (priority order):

1. ``ChatbotUserProfile.TimezoneIana`` (explicit profile value)
2. a phone-app supplied IANA timezone (written into the same profile field)
3. a ``StudySiteCode`` that maps to a supported site timezone
4. otherwise -> onboarding required

This module is intentionally light (no langchain / no graph imports) so the
Telegram adapter and the interview flow can import it cheaply.
"""

from __future__ import annotations

import difflib
import logging
import re
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from chatbot.config import get_settings
from chatbot.i18n import TIMEZONE_SITE_LABELS, message
from chatbot.prompts import load_prompt
from chatbot.services.memory import OperationalStoragePort, create_operational_storage


def OperationalTableRepository(
    connection_string: str | None = None,
    *,
    fail_soft: bool = True,
) -> OperationalStoragePort:
    """Compatibility constructor routed through the memory service."""

    return create_operational_storage(
        connection_string=connection_string,
        fail_soft=fail_soft,
    )

logger = logging.getLogger(__name__)

# Human-readable labels for the known supported sites. Unknown site codes fall
# back to the code itself so a custom CHATBOT_SUPPORTED_SITE_TIMEZONES_JSON still
# renders a usable menu.
SITE_LABELS_KR: Dict[str, str] = dict(TIMEZONE_SITE_LABELS["ko"])
SITE_LABELS_EN: Dict[str, str] = dict(TIMEZONE_SITE_LABELS["en"])

# Free-text aliases -> site code, to make "/update_timezone seoul" friendly.
_SITE_ALIASES: Dict[str, str] = {
    "seoul": "KR_SEOUL",
    "korea": "KR_SEOUL",
    "kr": "KR_SEOUL",
    "london": "UK_LONDON",
    "uk": "UK_LONDON",
    "madrid": "ES_MAINLAND",
    "spain": "ES_MAINLAND",
    "canary": "ES_CANARY",
    "ceuta": "ES_CEUTA",
}


def site_timezones() -> Dict[str, str]:
    """Return the configured {site_code: iana} map."""
    sites = get_settings().supported_site_timezones
    if isinstance(sites, dict) and sites:
        return {str(k): str(v) for k, v in sites.items()}
    return {
        "KR_SEOUL": "Asia/Seoul",
        "UK_LONDON": "Europe/London",
        "ES_MAINLAND": "Europe/Madrid",
        "ES_CANARY": "Atlantic/Canary",
        "ES_CEUTA": "Africa/Ceuta",
    }


def _site_order() -> List[str]:
    return list(site_timezones().keys())


def _is_valid_iana(value: str) -> bool:
    value = str(value or "").strip()
    if not value:
        return False
    try:
        ZoneInfo(value)
        return True
    except Exception:
        return False


def timezone_is_resolved(profile: Any) -> bool:
    """True only when the profile carries an explicit, valid IANA timezone.

    Uses the raw entity rather than the dataclass field, because
    ``OperationalTableRepository.get_user_profile`` substitutes a default timezone
    when no row exists. We must not treat that default as a user-confirmed value.
    """
    raw = getattr(profile, "raw", None)
    if not isinstance(raw, dict):
        return False
    return _is_valid_iana(str(raw.get("TimezoneIana") or ""))


def build_menu_message(is_korean: bool, *, intro: bool = True) -> str:
    """Render the site-selection menu shown during onboarding."""
    labels = SITE_LABELS_KR if is_korean else SITE_LABELS_EN
    lines: List[str] = []
    if intro:
        lines.append(message("timezone.menu.intro", is_korean=is_korean))
        lines.append("")
    for idx, code in enumerate(_site_order(), start=1):
        lines.append(f"{idx}. {labels.get(code, code)}")
    other_num = len(_site_order()) + 1
    lines.append(f"{other_num}. " + message("timezone.menu.other", is_korean=is_korean))
    lines.append("")
    lines.append(message("timezone.menu.reply", is_korean=is_korean))
    lines.append(message("timezone.menu.example", is_korean=is_korean))
    lines.append(message("timezone.menu.not_listed", is_korean=is_korean, other_num=other_num))
    lines.append(message("timezone.menu.command", is_korean=is_korean))
    return "\n".join(lines)


def is_other_choice(text: str) -> bool:
    """Return whether the user picked the numbered or named 'Other' option."""
    t = str(text or "").strip().lower()
    if t in {"other", "elsewhere"}:
        return True
    return t == str(len(_site_order()) + 1)


def other_prompt_message(is_korean: bool) -> str:
    """Prompt shown after the user picks 'Other' — ask for their country/city."""
    return message("timezone.other_prompt", is_korean=is_korean)


def _guess_timezone(text: str) -> Optional[str]:
    """Best-effort: map a free country/city to an IANA zone by matching the zone's
    last path component (e.g. 'new york' -> 'America/New_York', 'tokyo' ->
    'Asia/Tokyo'). Returns None when nothing matches."""
    norm = "_".join(str(text or "").strip().lower().replace("-", " ").split())
    if not norm:
        return None
    try:
        from zoneinfo import available_timezones
        zones = available_timezones()
    except Exception:
        return None
    for zone in zones:
        if zone.rsplit("/", 1)[-1].lower() == norm:
            return zone
    return None


def resolve_choice_ex(text: str) -> Tuple[Optional[str], str, str]:
    """Resolve a user choice to ``(iana_timezone, site_code, source)``.

    ``source`` reports HOW it resolved so callers can confirm inferred matches:
      - "exact": menu number, site code, friendly alias, raw IANA, or a clean
                 country/city match (the designated input forms);
      - "fuzzy": a typo corrected by fuzzy matching ("koera" -> Seoul);
      - "llm":   the LLM read a free-form / typo'd location.
    ``iana`` is ``None`` (source "") when nothing resolves.
    """
    raw = str(text or "").strip()
    if not raw:
        return None, "", ""

    sites = site_timezones()
    order = _site_order()

    # 1) menu number
    if raw.isdigit():
        index = int(raw) - 1
        if 0 <= index < len(order):
            code = order[index]
            return sites[code], code, "exact"
        return None, "", ""

    upper = raw.upper()
    # 2) exact site code
    if upper in sites:
        return sites[upper], upper, "exact"

    # 3) friendly alias
    alias = _SITE_ALIASES.get(raw.lower())
    if alias and alias in sites:
        return sites[alias], alias, "exact"

    # 4) raw IANA timezone (custom)
    if _is_valid_iana(raw):
        for code, iana in sites.items():
            if iana == raw:
                return iana, code, "exact"
        return raw, "", "exact"

    # 5) best-effort free country/city -> IANA (e.g. "Tokyo", "New York")
    guess = _guess_timezone(raw)
    if guess:
        for code, iana in sites.items():
            if iana == guess:
                return guess, code, "exact"
        return guess, "", "exact"

    # 6) fuzzy alias match (typo tolerance): "koera" -> "korea", "seuol" -> "seoul".
    fuzzy = difflib.get_close_matches(raw.lower(), list(_SITE_ALIASES.keys()), n=1, cutoff=0.72)
    if fuzzy:
        code = _SITE_ALIASES[fuzzy[0]]
        if code in sites:
            return sites[code], code, "fuzzy"

    # 7) LLM fallback for free-form / typo'd country or city (best-effort).
    llm_iana = _llm_timezone(raw)
    if llm_iana and _is_valid_iana(llm_iana):
        for code, iana in sites.items():
            if iana == llm_iana:
                return llm_iana, code, "llm"
        return llm_iana, "", "llm"

    return None, "", ""


def resolve_choice(text: str) -> Optional[Tuple[str, str]]:
    """Resolve a user choice to ``(iana_timezone, site_code)``, or ``None``.
    Back-compat wrapper over :func:`resolve_choice_ex` (drops the source)."""
    iana, site_code, _source = resolve_choice_ex(text)
    return (iana, site_code) if iana else None


def _llm_timezone(text: str) -> Optional[str]:
    """Ask the LLM to map a free/typo'd country or city to an IANA timezone.
    Lazy import keeps this module light; returns None on any failure."""
    try:
        from chatbot.nlu import _llm_pick
    except Exception:
        return None
    out = _llm_pick(
        load_prompt("operational/guided_input/timezone.txt").rstrip("\n").format(text=str(text or "").strip()),
        max_tokens=12,
    )
    match = re.search(r"[A-Za-z]+/[A-Za-z_\-/]+", str(out or ""))
    return match.group(0) if match else None


def save_resolved_timezone(
    app_user_id: str,
    timezone_iana: str,
    site_code: str = "",
    *,
    source: str = "user",
    repository: Optional[OperationalTableRepository] = None,
) -> bool:
    """Persist a resolved timezone to ChatbotUserProfile (MERGE, no table create)."""
    repository = repository or OperationalTableRepository(fail_soft=True)
    return repository.save_user_timezone(
        app_user_id,
        timezone_iana,
        source=source,
        study_site_code=site_code,
    )


def handle_update_timezone_command(
    app_user_id: str,
    arg: str,
    *,
    is_korean: bool = False,
    source: str = "user_command",
) -> str:
    """Handle the ``/update_timezone`` Telegram command and return the reply text."""
    arg = str(arg or "").strip()
    if not arg:
        return build_menu_message(is_korean, intro=True)

    if is_other_choice(arg):
        return other_prompt_message(is_korean)

    resolved = resolve_choice(arg)
    if not resolved:
        return message("timezone.unrecognized", is_korean=is_korean) + "\n\n" + build_menu_message(is_korean, intro=False)

    iana, site_code = resolved
    saved = save_resolved_timezone(app_user_id, iana, site_code, source=source)
    if not saved:
        return message("timezone.save_error", is_korean=is_korean)

    return message("timezone.saved", is_korean=is_korean, iana=iana)
