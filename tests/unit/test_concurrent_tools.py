# edition: baseline
"""
Unit tests for concurrent tool execution in ReactEngine.

Tests cover _partition_tool_calls batching logic, _execute_tool_batch
concurrency, and full ReactEngine with mixed READ/WRITE tool calls.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from src.engine.protocols import (
    LLMResponse,
    ToolCall,
    ToolResult,
    UsageInfo,
)
from src.engine.react.agent import (
    ReactEngine,
    _execute_tool_batch,
    _partition_tool_calls,
)
from src.streaming.events import (
    RunFinishedEvent,
    RunStartedEvent,
    TextMessageEvent,
    ToolCallEvent,
)
from src.tools.mcp.server import MCPServer
from src.tools.mcp.tool import Tool
from src.tools.mcp.types import MCPCategory, RiskLevel, ToolType


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_tool(name: str, tool_type: ToolType = ToolType.READ) -> Tool:
    """Create a minimal Tool for registration."""
    return Tool(
        name=name,
        description=f"Test tool {name}",
        handler=AsyncMock(return_value="ok"),
        parameters={},
        required_params=(),
        tool_type=tool_type,
        category=MCPCategory.GLOBAL,
        risk_level=RiskLevel.LOW,
        tags=frozenset(),
    )


def _make_server(*tools: Tool) -> MCPServer:
    """Create an MCPServer with registered tools."""
    server = MCPServer(name="test-server")
    for tool in tools:
        server.register_tool(tool)
    return server


class MockLLMClient:
    """LLMClient mock returning pre-configured responses."""

    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)
        self._call_count = 0

    async def invoke(
        self,
        messages,
        tools=None,
        tool_choice=None,
        system_blocks=None,
        intent=None,
    ) -> LLMResponse:
        if self._call_count < len(self._responses):
            resp = self._responses[self._call_count]
            self._call_count += 1
            return resp
        return LLMResponse(content="default response")


class MockToolExecutor:
    """ToolExecutor mock that records calls."""

    def __init__(self, results: dict[str, ToolResult] | None = None) -> None:
        self._results = results or {}
        self.calls: list[tuple[str, dict]] = []

    async def execute(self, tool_name: str, tool_input: dict) -> ToolResult:
        self.calls.append((tool_name, tool_input))
        return self._results.get(
            tool_name, ToolResult(content=f"result of {tool_name}"),
        )


class TimingToolExecutor:
    """ToolExecutor that records start/end times for concurrency verification."""

    def __init__(self, delay: float = 0.05) -> None:
        self._delay = delay
        self.timeline: list[tuple[str, str, float]] = []

    async def execute(self, tool_name: str, tool_input: dict) -> ToolResult:
        start = asyncio.get_event_loop().time()
        self.timeline.append((tool_name, "start", start))
        await asyncio.sleep(self._delay)
        end = asyncio.get_event_loop().time()
        self.timeline.append((tool_name, "end", end))
        return ToolResult(content=f"result of {tool_name}")


async def _collect_events(engine: ReactEngine, **kwargs) -> list:
    events = []
    async for event in engine.execute(**kwargs):
        events.append(event)
    return events


# ---------------------------------------------------------------------------
# _partition_tool_calls tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_partition_groups_consecutive_read_tools():
    """Consecutive READ tools are grouped into a single batch."""
    server = _make_server(
        _make_tool("read_a", ToolType.READ),
        _make_tool("read_b", ToolType.SEARCH),
        _make_tool("read_c", ToolType.READ),
    )
    calls = [
        ToolCall(tool_name="read_a", tool_input={}, tool_call_id="tc1"),
        ToolCall(tool_name="read_b", tool_input={}, tool_call_id="tc2"),
        ToolCall(tool_name="read_c", tool_input={}, tool_call_id="tc3"),
    ]

    batches = _partition_tool_calls(calls, server)

    # All three are concurrent-safe, so they should be in one batch
    assert len(batches) == 1
    assert len(batches[0]) == 3


@pytest.mark.asyncio
async def test_partition_separates_write_tools():
    """WRITE tools each get their own single-item batch."""
    server = _make_server(
        _make_tool("write_a", ToolType.WRITE),
        _make_tool("write_b", ToolType.WRITE),
    )
    calls = [
        ToolCall(tool_name="write_a", tool_input={}, tool_call_id="tc1"),
        ToolCall(tool_name="write_b", tool_input={}, tool_call_id="tc2"),
    ]

    batches = _partition_tool_calls(calls, server)

    assert len(batches) == 2
    assert len(batches[0]) == 1
    assert len(batches[1]) == 1
    assert batches[0][0].tool_name == "write_a"
    assert batches[1][0].tool_name == "write_b"


@pytest.mark.asyncio
async def test_partition_mixed_read_write():
    """Mixed READ and WRITE tools are partitioned correctly."""
    server = _make_server(
        _make_tool("read_a", ToolType.READ),
        _make_tool("read_b", ToolType.SEARCH),
        _make_tool("write_c", ToolType.WRITE),
        _make_tool("read_d", ToolType.READ),
    )
    calls = [
        ToolCall(tool_name="read_a", tool_input={}, tool_call_id="tc1"),
        ToolCall(tool_name="read_b", tool_input={}, tool_call_id="tc2"),
        ToolCall(tool_name="write_c", tool_input={}, tool_call_id="tc3"),
        ToolCall(tool_name="read_d", tool_input={}, tool_call_id="tc4"),
    ]

    batches = _partition_tool_calls(calls, server)

    # [read_a, read_b], [write_c], [read_d]
    assert len(batches) == 3
    assert len(batches[0]) == 2  # read_a + read_b grouped
    assert len(batches[1]) == 1  # write_c alone
    assert len(batches[2]) == 1  # read_d alone (new group after write)


@pytest.mark.asyncio
async def test_partition_no_mcp_server_all_sequential():
    """Without an mcp_server, all calls fall back to sequential (one per batch)."""
    calls = [
        ToolCall(tool_name="tool_a", tool_input={}, tool_call_id="tc1"),
        ToolCall(tool_name="tool_b", tool_input={}, tool_call_id="tc2"),
        ToolCall(tool_name="tool_c", tool_input={}, tool_call_id="tc3"),
    ]

    batches = _partition_tool_calls(calls, None)

    assert len(batches) == 3
    assert all(len(b) == 1 for b in batches)


@pytest.mark.asyncio
async def test_partition_empty_list():
    """Empty tool_calls list returns empty batches."""
    batches = _partition_tool_calls([], None)
    assert batches == []


# ---------------------------------------------------------------------------
# _execute_tool_batch tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_execute_batch_single_item():
    """Single-item batch runs directly (not via gather)."""
    executor = MockToolExecutor({"tool_a": ToolResult(content="result_a")})
    batch = [ToolCall(tool_name="tool_a", tool_input={"x": 1}, tool_call_id="tc1")]

    results = await _execute_tool_batch(batch, executor)

    assert len(results) == 1
    assert results[0].content == "result_a"
    assert executor.calls == [("tool_a", {"x": 1})]


@pytest.mark.asyncio
async def test_execute_batch_multiple_runs_concurrently():
    """Multi-item batch runs tools concurrently via asyncio.gather."""
    executor = TimingToolExecutor(delay=0.05)
    batch = [
        ToolCall(tool_name="tool_a", tool_input={}, tool_call_id="tc1"),
        ToolCall(tool_name="tool_b", tool_input={}, tool_call_id="tc2"),
        ToolCall(tool_name="tool_c", tool_input={}, tool_call_id="tc3"),
    ]

    results = await _execute_tool_batch(batch, executor)

    assert len(results) == 3

    # Verify concurrency: all starts should happen before any end
    starts = [t for name, event, t in executor.timeline if event == "start"]
    ends = [t for name, event, t in executor.timeline if event == "end"]
    # If truly concurrent, the latest start should be before the earliest end
    assert max(starts) < min(ends), (
        "Tools did not run concurrently - starts should overlap with execution"
    )


@pytest.mark.asyncio
async def test_execute_batch_error_handling():
    """Tool execution error is captured in ToolResult, not raised."""

    class FailingExecutor:
        async def execute(self, tool_name: str, tool_input: dict) -> ToolResult:
            raise RuntimeError(f"{tool_name} exploded")

    batch = [ToolCall(tool_name="bad_tool", tool_input={}, tool_call_id="tc1")]

    results = await _execute_tool_batch(batch, FailingExecutor())

    assert len(results) == 1
    assert results[0].is_error is True
    assert "exploded" in results[0].content


# ---------------------------------------------------------------------------
# Full ReactEngine with concurrent tool calls
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_react_engine_concurrent_tool_calls():
    """ReactEngine with mcp_server batches READ tools and serializes WRITE tools."""
    server = _make_server(
        _make_tool("search_a", ToolType.SEARCH),
        _make_tool("search_b", ToolType.SEARCH),
        _make_tool("write_file", ToolType.WRITE),
    )

    llm = MockLLMClient([
        # Round 1: LLM requests two search tools + one write tool
        LLMResponse(
            content="",
            tool_calls=(
                ToolCall(tool_name="search_a", tool_input={"q": "x"}, tool_call_id="tc1"),
                ToolCall(tool_name="search_b", tool_input={"q": "y"}, tool_call_id="tc2"),
                ToolCall(tool_name="write_file", tool_input={"path": "a.txt"}, tool_call_id="tc3"),
            ),
            usage=UsageInfo(total_tokens=20),
        ),
        # Round 2: final text
        LLMResponse(content="All done.", usage=UsageInfo(total_tokens=10)),
    ])

    executor = MockToolExecutor({
        "search_a": ToolResult(content="result_a"),
        "search_b": ToolResult(content="result_b"),
        "write_file": ToolResult(content="written"),
    })

    engine = ReactEngine(
        llm_client=llm,
        tool_executor=executor,
        mcp_server=server,
    )

    events = await _collect_events(
        engine, system_prompt="sys", task="do work",
    )

    # All three tool calls executed
    tool_events = [e for e in events if isinstance(e, ToolCallEvent)]
    assert len(tool_events) == 3
    tool_names = [e.tool_name for e in tool_events]
    assert "search_a" in tool_names
    assert "search_b" in tool_names
    assert "write_file" in tool_names

    # Final text returned
    text_events = [e for e in events if isinstance(e, TextMessageEvent)]
    assert text_events[0].content == "All done."

    # Finished successfully
    finish = [e for e in events if isinstance(e, RunFinishedEvent)][0]
    assert finish.success is True


@pytest.mark.asyncio
async def test_react_engine_no_mcp_server_sequential():
    """Without mcp_server, all tool calls run sequentially."""
    llm = MockLLMClient([
        LLMResponse(
            content="",
            tool_calls=(
                ToolCall(tool_name="tool_a", tool_input={}, tool_call_id="tc1"),
                ToolCall(tool_name="tool_b", tool_input={}, tool_call_id="tc2"),
            ),
            usage=UsageInfo(total_tokens=15),
        ),
        LLMResponse(content="Done.", usage=UsageInfo(total_tokens=10)),
    ])

    executor = MockToolExecutor()
    engine = ReactEngine(
        llm_client=llm,
        tool_executor=executor,
        mcp_server=None,
    )

    events = await _collect_events(
        engine, system_prompt="sys", task="test",
    )

    tool_events = [e for e in events if isinstance(e, ToolCallEvent)]
    assert len(tool_events) == 2

    # Both tools were called
    assert executor.calls == [
        ("tool_a", {}),
        ("tool_b", {}),
    ]
