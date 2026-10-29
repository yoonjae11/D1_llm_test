"""Safe, cached loader for packaged UTF-8 prompt resources."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from langchain_core.prompts import ChatPromptTemplate


_PROMPT_ROOT = Path(__file__).resolve().parent
_SYSTEM_MARKER = "[SYSTEM]\n"
_USER_MARKER = "\n[USER]\n"


def _resource_path(resource: str) -> Path:
    """Resolve a ``.txt`` resource while preventing absolute/path traversal."""

    normalized = str(resource or "").replace("\\", "/").strip()
    relative = PurePosixPath(normalized)
    if (
        not normalized
        or relative.is_absolute()
        or relative.suffix != ".txt"
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise ValueError(f"Invalid prompt resource: {resource!r}")

    candidate = _PROMPT_ROOT.joinpath(*relative.parts).resolve()
    try:
        candidate.relative_to(_PROMPT_ROOT)
    except ValueError as exc:  # Defensive check for symlinks escaping the root.
        raise ValueError(f"Prompt resource escapes package root: {resource!r}") from exc
    return candidate


@lru_cache(maxsize=None)
def load_prompt(resource: str) -> str:
    """Load one prompt resource as UTF-8 text.

    Only relative ``.txt`` paths below :mod:`chatbot.prompts` are accepted.  A
    missing/empty resource is a startup error rather than a silent empty prompt.
    """

    path = _resource_path(resource)
    text = path.read_text(encoding="utf-8")
    if not text:
        raise ValueError(f"Prompt resource is empty: {resource!r}")
    return text


def split_role_prompt(text: str, *, resource: str = "<rendered prompt>") -> tuple[str, str]:
    """Split one self-contained task resource into system and user messages."""

    normalized = str(text or "").replace("\r\n", "\n")
    if not normalized.startswith(_SYSTEM_MARKER) or normalized.count(_USER_MARKER) != 1:
        raise ValueError(
            f"Role prompt must contain one [SYSTEM] section followed by one [USER] section: {resource!r}"
        )
    system_text, user_text = normalized[len(_SYSTEM_MARKER) :].split(
        _USER_MARKER, 1
    )
    system_text = system_text.strip()
    user_text = user_text.strip()
    if not system_text or not user_text or "[SYSTEM]" in user_text or "[USER]" in user_text:
        raise ValueError(f"Invalid or empty role prompt sections: {resource!r}")
    return system_text, user_text


@lru_cache(maxsize=None)
def load_role_prompt(resource: str) -> tuple[str, str]:
    """Load a task resource that preserves separate system and user roles."""

    return split_role_prompt(load_prompt(resource), resource=resource)


def load_chat_prompt(resource: str) -> "ChatPromptTemplate":
    """Create the same single-user-message template used by ``from_template``.

    Keeping this constructor is intentional: changing these prompts to system
    messages would change the current Chat Completions and Responses API behavior.
    """

    # Raw text consumers (NLU, threshold alerts, timezone resolution) should not
    # pay the LangChain import/cold-start cost. Only template callers load it.
    from langchain_core.prompts import ChatPromptTemplate

    return ChatPromptTemplate.from_template(load_prompt(resource))
