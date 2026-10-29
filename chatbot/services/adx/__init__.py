"""ADX service boundary."""

from .ports import ADXQueryPort
from .service import default_adx_service

__all__ = ["ADXQueryPort", "default_adx_service"]
