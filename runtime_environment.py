"""Deterministic local environment loading for direct Python execution.

Azure Functions injects App Settings (and Core Tools injects
``local.settings.json``) before importing the application.  This module is only
the fallback used by local scripts that import the packages directly.

Precedence, highest first:

1. values already present in the process environment;
2. ``local.settings.json``;
3. ``.env.local``;
4. ``.env``.

No file value can overwrite a value supplied by the host process.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, MutableMapping


_TRUE_VALUES = frozenset({"1", "true", "yes", "y", "on"})


def env_bool(
    name: str,
    default: bool = False,
    *,
    blank_uses_default: bool = True,
) -> bool:
    """Read a conventional boolean environment value.

    ``blank_uses_default=False`` retains the legacy Functions configuration
    behavior where an explicitly blank value is treated as false.
    """

    raw = os.environ.get(name)
    if raw is None:
        return default
    normalized = str(raw).strip().lower()
    if not normalized and blank_uses_default:
        return default
    return normalized in _TRUE_VALUES


def env_int(name: str, default: int) -> int:
    """Read an integer environment value, falling back on missing/invalid input."""

    try:
        return int(str(os.environ.get(name, "")).strip())
    except Exception:
        return default


def env_json(name: str, default: Any) -> Any:
    """Read a JSON environment value, falling back on missing/invalid input."""

    raw = str(os.environ.get(name, "")).strip()
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


def _read_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return values
    for line in lines:
        item = line.strip()
        if not item or item.startswith("#") or "=" not in item:
            continue
        key, value = item.split("=", 1)
        key = key.strip()
        if key:
            values[key] = value.strip().strip('"').strip("'")
    return values


def _read_local_settings(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError):
        return {}
    raw_values = payload.get("Values", {})
    if not isinstance(raw_values, dict):
        return {}
    return {
        str(key): str(value)
        for key, value in raw_values.items()
        if value is not None
    }


def load_local_environment(
    repository_root: Path | str | None = None,
    *,
    environ: MutableMapping[str, str] | None = None,
) -> None:
    """Populate only missing environment keys from local configuration files."""

    root = Path(repository_root) if repository_root is not None else Path(__file__).resolve().parent
    target = os.environ if environ is None else environ

    # Merge from lowest to highest local priority.  Existing process values are
    # applied last by simply never replacing them.
    local_values: dict[str, str] = {}
    local_values.update(_read_dotenv(root / ".env"))
    local_values.update(_read_dotenv(root / ".env.local"))
    local_values.update(_read_local_settings(root / "local.settings.json"))

    for key, value in local_values.items():
        target.setdefault(key, value)


__all__ = ["env_bool", "env_int", "env_json", "load_local_environment"]
