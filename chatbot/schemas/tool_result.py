"""Transport-neutral result returned by the Sensor Analytics flow."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass
class ChatbotResponse:
    """Backward-compatible response contract used by Telegram and local tools."""

    text: str
    route: str
    intent: str | None
    execution_status: str
    kql_tables: List[str] = field(default_factory=list)
    adx_row_count: int = 0
    payload: Dict[str, Any] = field(default_factory=dict)


__all__ = ["ChatbotResponse"]
