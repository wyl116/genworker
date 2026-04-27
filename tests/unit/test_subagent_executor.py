# edition: baseline
"""Tests for SubAgentExecutor - spawn, collect, cancel, strategies."""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from src.events.models import Event
from src.worker.planning.models import SubGoal
from src.worker.planning.subagent.executor import SubAgentExecutor
from src.worker.planning.subagent.models import (
    SubAgentContext,
    SubAgentHandle,
    SubAgentResult,
)


# ---------------------------------------------------------------------------
# Mock task executor
# ---------------------------------------------------------------------------

class MockTaskExecutor:
    """Simulates SubAgent task execution with configurable behavior."""

    def __init__(
        self,
        results: dict[str, str] | None = None,
        delays: dict[str, float] | None = None,
        errors: dict[str, Exception] | None = None,
    ) -> None:
        self._results = results or {}
        self._delays = delays or {}
        self._errors = errors or {}
        self.executed: list[str] = []

    async def execute_subagent(self, context: SubAgentContext) -> str:
        agent_id = context.agent_id
        self.executed.append(agent_id)

        delay = self._delays.get(agent_id, 0.0)
        if delay > 0:
            await asyncio.sleep(delay)

        error = self._errors.get(agent_id)
        if error is not None:
            raise error

        return self._results.get(agent_id, f"result-{agent_id}")


