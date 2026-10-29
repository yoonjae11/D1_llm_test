# -*- coding: utf-8 -*-
"""
LangGraph node for capability, profile, timezone, and data-inventory replies.

Provides a clear, schema-based answer when users ask
"What can you do?", "What data do you have?", etc.

The capabilities intent is now detected by the LLM-based intent router
(intent_router.py) instead of rule-based pattern matching.
"""

from langchain_core.messages import AIMessage
from langchain_core.output_parsers import StrOutputParser

from chatbot.graph.state import AgentState
from chatbot.i18n import CAPABILITIES_GENERAL, METRIC_LABELS, message
from chatbot.policies.availability import SOURCE_TABLE_AVAILABILITY_PREFIX
from chatbot.prompts import load_chat_prompt
from chatbot.nodes.common.support import (
    clean_model_text as _clean_model_text,
    legacy_debug as _debug,
    llm,
    safe_json_loads as _safe_json_loads,
)
from chatbot.services.memory import get_registered_user_profile

def _latest_user_text(state: AgentState) -> str:
    messages = state.get("messages") or []
    for msg in reversed(messages):
        if str(getattr(msg, "type", "")).lower() in {"human", "user"}:
            content = getattr(msg, "content", "")
            if isinstance(content, list):
                return " ".join(str(item) for item in content)
            return str(content or "")
    return ""


# ADX Type -> friendly English label. Falls back to a prettified
# version of the raw type when not listed.
_METRIC_LABELS = METRIC_LABELS


def _gender_label(value) -> str:
    return str(value or "").replace("_", " ").strip()


def _profile_answer(app_user_id: str, question: str, is_korean: bool) -> str:
    """Answer a question about the user's OWN stored profile (name / age / gender),
    read from the Users table (the onboarding store). Answers the specific field(s)
    asked for, or all of them when the question isn't field-specific."""
    name = age = gender = ""
    try:
        user = get_registered_user_profile(app_user_id)
        name = str(user.get("Name") or "").strip()
        age = str(user.get("Age") or "").strip()
        gender = _gender_label(user.get("Gender"))
    except Exception:
        pass

    q = str(question or "").lower()
    wants_age = any(t in q for t in ("age", "old"))
    wants_gender = any(t in q for t in ("sex", "gender"))
    wants_name = "name" in q
    if not (wants_age or wants_gender or wants_name):  # not field-specific -> answer all
        wants_age = wants_gender = wants_name = True

    fields = []
    if wants_name:
        fields.append((message("capabilities.profile.field.name", is_korean=is_korean), name))
    if wants_age:
        fields.append((message("capabilities.profile.field.age", is_korean=is_korean), age))
    if wants_gender:
        fields.append((message("capabilities.profile.field.gender", is_korean=is_korean), gender))

    known = [f"{label}: {val}" for label, val in fields if val]
    unknown = [label for label, val in fields if not val]

    lines = list(known)
    if unknown:
        lines.append(message("capabilities.profile.not_set", is_korean=is_korean, labels=", ".join(unknown)))
    body = "\n".join(lines) if lines else message("capabilities.profile.empty", is_korean=is_korean)
    return body + "\n\n" + message("capabilities.profile.update_hint", is_korean=is_korean)


def _humanize_metric_type(metric_type: str, is_korean: bool = False) -> str:
    key = str(metric_type or "").strip().upper()
    if key in _METRIC_LABELS:
        return _METRIC_LABELS[key][1 if is_korean else 0]
    # Table-scoped availability keys are an internal persistence contract for
    # sources (such as Withings) that do not expose a Phone-style ``Type``.
    # Never leak that implementation detail into a user-facing inventory.
    if key.startswith(SOURCE_TABLE_AVAILABILITY_PREFIX):
        return message("capabilities.inventory.connected_source", is_korean=is_korean)
    return key.replace("_", " ").strip().lower()


def _format_local_date(value) -> str:
    text = str(value or "")
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        return text[:10]
    return text


def _personal_availability_text(availability: dict, is_korean: bool = False) -> str:
    """Plain-text list of the metrics this user actually has, with latest date."""
    rows = []
    for metric_type, item in availability.items():
        is_available = item.get("is_available") if isinstance(item, dict) else getattr(item, "is_available", False)
        if not is_available:
            continue
        latest = item.get("latest_date_to_local") if isinstance(item, dict) else getattr(item, "latest_date_to_local", "")
        rows.append((_humanize_metric_type(metric_type, is_korean), _format_local_date(latest)))
    if not rows:
        return ""
    rows.sort(key=lambda r: r[0])
    lines = [
        message(
            "capabilities.inventory.item_dated" if date else "capabilities.inventory.item",
            is_korean=is_korean,
            name=name,
            date=date,
        )
        for name, date in rows
    ]
    return message("capabilities.inventory.header", is_korean=is_korean, lines="\n".join(lines))


