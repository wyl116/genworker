"""
LangGraph streaming adapter - for ReactEngine internal use only.

Transforms LangGraph astream_events into StreamEvent frozen dataclasses.
External consumers use event_adapter.py for AG-UI SSE conversion.
"""
from typing import Any, AsyncGenerator

from ..events import (
    ApprovalPendingEvent,
    ErrorEvent,
    RunFinishedEvent,
    RunStartedEvent,
    StreamEvent,
    TextMessageEvent,
    ToolCallEvent,
)

from src.common.logger import get_logger

logger = get_logger()


class LangGraphStreamAdapter:
    """
    LangGraph -> StreamEvent adapter.

    Maps LangGraph astream_events to our frozen dataclass events.
    Used internally by ReactEngine when backed by LangGraph.
    """

    async def adapt(
        self,
        source: Any,
        run_id: str,
    ) -> AsyncGenerator[StreamEvent, None]:
        """
        Adapt a LangGraph astream_events async generator.

        Args:
            source: AsyncGenerator from graph.astream_events().
            run_id: Run identifier.

        Yields:
            StreamEvent frozen dataclasses.
        """
        yield RunStartedEvent(run_id=run_id)

        try:
            async for event in source:
                adapted = self._map_event(event, run_id)
                if adapted is not None:
                    yield adapted
        except Exception as exc:
            logger.error(f"[LangGraphAdapter] Stream error: {exc}", exc_info=True)
            yield ErrorEvent(run_id=run_id, code="STREAM_ERROR", message=str(exc))

        yield RunFinishedEvent(run_id=run_id)

    def _map_event(self, event: dict, run_id: str) -> StreamEvent | None:
        """Map a single LangGraph event to a StreamEvent or None."""
        kind = event.get("event", "")

        if kind == "on_chat_model_stream":
            chunk = event.get("data", {}).get("chunk", None)
            if chunk and hasattr(chunk, "content") and chunk.content:
                return TextMessageEvent(run_id=run_id, content=chunk.content)
        elif kind == "on_chain_stream":
            chunk = event.get("data", {}).get("chunk", None)
            if isinstance(chunk, dict) and "__approval_pending__" in chunk:
                payload = dict(chunk.get("__approval_pending__", {}) or {})
                return ApprovalPendingEvent(
                    run_id=run_id,
                    thread_id=str(payload.get("thread_id", "") or run_id),
                    inbox_id=str(payload.get("inbox_id", "") or ""),
                    prompt=str(payload.get("prompt", "") or ""),
                )

        elif kind == "on_tool_end":
            data = event.get("data", {})
            name = event.get("name", "unknown")
            output = data.get("output", "")
            return ToolCallEvent(
                run_id=run_id,
                tool_name=name,
                tool_result=str(output),
            )

        return None
