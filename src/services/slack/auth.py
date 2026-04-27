"""Slack auth provider."""
from __future__ import annotations

from .config import SlackConfig


class SlackAuth:
    """Provide the configured bot token through the shared auth interface."""

    def __init__(self, config: SlackConfig) -> None:
        self._config = config

    async def get_token(self, _scope_key: str) -> tuple[str, int]:
        return self._config.bot_token, 0
