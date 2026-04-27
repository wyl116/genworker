"""
Shared primitives for SSE protocol formatters.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import Any

from src.streaming.events import StreamEvent


def _compact(payload: dict[str, Any]) -> dict[str, Any]:
    """Drop None values to keep SSE payloads compact and predictable."""
    return {key: value for key, value in payload.items() if value is not None}


class BaseSseFormatter(ABC):
    """Request-scoped formatter that converts StreamEvent into SSE lines."""

    @abstractmethod
    def serialize(self, event: StreamEvent) -> list[dict[str, Any]]:
        """Convert one internal event into one or more protocol payloads."""

    def format(self, event: StreamEvent) -> list[str]:
        """Encode payload dicts as `data: ...` SSE frames."""
        return [
            f"data: {json.dumps(_compact(payload), ensure_ascii=False)}\n\n"
            for payload in self.serialize(event)
        ]
