"""Small, dependency-free security helpers for HTTP authentication flows."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any
from urllib.parse import urlsplit, urlunsplit


class OAuthStateError(ValueError):
    """Raised when an OAuth state value is invalid, tampered with, or expired."""


class OAuthStateConfigurationError(RuntimeError):
    """Raised when signed OAuth state cannot be created or checked safely."""


def constant_time_secret_matches(expected: str, supplied: str) -> bool:
    """Compare secrets without leaking a useful prefix-timing signal."""

    expected_bytes = str(expected or "").encode("utf-8")
    supplied_bytes = str(supplied or "").encode("utf-8")
    return bool(expected_bytes) and hmac.compare_digest(expected_bytes, supplied_bytes)


def passcode_matches(user_entity: dict[str, Any] | None, supplied: str) -> bool:
    """Check the legacy cross-app passcode contract in constant time.

    The Smartphone Function App currently reads the same ``Passcode`` property,
    so changing this property to a hash in only this app would break mobile login.
    """

    stored = "" if not user_entity else str(user_entity.get("Passcode", ""))
    return constant_time_secret_matches(stored, supplied)


def build_withings_redirect_uri(configured_base_url: str, request_url: str) -> str:
    """Return the canonical OAuth callback URL.

    ``BASE_URL`` is canonically an origin (for example
    ``https://example.azurewebsites.net``). A historic trailing ``/api`` is
    accepted and normalized so existing deployments do not produce ``/api/api``.
    """

    configured = str(configured_base_url or "").strip()
    source = configured or str(request_url or "").strip()
    parsed = urlsplit(source)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise OAuthStateConfigurationError("BASE_URL must be an absolute HTTP(S) origin.")

    if configured:
        path = parsed.path.rstrip("/")
        if path.lower() == "/api":
            path = ""
        elif path:
            raise OAuthStateConfigurationError(
                "BASE_URL must not contain a path (a legacy trailing /api is accepted)."
            )
        origin = urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))
    else:
        origin = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))

    return f"{origin.rstrip('/')}/api/withings_auth"


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    try:
        return base64.b64decode(value + padding, altchars=b"-_", validate=True)
    except Exception as exc:
        raise OAuthStateError("OAuth state encoding is invalid.") from exc


def create_oauth_state(
    app_user_id: str,
    telegram_chat_id: str,
    secret: str,
    *,
    now: int | None = None,
) -> str:
    """Create a compact HMAC-SHA256 signed OAuth state value."""

    if not str(secret or ""):
        raise OAuthStateConfigurationError("WITHINGS_OAUTH_STATE_SECRET is missing.")
    app_user_id = str(app_user_id or "").strip()
    telegram_chat_id = str(telegram_chat_id or "").strip()
    if not app_user_id or not telegram_chat_id:
        raise OAuthStateError("OAuth state identifiers are missing.")

    payload = {
        "v": 1,
        "u": app_user_id,
        "c": telegram_chat_id,
        "iat": int(time.time() if now is None else now),
    }
    encoded_payload = _b64url_encode(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
    signed_value = f"v1.{encoded_payload}"
    signature = hmac.new(
        str(secret).encode("utf-8"), signed_value.encode("ascii"), hashlib.sha256
    ).digest()
    return f"{signed_value}.{_b64url_encode(signature)}"


def verify_oauth_state(
    state: str,
    secret: str,
    ttl_seconds: int,
    *,
    now: int | None = None,
    allow_legacy_local: bool = False,
    environment_name: str = "",
) -> tuple[str, str | None]:
    """Verify and unpack OAuth state, with an explicitly local-only legacy mode."""

    raw_state = str(state or "").strip()
    if not raw_state:
        raise OAuthStateError("OAuth state is missing.")

    local_environment = str(environment_name or "").strip().lower() in {
        "local",
        "dev",
        "development",
        "test",
    }
    if not raw_state.startswith("v1."):
        if allow_legacy_local and local_environment and ":" in raw_state:
            app_user_id, telegram_chat_id = raw_state.split(":", 1)
            if app_user_id.strip() and telegram_chat_id.strip():
                return app_user_id.strip(), telegram_chat_id.strip()
        raise OAuthStateError("Unsigned OAuth state is not accepted.")

    if not str(secret or ""):
        raise OAuthStateConfigurationError("WITHINGS_OAUTH_STATE_SECRET is missing.")
    try:
        version, encoded_payload, supplied_signature = raw_state.split(".", 2)
    except ValueError as exc:
        raise OAuthStateError("OAuth state format is invalid.") from exc
    signed_value = f"{version}.{encoded_payload}"
    expected_signature = _b64url_encode(
        hmac.new(
            str(secret).encode("utf-8"), signed_value.encode("ascii"), hashlib.sha256
        ).digest()
    )
    if not hmac.compare_digest(expected_signature, supplied_signature):
        raise OAuthStateError("OAuth state signature is invalid.")

    try:
        payload = json.loads(_b64url_decode(encoded_payload).decode("utf-8"))
        app_user_id = str(payload["u"]).strip()
        telegram_chat_id = str(payload["c"]).strip()
        issued_at = int(payload["iat"])
    except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise OAuthStateError("OAuth state payload is invalid.") from exc
    if payload.get("v") != 1 or not app_user_id or not telegram_chat_id:
        raise OAuthStateError("OAuth state payload is invalid.")

    ttl = int(ttl_seconds)
    if ttl <= 0:
        raise OAuthStateConfigurationError("WITHINGS_OAUTH_STATE_TTL_SECONDS must be positive.")
    current_time = int(time.time() if now is None else now)
    if issued_at > current_time + 60:
        raise OAuthStateError("OAuth state was issued in the future.")
    if current_time - issued_at > ttl:
        raise OAuthStateError("OAuth state has expired.")
    return app_user_id, telegram_chat_id


__all__ = [
    "OAuthStateConfigurationError",
    "OAuthStateError",
    "build_withings_redirect_uri",
    "constant_time_secret_matches",
    "create_oauth_state",
    "passcode_matches",
    "verify_oauth_state",
]
