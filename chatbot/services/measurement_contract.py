"""Request-local producer metadata, independent of dataset and user identity.

This declares units only, never rescales observations. Without a caller contract
the existing payload discovery and unknown-unit behavior remain unchanged.
"""
from contextlib import contextmanager
from contextvars import ContextVar

_units = ContextVar("measurement_producer_units", default=None)
_SPEED_UNITS = {"METER_PER_SECOND", "METER_PER_MINUTE", "KILOMETER_PER_HOUR"}


@contextmanager
def use_workout_speed_unit(unit: str | None):
    if unit is not None and unit not in _SPEED_UNITS:
        raise ValueError("Unsupported producer speed unit")
    token = _units.set(unit)
    try:
        yield
    finally:
        _units.reset(token)


def producer_measurement_contracts() -> list[dict]:
    unit = _units.get()
    return ([] if unit is None else [{"resource": "health.samples", "type": "WORKOUT",
            "path": ["speed"], "unit": unit, "basis": "explicit_caller_producer_contract",
            "applies_to": "recorded_payload_field_only", "changes_values": False}])

