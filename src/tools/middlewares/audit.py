"""
Audit middleware - logs tool invocations for audit trail.
"""
import time
from typing import Any, Callable

from src.common.logger import get_logger

from ..formatters import ToolResult
from ..pipeline import ToolCallContext

logger = get_logger()


class AuditMiddleware:
    """
    Logs tool call details before and after execution.

    Records: worker_id, tenant_id, tool_name, risk_level,
    execution time, and success/error status.
    """

    async def process(
        self, ctx: ToolCallContext, next_fn: Callable[[], Any]
    ) -> ToolResult:
        """Log audit trail and delegate."""
        start = time.monotonic()

        logger.info(
            f"[Audit] Tool call start | "
            f"worker={ctx.worker_id} tenant={ctx.tenant_id} "
            f"tool={ctx.tool_name} risk={ctx.risk_level}"
        )

        result: ToolResult = await next_fn()

        elapsed_ms = (time.monotonic() - start) * 1000

        logger.info(
            f"[Audit] Tool call end | "
            f"tool={ctx.tool_name} is_error={result.is_error} "
            f"elapsed={elapsed_ms:.1f}ms"
        )

        return result
