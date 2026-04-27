"""
Sanitize middleware - cleans sensitive data from tool output.
"""
import re
from typing import Any, Callable

from src.common.logger import get_logger

from ..formatters import ToolResult
from ..pipeline import ToolCallContext

logger = get_logger()

# Patterns to redact from output
_SENSITIVE_PATTERNS: tuple[tuple[str, str], ...] = (
    # API keys, tokens
    (r'(?i)(api[_-]?key|token|secret|password|credentials?)\s*[:=]\s*\S+',
     r'\1=[REDACTED]'),
    # Bearer tokens
    (r'(?i)Bearer\s+\S+', 'Bearer [REDACTED]'),
    # Connection strings with passwords
    (r'(?i)://[^:]+:[^@]+@', '://[REDACTED]@'),
)

_COMPILED_PATTERNS: tuple[tuple[re.Pattern, str], ...] = tuple(
    (re.compile(pattern), replacement)
    for pattern, replacement in _SENSITIVE_PATTERNS
)


class SanitizeMiddleware:
    """
    Sanitizes tool output to remove sensitive information.

    Runs after execution, redacting patterns that look like
    credentials, tokens, or connection strings.
    """

    def __init__(self, extra_patterns: tuple[tuple[str, str], ...] = ()):
        compiled_extra = tuple(
            (re.compile(p), r) for p, r in extra_patterns
        )
        self._patterns = _COMPILED_PATTERNS + compiled_extra

    async def process(
        self, ctx: ToolCallContext, next_fn: Callable[[], Any]
    ) -> ToolResult:
        """Execute downstream and sanitize the result content."""
        result: ToolResult = await next_fn()
        sanitized_content = self._sanitize(result.content)

        if sanitized_content != result.content:
            logger.debug(
                f"[SanitizeMiddleware] Redacted sensitive data in "
                f"tool '{ctx.tool_name}' output"
            )
            return ToolResult(
                content=sanitized_content,
                is_error=result.is_error,
                truncated=result.truncated,
                original_length=result.original_length,
                metadata=result.metadata,
            )

        return result

    def _sanitize(self, content: str) -> str:
        """Apply all sanitization patterns to content."""
        result = content
        for pattern, replacement in self._patterns:
            result = pattern.sub(replacement, result)
        return result
