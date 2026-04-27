"""
Timeout middleware - wraps downstream execution with asyncio timeout.
"""
import asyncio
from typing import Any, Callable

from src.common.logger import get_logger

from ..formatters import ToolResult
from ..pipeline import ToolCallContext

logger = get_logger()

DEFAULT_TIMEOUT_SECONDS = 30


class TimeoutMiddleware:
    """
    Middleware that enforces a timeout on tool execution.

    If the downstream chain exceeds the timeout, returns an error ToolResult.
    """

    def __init__(self, default_timeout: float = DEFAULT_TIMEOUT_SECONDS):
        self._default_timeout = default_timeout

    async def process(
        self, ctx: ToolCallContext, next_fn: Callable[[], Any]
    ) -> ToolResult:
        """Wrap downstream in asyncio timeout."""
        try:
            return await asyncio.wait_for(
                next_fn(), timeout=self._default_timeout
            )
        except asyncio.TimeoutError:
            msg = (
                f"Tool '{ctx.tool_name}' execution timed out "
                f"after {self._default_timeout}s"
            )
            logger.warning(f"[TimeoutMiddleware] {msg}")
            return ToolResult(content=msg, is_error=True)
