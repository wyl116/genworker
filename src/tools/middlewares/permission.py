"""
Permission middleware - checks risk_level against worker permission level.
"""
from typing import Any, Callable

from src.common.logger import get_logger

from ..formatters import ToolResult
from ..mcp.types import RiskLevel
from ..pipeline import ToolCallContext

logger = get_logger()

# Risk level ordering for comparison
_RISK_ORDER: dict[str, int] = {
    RiskLevel.LOW.value: 0,
    RiskLevel.MEDIUM.value: 1,
    RiskLevel.HIGH.value: 2,
    RiskLevel.CRITICAL.value: 3,
}

# Default maximum risk level a worker can execute
DEFAULT_MAX_RISK = RiskLevel.HIGH.value


class PermissionMiddleware:
    """
    Middleware that checks tool risk_level vs worker's allowed risk level.

    If the tool's risk exceeds the worker's max allowed risk,
    returns a PermissionDenial as ToolResult.
    """

    def __init__(self, max_risk_level: str = DEFAULT_MAX_RISK):
        self._max_risk_order = _RISK_ORDER.get(max_risk_level, 2)

    async def process(
        self, ctx: ToolCallContext, next_fn: Callable[[], Any]
    ) -> ToolResult:
        """Check risk level and delegate or deny."""
        tool_risk_order = _RISK_ORDER.get(ctx.risk_level, 0)

        if tool_risk_order > self._max_risk_order:
            reason = (
                f"Tool '{ctx.tool_name}' requires risk_level='{ctx.risk_level}' "
                f"but worker max allowed risk is "
                f"'{_order_to_level(self._max_risk_order)}'"
            )
            logger.warning(f"[PermissionMiddleware] Denied: {reason}")
            return ToolResult(content=reason, is_error=True)

        return await next_fn()


def _order_to_level(order: int) -> str:
    """Convert risk order back to level name."""
    for level, o in _RISK_ORDER.items():
        if o == order:
            return level
    return "unknown"
