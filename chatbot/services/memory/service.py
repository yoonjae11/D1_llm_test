"""Operational-storage factory preserving the existing repository behavior."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Optional

from .ports import OperationalStoragePort, RegisteredUserProfilePort


_OPERATIONAL_PORT: ContextVar[OperationalStoragePort | None] = ContextVar(
    "sensei_operational_memory_port", default=None,
)
_REGISTERED_PROFILE_PORT: ContextVar[RegisteredUserProfilePort | None] = ContextVar(
    "sensei_registered_profile_port", default=None,
)


@contextmanager
def use_memory_ports(
    operational_port: OperationalStoragePort,
    registered_profile_port: RegisteredUserProfilePort | None = None,
):
    """Bind test storage to this request and restore it after completion.

    Context variables propagate into the entrypoint's async worker and the
    orchestration workers without changing concurrent production requests.
    """
    operational_token = _OPERATIONAL_PORT.set(operational_port)
    profile_token = _REGISTERED_PROFILE_PORT.set(registered_profile_port)
    try:
        yield operational_port
    finally:
        _REGISTERED_PROFILE_PORT.reset(profile_token)
        _OPERATIONAL_PORT.reset(operational_token)


def create_operational_storage(
    connection_string: Optional[str] = None,
    *,
    fail_soft: bool = True,
) -> OperationalStoragePort:
    overridden = _OPERATIONAL_PORT.get()
    if overridden is not None:
        return overridden
    from chatbot.adapters.defaults import create_operational_storage_adapter

    return create_operational_storage_adapter(
        connection_string=connection_string,
        fail_soft=fail_soft,
    )


def get_registered_user_profile(
    app_user_id: str,
    *,
    port: RegisteredUserProfilePort | None = None,
) -> dict[str, object]:
    """Read the established onboarding profile through a lazy adapter."""

    if port is None:
        port = _REGISTERED_PROFILE_PORT.get()
    if port is None:
        from chatbot.adapters.defaults import LegacyRegisteredUserProfileAdapter

        port = LegacyRegisteredUserProfileAdapter()
    return dict(port.get_registered_user(app_user_id) or {})


__all__ = ["create_operational_storage", "get_registered_user_profile", "use_memory_ports"]
