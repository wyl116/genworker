# edition: baseline
"""Tests for SubAgent collection strategies: fail_fast, best_effort, retry_once."""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from src.events.models import Event
from src.worker.planning.models import SubGoal
from src.worker.planning.subagent.executor import SubAgentExecutor
from src.worker.planning.subagent.models import SubAgentContext


# ---------------------------------------------------------------------------
# Mock implementations
# ---------------------------------------------------------------------------

class MockTaskExecutor:
    """Task executor with per-agent behavior control."""

    def __init__(
        self,
        results: dict[str, str] | None = None,
        errors: dict[str, Exception] | None = None,
        delays: dict[str, float] | None = None,
    ) -> None:
        self._results = results or {}
        self._errors = errors or {}
        self._delays = delays or {}

    async def execute_subagent(self, context: SubAgentContext) -> str:
        agent_id = context.agent_id
        delay = self._delays.get(agent_id, 0.0)
        if delay > 0:
            await asyncio.sleep(delay)
        error = self._errors.get(agent_id)
        if error is not None:
            raise error
        return self._results.get(agent_id, f"done-{agent_id}")


class MockEventBus:
    def __init__(self) -> None:
        self.events: list[Event] = []

    async def publish(self, event: Event) -> int:
        self.events.append(event)
        return 1


def _ctx(agent_id: str, timeout: int = 5) -> SubAgentContext:
    return SubAgentContext(
        agent_id=agent_id,
        parent_worker_id="w-1",
        parent_task_id="t-1",
        sub_goal=SubGoal(id=agent_id, description=f"Goal {agent_id}"),
        timeout_seconds=timeout,
    )


# ---------------------------------------------------------------------------
# Tests: best_effort strategy
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_best_effort_all_succeed():
    """best_effort with all successes returns all results."""
    mock = MockTaskExecutor(results={"a": "r1", "b": "r2", "c": "r3"})
    executor = SubAgentExecutor(mock)

    handles = await executor.spawn_parallel(
        (_ctx("a"), _ctx("b"), _ctx("c"))
    )
    agg = await executor.collect_all(handles, strategy="best_effort")

    assert agg.success_count == 3
    assert agg.failure_count == 0


@pytest.mark.asyncio
async def test_best_effort_partial_failure():
    """best_effort continues past failures and returns all results."""
    mock = MockTaskExecutor(
        results={"a": "ok", "c": "ok"},
        errors={"b": RuntimeError("fail")},
    )
    executor = SubAgentExecutor(mock)

    handles = await executor.spawn_parallel(
        (_ctx("a"), _ctx("b"), _ctx("c"))
    )
    agg = await executor.collect_all(handles, strategy="best_effort")

    assert agg.success_count == 2
    assert agg.failure_count == 1
    assert "ok" in agg.combined_content


# ---------------------------------------------------------------------------
# Tests: fail_fast strategy
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fail_fast_cancels_remaining():
    """fail_fast cancels remaining SubAgents on first failure."""
    mock = MockTaskExecutor(
        errors={"a": RuntimeError("boom")},
        results={"b": "ok", "c": "ok"},
        delays={"b": 0.5, "c": 0.5},
    )
    event_bus = MockEventBus()
    executor = SubAgentExecutor(mock, event_bus=event_bus)

    handles = await executor.spawn_parallel(
        (_ctx("a"), _ctx("b"), _ctx("c"))
    )
    agg = await executor.collect_all(handles, strategy="fail_fast")

    # 'a' failed, 'b' and 'c' should be cancelled
    statuses = {r.agent_id: r.status for r in agg.sub_results}
    assert statuses["a"] == "failure"
    # Remaining are cancelled
    assert statuses["b"] == "cancelled"
    assert statuses["c"] == "cancelled"


@pytest.mark.asyncio
async def test_fail_fast_all_succeed():
    """fail_fast with all successes behaves like best_effort."""
    mock = MockTaskExecutor(results={"a": "r1", "b": "r2"})
    executor = SubAgentExecutor(mock)

    handles = await executor.spawn_parallel((_ctx("a"), _ctx("b")))
    agg = await executor.collect_all(handles, strategy="fail_fast")

    assert agg.success_count == 2
    assert agg.failure_count == 0


# ---------------------------------------------------------------------------
# Tests: retry_once strategy
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_retry_once_marks_error_after_retry():
    """retry_once: failed SubAgent is retried, still fails -> marked error."""
    mock = MockTaskExecutor(
        results={"b": "ok"},
        errors={"a": RuntimeError("fail")},
    )
    executor = SubAgentExecutor(mock)

    handles = await executor.spawn_parallel((_ctx("a"), _ctx("b")))
    agg = await executor.collect_all(handles, strategy="retry_once")

    statuses = {r.agent_id: r.status for r in agg.sub_results}
    assert statuses["a"] == "failure"
    assert "retry" in (
        next(r.error for r in agg.sub_results if r.agent_id == "a") or ""
    ).lower()
    assert statuses["b"] == "success"


@pytest.mark.asyncio
async def test_retry_once_all_succeed():
    """retry_once with all successes returns clean results."""
    mock = MockTaskExecutor(results={"a": "ok", "b": "ok"})
    executor = SubAgentExecutor(mock)

    handles = await executor.spawn_parallel((_ctx("a"), _ctx("b")))
    agg = await executor.collect_all(handles, strategy="retry_once")

    assert agg.success_count == 2
    assert agg.failure_count == 0


# ---------------------------------------------------------------------------
# Tests: invalid strategy
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_invalid_strategy_raises():
    """Invalid strategy name raises ValueError."""
    mock = MockTaskExecutor()
    executor = SubAgentExecutor(mock)

    handles = await executor.spawn_parallel((_ctx("a"),))
    with pytest.raises(ValueError, match="Invalid strategy"):
        await executor.collect_all(handles, strategy="invalid")


# ---------------------------------------------------------------------------
# Tests: memory read-only (SubAgent does not write to host memory)
# ---------------------------------------------------------------------------

def test_subagent_context_memory_is_readonly():
    """SubAgentContext.memory_snapshot is a frozen tuple (read-only)."""
    ctx = _ctx("a")
    assert isinstance(ctx.memory_snapshot, tuple)
    assert isinstance(ctx.rules_snapshot, tuple)
    # Cannot mutate
    with pytest.raises(AttributeError):
        ctx.memory_snapshot = ()  # type: ignore[misc]
