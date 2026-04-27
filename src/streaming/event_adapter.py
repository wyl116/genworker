"""
Compatibility layer for SSE formatting.

`format_sse_line()` and `stream_event_to_sse()` preserve the original legacy
payload shape. New code should use `create_sse_formatter()` and select either
the `ag-ui` or `legacy` protocol explicitly.
"""

from __future__ import annotations

from typing import Any

from .events import StreamEvent
from .protocols import AgUiSseFormatter, BaseSseFormatter, LegacySseFormatter
from .protocols.legacy import stream_event_to_legacy_sse

SUPPORTED_SSE_PROTOCOLS = ("ag-ui", "legacy")


def create_sse_formatter(
    protocol: str = "ag-ui",
    *,
    thread_id: str | None = None,
) -> BaseSseFormatter:
    """Create a request-scoped formatter for the selected SSE protocol."""
    if protocol == "ag-ui":
        return AgUiSseFormatter(thread_id=thread_id)
    if protocol == "legacy":
        return LegacySseFormatter()
    raise ValueError(
        f"Unsupported SSE protocol '{protocol}'. "
        f"Expected one of: {', '.join(SUPPORTED_SSE_PROTOCOLS)}"
    )


def stream_event_to_sse(event: StreamEvent) -> dict[str, Any]:
    """Backward-compatible legacy event JSON conversion."""
    return stream_event_to_legacy_sse(event)


def format_sse_line(event: StreamEvent) -> str:
    """Backward-compatible legacy SSE line formatter."""
    return LegacySseFormatter().format(event)[0]