# Few-shot LLM sub-router for the capabilities intent. Question understanding
# belongs to the LLM (robust to typos like "tizezone"); keyword tokens are only a
# fallback when the LLM call fails.
_CAP_SUBINTENT_PROMPT = load_chat_prompt(
    "operational/capabilities_responder.txt"
)

_TIMEZONE_TOKENS = ["timezone", "time zone", "time-zone", "tizezone", "which region", "what region"]
_PROFILE_TOKENS = [
    "my age", "how old am i", "how old i am", "my name", "my sex", "my gender",
    "what gender", "what sex",
]
_INVENTORY_TOKENS = [
    "data", "metric", "collect", "collecting", "tracked", "tracking", "track",
    "record", "recording", "available",
]


def _rule_based_subintent(question: str) -> str:
    q = str(question or "").lower()
    if any(token in q for token in _TIMEZONE_TOKENS):
        return "timezone"
    if any(token in q for token in _PROFILE_TOKENS):
        return "profile"
    if any(token in q for token in _INVENTORY_TOKENS):
        return "inventory"
    return "general"


def _classify_capability_subintent(question: str) -> str:
    """LLM few-shot sub-router (typo-tolerant); rule tokens are the fallback."""
    try:
        chain = _CAP_SUBINTENT_PROMPT | llm | StrOutputParser()
        raw = chain.invoke({"question": question})
        parsed = _safe_json_loads(_clean_model_text(raw), default={})
        target = str(parsed.get("target", "")).strip().lower() if isinstance(parsed, dict) else ""
        if target in {"timezone", "profile", "inventory", "general"}:
            return target
    except Exception as exc:  # pragma: no cover - defensive fallback path.
        _debug(f"  -> capability subintent LLM failed, using rules: {exc}")
    return _rule_based_subintent(question)


def capabilities_responder(state: AgentState):
    """LangGraph node that returns the capabilities / personal-availability response.

    Sub-routes (timezone / profile / inventory / general) via a few-shot LLM classifier
    so typos and unseen phrasings are handled by language understanding rather than a
    rigid keyword list:
    - timezone -> answer from the user's stored timezone.
    - profile  -> answer the user's stored name / age / gender (Users table).
    - inventory -> list the user's ACTUAL available metrics from the availability cache.
    - general  -> the general capabilities description.
    """
    question = _latest_user_text(state)
    profile = state.get("user_profile") if isinstance(state.get("user_profile"), dict) else {}
    timezone = str(state.get("timezone_iana") or profile.get("timezone_iana") or "").strip()
    availability = state.get("availability_context") if isinstance(state.get("availability_context"), dict) else {}
    is_korean = False

    target = _classify_capability_subintent(question)
    _debug(f"  -> capability subintent: {target}")

    if target == "timezone":
        if timezone:
            answer = message("capabilities.timezone.current", is_korean=is_korean, timezone=timezone)
        else:
            answer = message("capabilities.timezone.missing", is_korean=is_korean)
        return {"messages": [AIMessage(content=answer)]}

    if target == "profile":
        app_user_id = str(state.get("user_id") or profile.get("app_user_id") or "").strip()
        return {"messages": [AIMessage(content=_profile_answer(app_user_id, question, is_korean))]}

    if target == "inventory":
        # Refresh availability on demand so the list reflects the latest sync (the
        # cached value is only as fresh as the last periodic / login refresh).
        app_user_id = str(state.get("user_id") or profile.get("app_user_id") or "").strip()
        if app_user_id:
            try:
                from chatbot.availability_refresh import refresh_and_context
                fresh = refresh_and_context(app_user_id)
                if fresh:
                    availability = fresh
            except Exception as exc:
                _debug(f"  -> inventory on-demand availability refresh failed: {exc}")

    if target == "inventory" and availability:
        listing = _personal_availability_text(availability, is_korean=is_korean)
        if listing:
            tail = message("capabilities.inventory.tail", is_korean=is_korean)
            return {"messages": [AIMessage(content=listing + "\n\n" + tail)]}

    return {"messages": [AIMessage(content=CAPABILITIES_GENERAL["en"])]}
