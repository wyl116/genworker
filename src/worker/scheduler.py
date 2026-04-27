"""
WorkerScheduler - worker-level task scheduler.

Manages:
- Concurrent task limiting
- Daily task quota enforcement
- Priority-based queuing
"""
import asyncio
import inspect
import logging
from dataclasses import dataclass
from datetime import date

from src.events.models import EventBusProtocol
from src.worker.scheduler_effects import SchedulerSideEffects

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SchedulerConfig:
    """Configuration for the WorkerScheduler."""
    max_concurrent_tasks: int = 5
    daily_task_quota: int = 100
    goal_check_enabled: bool = True
    max_task_retries: int = 2


@dataclass(frozen=True)
class TriggerPriority:
    """Priority levels for different trigger sources (lower = higher priority)."""
    DUTY: int = 10           # Duty triggers are highest priority
    PERSONA_TRIGGER: int = 20  # PERSONA.md triggers are next
    GOAL: int = 30           # Goal-driven tasks are lowest priority


class QuotaExhaustedError(Exception):
    """Raised when daily task quota is exhausted."""
    pass


class WorkerScheduler:
    """
    Worker-level scheduler: concurrent limiting + quota management + priority queue.

    Tasks beyond max_concurrent_tasks are queued by priority.
    Tasks beyond daily_task_quota are rejected.
    """

    def __init__(
        self,
        config: SchedulerConfig,
        worker_router=None,  # WorkerRouter (optional, for execution)
        event_bus: EventBusProtocol | None = None,
        dead_letter_store=None,
        engine_dispatcher=None,
        inbox_store=None,
    ) -> None:
        self._config = config
        self._worker_router = worker_router
        self._side_effects = SchedulerSideEffects(
            event_bus=event_bus,
            dead_letter_store=dead_letter_store,
        )
        self._engine_dispatcher = engine_dispatcher
        self._inbox_store = inbox_store
        self._active_count = 0
        self._daily_count = 0
        self._daily_count_date: date | None = None
        self._queue: list[tuple[int, asyncio.Future, dict]] = []  # (priority, future, job)
        self._lock = asyncio.Lock()

    def replace_worker_router(self, worker_router) -> None:
        """Refresh the router used for scheduled task execution."""
        self._worker_router = worker_router

    def replace_engine_dispatcher(self, engine_dispatcher) -> None:
        """Refresh the engine dispatcher used for langgraph resume jobs."""
        self._engine_dispatcher = engine_dispatcher

    def replace_inbox_store(self, inbox_store) -> None:
        """Refresh the inbox store used by langgraph resume jobs."""
        self._inbox_store = inbox_store

    async def submit_task(
        self,
        job: dict,
        priority: int,
    ) -> bool:
        """
        Submit a task for execution.

        Flow:
        1. Check daily quota (reject if exceeded)
        2. Check concurrent limit (queue if exceeded)
        3. Execute or enqueue based on capacity

        Returns True if task was accepted (executing or queued),
        False if rejected (quota exhausted).
        """
        async with self._lock:
            self._refresh_daily_counter()

            if self._daily_count >= self._config.daily_task_quota:
                logger.warning(
                    f"[WorkerScheduler] Daily quota exhausted "
                    f"({self._daily_count}/{self._config.daily_task_quota})"
                )
                return False

            self._daily_count += 1

            if self._active_count < self._config.max_concurrent_tasks:
                self._active_count += 1
                asyncio.create_task(self._run_task(job, priority))
                return True

            # Queue the task
            future: asyncio.Future = asyncio.get_event_loop().create_future()
            self._queue.append((priority, future, job))
            self._queue.sort(key=lambda x: x[0])
            logger.info(
                f"[WorkerScheduler] Task queued (priority={priority}, "
                f"queue_size={len(self._queue)})"
            )
            return True

    async def submit_langgraph_resume(
        self,
        payload: dict,
        *,
        priority: int,
    ) -> bool:
        """Submit a langgraph resume job onto the worker scheduler queue."""
        async def _runner():
            engine = getattr(self._engine_dispatcher, "langgraph_engine", None)
            if engine is None:
                raise RuntimeError("LangGraph engine is not configured")
            inbox_id = str(payload.get("inbox_id", "") or "")
            resume_failed = False
            failure_reason = ""
            try:
                async for event in engine.resume(
                    tenant_id=str(payload.get("tenant_id", "") or ""),
                    worker_id=str(payload.get("worker_id", "") or ""),
                    thread_id=str(payload.get("thread_id", "") or ""),
                    skill_id=str(payload.get("skill_id", "") or ""),
                    decision=dict(payload.get("decision", {}) or {}),
                    expected_digest=str(payload.get("expected_digest", "") or ""),
                    inbox_id=inbox_id,
                ):
                    event_type_raw = getattr(event, "event_type", "") or ""
                    event_type = str(getattr(event_type_raw, "value", event_type_raw) or "")
                    if event_type == "ERROR":
                        resume_failed = True
                        failure_reason = str(
                            getattr(event, "message", "") or getattr(event, "code", "") or "resume_failed"
                        )
                    elif event_type == "RUN_FINISHED" and not getattr(event, "success", True):
                        resume_failed = True
                        if not failure_reason:
                            failure_reason = str(getattr(event, "stop_reason", "") or "run_failed")
            except Exception as exc:
                if self._inbox_store is not None and inbox_id:
                    await self._inbox_store.mark_error(
                        inbox_id,
                        tenant_id=str(payload.get("tenant_id", "") or ""),
                        worker_id=str(payload.get("worker_id", "") or ""),
                        reason=str(exc),
                    )
                raise
            if resume_failed:
                if self._inbox_store is not None and inbox_id:
                    await self._inbox_store.mark_error(
                        inbox_id,
                        tenant_id=str(payload.get("tenant_id", "") or ""),
                        worker_id=str(payload.get("worker_id", "") or ""),
                        reason=failure_reason or "resume_failed",
                    )
                return {"success": True, "error": failure_reason or "resume_failed"}
            if self._inbox_store is not None and inbox_id:
                await self._inbox_store.mark_consumed(
                    [inbox_id],
                    tenant_id=str(payload.get("tenant_id", "") or ""),
                    worker_id=str(payload.get("worker_id", "") or ""),
                )
            return {"success": True}

        return await self.submit_task(
            {
                "task": f"langgraph-resume:{payload.get('thread_id', '')}",
                "tenant_id": str(payload.get("tenant_id", "") or ""),
                "worker_id": str(payload.get("worker_id", "") or ""),
                "runner": _runner,
            },
            priority=priority,
        )

    async def _run_task(self, job: dict, priority: int) -> None:
        """Execute a task and consume the next queued task on completion."""
        try:
            result = await self._execute_job(job)
            if result.get("success", True):
                await self._resolve_completion(job, result)
                return
            await self._handle_task_failure(
                job,
                priority,
                result.get("error", "task returned failure"),
                result,
            )
        except Exception as exc:
            logger.error(f"[WorkerScheduler] Task execution failed: {exc}")
            await self._handle_task_failure(job, priority, str(exc))
        finally:
            await self._task_completed()

    async def _execute_job(self, job: dict) -> dict:
        """Execute a scheduled job and collect a minimal result."""
        runner = job.get("runner")
        if runner is not None:
            outcome = runner()
            if inspect.isawaitable(outcome):
                result = await outcome
            else:
                result = outcome
            return result or {"success": True}

        task_desc = job.get("task", "")
        tenant_id = job.get("tenant_id", "")
        worker_id = job.get("worker_id")
        manifest = job.get("manifest")

        if self._worker_router is None:
            raise RuntimeError("Worker router is not configured")

        content_parts: list[str] = []
        error_message = ""
        success = True
        run_id = ""

        route_kwargs = {
            "task": task_desc,
            "tenant_id": tenant_id,
            "worker_id": worker_id,
        }
        preferred_skill_ids = tuple(
            str(item).strip()
            for item in (job.get("preferred_skill_ids") or ())
            if str(item).strip()
        )
        if preferred_skill_ids:
            route_kwargs["preferred_skill_ids"] = preferred_skill_ids
        if manifest is not None:
            route_kwargs["manifest"] = manifest

        async for event in self._worker_router.route_stream(**route_kwargs):
            run_id = getattr(event, "run_id", run_id)
            content = getattr(event, "content", "")
            if content:
                content_parts.append(content)
            if getattr(event, "event_type", "") == "ERROR":
                success = False
                error_message = getattr(event, "message", "unknown error")
            if getattr(event, "event_type", "") == "RUN_FINISHED" and not getattr(
                event, "success", True,
            ):
                success = False
                error_message = getattr(event, "stop_reason", "run_failed")

        return {
            "success": success,
            "content": "".join(content_parts).strip(),
            "error": error_message,
            "run_id": run_id,
            "task_id": getattr(manifest, "task_id", ""),
        }

    async def _resolve_completion(self, job: dict, result: dict) -> None:
        """Resolve completion futures and publish task.failed when needed."""
        await self._side_effects.resolve_completion(job, result)

    async def _handle_task_failure(
        self,
        job: dict,
        priority: int,
        error_message: str,
        result: dict | None = None,
    ) -> None:
        """Retry transient failures, then dead-letter exhausted jobs."""
        retry_count = int(job.get("retry_count", 0) or 0)
        max_retries = max(self._config.max_task_retries, 0)

        if retry_count < max_retries:
            retried_job = {**job, "retry_count": retry_count + 1}
            async with self._lock:
                self._queue.append((priority, None, retried_job))
                self._queue.sort(key=lambda item: item[0])
            logger.warning(
                "[WorkerScheduler] Task retry %d/%d: %s",
                retry_count + 1,
                max_retries,
                error_message,
            )
            return

        failure_result = result or {"success": False, "error": error_message}
        await self._resolve_completion(job, failure_result)
        await self._side_effects.send_to_dead_letter(job, error_message, retry_count + 1)
        await self._side_effects.publish_dead_lettered(job, error_message, retry_count + 1)

    async def _task_completed(self) -> None:
        """Handle task completion: dequeue next task if any."""
        async with self._lock:
            self._active_count -= 1

            if self._queue:
                priority, future, job = self._queue.pop(0)
                self._active_count += 1
                asyncio.create_task(self._run_task(job, priority))
                logger.debug(
                    f"[WorkerScheduler] Dequeued task (priority={priority})"
                )

    def _refresh_daily_counter(self) -> None:
        """Reset daily counter if the date has changed."""
        today = date.today()
        if self._daily_count_date != today:
            self._daily_count = 0
            self._daily_count_date = today

    @property
    def active_count(self) -> int:
        """Number of currently executing tasks."""
        return self._active_count

    @property
    def queue_size(self) -> int:
        """Number of tasks waiting in the queue."""
        return len(self._queue)

    @property
    def daily_count(self) -> int:
        """Number of tasks submitted today."""
        return self._daily_count

    @property
    def config(self) -> SchedulerConfig:
        """Current scheduler configuration."""
        return self._config
