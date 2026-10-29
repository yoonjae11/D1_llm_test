"""ADX query service interface."""

from typing import Any, Protocol


class ADXQueryPort(Protocol):
    def execute_query(self, query: str, suppress_error_log: bool = False) -> Any: ...


__all__ = ["ADXQueryPort"]