class MockEventBus:
    """Collects published events for assertions."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    async def publish(self, event: Event) -> int:
        self.events.append(event)
        return 1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_context(
    agent_id: str,
    goal_id: str = "",
    timeout: int = 5,
) -> SubAgentContext:
    goal_id = goal_id or agent_id
    return SubAgentContext(
        agent_id=agent_id,
        parent_worker_id="worker-1",
        parent_task_id="task-1",
        sub_goal=SubGoal(id=goal_id, description=f"Goal for {agent_id}"),
        timeout_seconds=timeout,
    )


# ---------------------------------------------------------------------------
# Tests: spawn and collect
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_spawn_and_collect_success():
    """Spawn a SubAgent and collect its result."""
    executor_mock = MockTaskExecutor(results={"sa-1": "hello"})
    executor = SubAgentExecutor(executor_mock)

    ctx = _make_context("sa-1")
    handle = await executor.spawn(ctx)

    assert handle.agent_id == "sa-1"
    assert handle.status == "running"

    result = await executor.collect(handle)

    assert result.status == "success"
    assert result.content == "hello"
    assert result.agent_id == "sa-1"


@pytest.mark.asyncio
async def test_spawn_and_collect_failure():
    """Failed execution produces failure result."""
    executor_mock = MockTaskExecutor(
        errors={"sa-1": RuntimeError("boom")}
    )
    executor = SubAgentExecutor(executor_mock)

    ctx = _make_context("sa-1")
    handle = await executor.spawn(ctx)
    result = await executor.collect(handle)

    assert result.status == "failure"
    assert "boom" in (result.error or "")


@pytest.mark.asyncio
async def test_spawn_and_collect_timeout():
    """SubAgent exceeding timeout produces timeout result."""
    executor_mock = MockTaskExecutor(delays={"sa-1": 10.0})
    executor = SubAgentExecutor(executor_mock)

    ctx = _make_context("sa-1", timeout=1)
    handle = await executor.spawn(ctx)
    result = await executor.collect(handle)

    assert result.status == "timeout"
    assert result.error is not None
    assert "Timed out" in result.error


# ---------------------------------------------------------------------------
# Tests: spawn_parallel with concurrency control
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_spawn_parallel_concurrency_limit():
    """Semaphore limits concurrent spawns."""
    execution_order: list[str] = []

    class OrderTracker:
        async def execute_subagent(self, context: SubAgentContext) -> str:
            execution_order.append(context.agent_id)
            await asyncio.sleep(0.05)
            return f"done-{context.agent_id}"

    executor = SubAgentExecutor(OrderTracker(), max_concurrent_subagents=2)

    contexts = tuple(_make_context(f"sa-{i}") for i in range(4))
    handles = await executor.spawn_parallel(contexts, max_concurrent=2)

    assert len(handles) == 4

    # Collect all
    results = []
    for h in handles:
        results.append(await executor.collect(h))

    assert all(r.status == "success" for r in results)


@pytest.mark.asyncio
async def test_spawn_parallel_respects_max_concurrent():
    """With max_concurrent_subagents=3, the 4th SubAgent waits."""
    concurrency_peak = 0
    current_active = 0
    lock = asyncio.Lock()

    class ConcurrencyTracker:
        async def execute_subagent(self, context: SubAgentContext) -> str:
            nonlocal concurrency_peak, current_active
            async with lock:
                current_active += 1
                if current_active > concurrency_peak:
                    concurrency_peak = current_active
            await asyncio.sleep(0.1)
            async with lock:
                current_active -= 1
            return "done"

    executor = SubAgentExecutor(ConcurrencyTracker(), max_concurrent_subagents=3)

    contexts = tuple(_make_context(f"sa-{i}") for i in range(5))
    handles = await executor.spawn_parallel(contexts, max_concurrent=3)

    for h in handles:
        await executor.collect(h)

    assert concurrency_peak <= 3


# ---------------------------------------------------------------------------
# Tests: cancel
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cancel_running_subagent():
    """Cancelling a running SubAgent sets the cancel flag."""
    executor_mock = MockTaskExecutor(delays={"sa-1": 10.0})
    event_bus = MockEventBus()
    executor = SubAgentExecutor(executor_mock, event_bus=event_bus)

    ctx = _make_context("sa-1", timeout=30)
    handle = await executor.spawn(ctx)

    await asyncio.sleep(0.05)
    await executor.cancel(handle)

    # Give time for cancellation to propagate
    await asyncio.sleep(0.1)

    # Check event bus received failed event
    failed_events = [
        e for e in event_bus.events if e.type == "subagent.failed"
    ]
    assert len(failed_events) >= 1


# ---------------------------------------------------------------------------
# Tests: EventBus integration
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_lifecycle_events_published():
    """SubAgent lifecycle events are published to EventBus."""
    executor_mock = MockTaskExecutor(results={"sa-1": "ok"})
    event_bus = MockEventBus()
    executor = SubAgentExecutor(executor_mock, event_bus=event_bus)

    ctx = _make_context("sa-1")
    handle = await executor.spawn(ctx)
    await executor.collect(handle)

    event_types = [e.type for e in event_bus.events]
    assert "subagent.started" in event_types
    assert "subagent.completed" in event_types


@pytest.mark.asyncio
async def test_failure_event_published():
    """Failed SubAgent publishes subagent.failed event."""
    executor_mock = MockTaskExecutor(errors={"sa-1": ValueError("oops")})
    event_bus = MockEventBus()
    executor = SubAgentExecutor(executor_mock, event_bus=event_bus)

    ctx = _make_context("sa-1")
    handle = await executor.spawn(ctx)
    await executor.collect(handle)

    event_types = [e.type for e in event_bus.events]
    assert "subagent.started" in event_types
    assert "subagent.failed" in event_types


# ---------------------------------------------------------------------------
# Tests: collect with no task
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_collect_unknown_handle():
    """Collecting a handle with no backing task returns failure."""
    executor = SubAgentExecutor(MockTaskExecutor())

    handle = SubAgentHandle(agent_id="unknown", sub_goal_id="sg-1")
    result = await executor.collect(handle)

    assert result.status == "failure"
    assert "No task found" in (result.error or "")


# ---------------------------------------------------------------------------
# Tests: timeout isolation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_timeout_does_not_affect_others():
    """One SubAgent timing out does not affect siblings."""
    executor_mock = MockTaskExecutor(
        results={"sa-2": "ok"},
        delays={"sa-1": 10.0},
    )
    executor = SubAgentExecutor(executor_mock)

    ctx1 = _make_context("sa-1", timeout=1)
    ctx2 = _make_context("sa-2", timeout=5)

    handle1 = await executor.spawn(ctx1)
    handle2 = await executor.spawn(ctx2)

    result2 = await executor.collect(handle2)
    result1 = await executor.collect(handle1)

    assert result1.status == "timeout"
    assert result2.status == "success"
    assert result2.content == "ok"
