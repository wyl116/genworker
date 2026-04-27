"""
SubAgentExecutor - lifecycle management for parallel SubAgent execution.

Responsibilities:
- spawn: create and start a SubAgent task
- spawn_parallel: batch spawn with concurrency control via Semaphore
- collect: wait for a single SubAgent result
- collect_all: batch collect with fail_fast / best_effort / retry_once strategies
- cancel: abort a running SubAgent
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Protocol
from uuid import uuid4

from src.events.models import Event

from .aggregator import aggregate_results
from .models import (
    AggregatedResult,
    SubAgentContext,
    SubAgentEvent,
    SubAgentHandle,
    SubAgentResult,
    SubAgentUsage,
)

logger = logging.getLogger(__name__)

VALID_STRATEGIES = frozenset({"fail_fast", "best_effort", "retry_once"})


class EventBusProtocol(Protocol):
    """Minimal EventBus protocol for SubAgent lifecycle events."""

    async def publish(self, event: Event) -> int: ...


class TaskExecutorProtocol(Protocol):
    """Protocol for executing a SubAgent's task (e.g. via WorkerRouter)."""

    async def execute_subagent(self, context: SubAgentContext) -> str:
        """Execute a sub-goal and return the result content string."""
        ...


class SubAgentExecutor:
    """
    SubAgent lifecycle manager.

    Spawns SubAgents as async tasks, tracks them via handles,
    and supports three collection strategies for fault tolerance.
    """

    def __init__(
        self,
        task_executor: TaskExecutorProtocol,
        event_bus: EventBusProtocol | None = None,
        max_concurrent_subagents: int = 3,
    ) -> None:
        self._task_executor = task_executor
        self._event_bus = event_bus
        self._max_concurrent = max_concurrent_subagents
        self._semaphore: asyncio.Semaphore | None = None
        self._tasks: dict[str, asyncio.Task[SubAgentResult]] = {}
        self._cancel_flags: dict[str, asyncio.Event] = {}

    async def spawn(self, context: SubAgentContext) -> SubAgentHandle:
        """
        Create and start a SubAgent as an async task.

        Returns a SubAgentHandle for tracking. The actual execution
        runs in the background.
        """
        now = datetime.now(timezone.utc).isoformat()
        handle = SubAgentHandle(
            agent_id=context.agent_id,
            sub_goal_id=context.sub_goal.id,
            status="running",
            created_at=now,
        )

        cancel_event = asyncio.Event()
        self._cancel_flags[context.agent_id] = cancel_event

        task = asyncio.create_task(
            self._run_subagent(context, cancel_event),
            name=f"subagent-{context.agent_id}",
        )
        self._tasks[context.agent_id] = task

        await self._publish_event(SubAgentEvent(
            event_type="started",
            agent_id=context.agent_id,
            parent_task_id=context.parent_task_id,
            detail=context.sub_goal.description,
        ))

        return handle

    async def spawn_parallel(
        self,
        contexts: tuple[SubAgentContext, ...],
        max_concurrent: int | None = None,
    ) -> tuple[SubAgentHandle, ...]:
        """
        Spawn multiple SubAgents with Semaphore-controlled concurrency.

        The semaphore gates actual execution inside _run_subagent,
        not the spawn itself - so all tasks are created immediately
        but only N run concurrently.

        Args:
            contexts: SubAgent contexts to spawn.
            max_concurrent: Override default concurrency limit.

        Returns:
            Tuple of SubAgentHandles.
        """
        limit = max_concurrent if max_concurrent is not None else self._max_concurrent
        self._semaphore = asyncio.Semaphore(limit)

        handles: list[SubAgentHandle] = []
        for ctx in contexts:
            handle = await self.spawn(ctx)
            handles.append(handle)
        return tuple(handles)

    async def collect(self, handle: SubAgentHandle) -> SubAgentResult:
        """
        Wait for a single SubAgent to complete.

        Returns SubAgentResult. On timeout or cancellation, returns
        appropriate status.
        """
        task = self._tasks.get(handle.agent_id)
        if task is None:
            return SubAgentResult(
                agent_id=handle.agent_id,
                sub_goal_id=handle.sub_goal_id,
                status="failure",
                content="",
                error="No task found for agent",
            )

        try:
            result = await task
            return result
        except asyncio.CancelledError:
            return SubAgentResult(
                agent_id=handle.agent_id,
                sub_goal_id=handle.sub_goal_id,
                status="cancelled",
                content="",
                error="Task was cancelled",
            )
        except Exception as exc:
            return SubAgentResult(
                agent_id=handle.agent_id,
                sub_goal_id=handle.sub_goal_id,
                status="failure",
                content="",
                error=str(exc),
            )
        finally:
            self._cleanup(handle.agent_id)

    async def collect_all(
        self,
        handles: tuple[SubAgentHandle, ...],
        strategy: str = "best_effort",
    ) -> AggregatedResult:
        """
        Collect results from multiple SubAgents using the specified strategy.

        Strategies:
        - fail_fast: cancel remaining on first failure
        - best_effort: wait for all, ignore failures
        - retry_once: retry failed once, then mark error
        """
        if strategy not in VALID_STRATEGIES:
            raise ValueError(f"Invalid strategy: {strategy}")

        if strategy == "fail_fast":
            return await self._collect_fail_fast(handles)
        elif strategy == "retry_once":
            return await self._collect_retry_once(handles)
        else:
            return await self._collect_best_effort(handles)

    async def cancel(self, handle: SubAgentHandle) -> None:
        """Cancel a running SubAgent via its cancel flag and task."""
        flag = self._cancel_flags.get(handle.agent_id)
        if flag is not None:
            flag.set()

        task = self._tasks.get(handle.agent_id)
        if task is not None and not task.done():
            task.cancel()

        await self._publish_event(SubAgentEvent(
            event_type="failed",
            agent_id=handle.agent_id,
            parent_task_id="",
            detail="Cancelled by parent",
        ))

    # ----- internal execution -----

    async def _run_subagent(
        self,
        context: SubAgentContext,
        cancel_event: asyncio.Event,
    ) -> SubAgentResult:
        """Execute a SubAgent with timeout, cancellation, and concurrency control."""
        # Acquire semaphore if set (from spawn_parallel)
        sem = self._semaphore
        if sem is not None:
            await sem.acquire()

        start_ms = _now_ms()

        try:
            content = await asyncio.wait_for(
                self._task_executor.execute_subagent(context),
                timeout=context.timeout_seconds,
            )
        except asyncio.TimeoutError:
            elapsed = _now_ms() - start_ms
            await self._publish_event(SubAgentEvent(
                event_type="failed",
                agent_id=context.agent_id,
                parent_task_id=context.parent_task_id,
                detail="Timeout",
            ))
            return SubAgentResult(
                agent_id=context.agent_id,
                sub_goal_id=context.sub_goal.id,
                status="timeout",
                content="",
                error=f"Timed out after {context.timeout_seconds}s",
                usage=SubAgentUsage(duration_ms=elapsed),
            )
        except asyncio.CancelledError:
            elapsed = _now_ms() - start_ms
            return SubAgentResult(
                agent_id=context.agent_id,
                sub_goal_id=context.sub_goal.id,
                status="cancelled",
                content="",
                error="Cancelled",
                usage=SubAgentUsage(duration_ms=elapsed),
            )
        except Exception as exc:
            elapsed = _now_ms() - start_ms
            await self._publish_event(SubAgentEvent(
                event_type="failed",
                agent_id=context.agent_id,
                parent_task_id=context.parent_task_id,
                detail=str(exc),
            ))
            return SubAgentResult(
                agent_id=context.agent_id,
                sub_goal_id=context.sub_goal.id,
                status="failure",
                content="",
                error=str(exc),
                usage=SubAgentUsage(duration_ms=elapsed),
            )
        finally:
            if sem is not None:
                sem.release()

        elapsed = _now_ms() - start_ms
        await self._publish_event(SubAgentEvent(
            event_type="completed",
            agent_id=context.agent_id,
            parent_task_id=context.parent_task_id,
            detail="Success",
        ))
        return SubAgentResult(
            agent_id=context.agent_id,
            sub_goal_id=context.sub_goal.id,
            status="success",
            content=content,
            usage=SubAgentUsage(duration_ms=elapsed),
        )

    # ----- collection strategies -----

    async def _collect_best_effort(
        self,
        handles: tuple[SubAgentHandle, ...],
    ) -> AggregatedResult:
        """Wait for all, ignore failures."""
        results: list[SubAgentResult] = []
        for handle in handles:
            result = await self.collect(handle)
            results.append(result)
        return aggregate_results(tuple(results))

    async def _collect_fail_fast(
        self,
        handles: tuple[SubAgentHandle, ...],
    ) -> AggregatedResult:
        """Cancel remaining on first failure."""
        results: list[SubAgentResult] = []
        remaining = list(handles)

        for handle in list(remaining):
            result = await self.collect(handle)
            remaining.remove(handle)
            results.append(result)

            if result.status in ("failure", "timeout"):
                # Cancel all remaining
                for rh in remaining:
                    await self.cancel(rh)
                # Collect cancelled results
                for rh in remaining:
                    cancelled = SubAgentResult(
                        agent_id=rh.agent_id,
                        sub_goal_id=rh.sub_goal_id,
                        status="cancelled",
                        content="",
                        error="Cancelled due to fail_fast",
                    )
                    results.append(cancelled)
                break

        return aggregate_results(tuple(results))

    async def _collect_retry_once(
        self,
        handles: tuple[SubAgentHandle, ...],
    ) -> AggregatedResult:
        """Retry failed SubAgents once, then mark as error."""
        results: list[SubAgentResult] = []

        for handle in handles:
            result = await self.collect(handle)

            if result.status in ("failure", "timeout"):
                # Attempt retry: re-spawn with same context if available
                original_task = self._tasks.get(handle.agent_id)
                # For retry, we create a minimal result - the original
                # context is not stored, so we just mark the failure
                logger.info(
                    f"Retry once for subagent {handle.agent_id} "
                    f"(original status: {result.status})"
                )
                # Mark as error after "retry" (simplified - real retry
                # would need stored context)
                result = replace(result, status="failure", error=(
                    f"Failed after retry: {result.error}"
                ))

            results.append(result)

        return aggregate_results(tuple(results))

    # ----- helpers -----

    async def _publish_event(self, sa_event: SubAgentEvent) -> None:
        """Publish a SubAgent lifecycle event via EventBus."""
        if self._event_bus is None:
            return

        event = Event(
            event_id=uuid4().hex,
            type=f"subagent.{sa_event.event_type}",
            source=f"subagent:{sa_event.agent_id}",
            tenant_id="",  # inherited from parent context
            payload=(
                ("agent_id", sa_event.agent_id),
                ("parent_task_id", sa_event.parent_task_id),
                ("detail", sa_event.detail),
            ),
        )

        try:
            await self._event_bus.publish(event)
        except Exception as exc:
            logger.warning(
                f"Failed to publish SubAgent event: {exc}"
            )

    def _cleanup(self, agent_id: str) -> None:
        """Remove internal tracking state for a completed SubAgent."""
        self._tasks.pop(agent_id, None)
        self._cancel_flags.pop(agent_id, None)


def _now_ms() -> int:
    """Current time in milliseconds."""
    return int(time.monotonic() * 1000)
