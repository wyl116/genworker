"""Slack platform exceptions."""
from __future__ import annotations

from typing import Any


class SlackAPIError(RuntimeError):
    """Raised when Slack responds with an API error."""

    def __init__(self, message: str, *, payload: Any | None = None) -> None:
        super().__init__(message)
        self.payload = payload
