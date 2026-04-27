"""Dispatcher for parsed channel commands."""
from __future__ import annotations

from src.channels.models import ReplyContent


class CommandDispatcher:
    """Execute a parsed command and normalize failures."""

    async def execute(self, match, ctx) -> ReplyContent:
        try:
            content = await match.spec.handler(ctx)
            if isinstance(content, ReplyContent):
                return content
            return ReplyContent(text=str(content))
        except Exception as exc:
            return ReplyContent(text=f"Command /{match.spec.name} failed: {exc}")

