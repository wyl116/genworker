"""Side-effect helpers for WorkerScheduler completion and failure handling."""
from __future__ import annotations

from uuid import uuid4

from src.common.time import utc_now_iso
from src.events.models import Event
from src.worker.dead_letter import DeadLetterEntry


class SchedulerSideEffects:
    """Handle event publication and dead-letter persistence for scheduled jobs."""

    def __init__(self, event_bus=None, dead_letter_store=None) -> None:
        self._event_bus = event_bus
        self._dead_letter_store = dead_letter_store

    async def resolve_completion(self, job: dict, result: dict) -> None:
        """Resolve completion futures and publish terminal events."""
        completion_future = job.get("completion_future")
        if completion_future is not None and not completion_future.done():
            completion_future.set_result(result)

        if result.get("success", True):
            await self.publish_task_completed(job, result)
            await self.publish_isolated_run_completed(job, result)
            return

        error_message = result.get("error", "scheduled task failed")
        await self.publish_isolated_run_failed(job=job, error_message=error_message)
        await self.publish_task_failed(
            job=job,
            error_message=error_message,
            error_code="TASK_FAILED",
        )

    async def publish_task_failed(
        self,
        job: dict,
        error_message: str,
        error_code: str,
    ) -> None:
        """Publish task.failed when session context is available."""
        if self._event_bus is None:
            return

        session_id = str(job.get("session_id", "") or "")
        thread_id = str(job.get("thread_id", "") or "")
        if not session_id and not thread_id:
            return

        task_id = ""
        manifest = job.get("manifest")
        if manifest is not None:
            task_id = getattr(manifest, "task_id", "")

        await self._event_bus.publish(Event(
            event_id=f"evt-{uuid4().hex[:8]}",
            type="task.failed",
            source="worker_scheduler",
            tenant_id=job.get("tenant_id", ""),
            payload=(
                ("session_id", session_id),
                ("thread_id", thread_id),
                ("task_id", task_id),
                ("error_message", error_message),
                ("error_code", error_code),
                ("worker_id", job.get("worker_id", "")),
            ),
        ))

    async def publish_task_completed(
        self,
        job: dict,
        result: dict,
    ) -> None:
        """Publish task.completed when thread context is available."""
        if self._event_bus is None:
            return

        manifest = job.get("manifest")
        task_id = getattr(manifest, "task_id", "") or result.get("task_id", "")
        description = getattr(manifest, "task_description", "") or job.get("task", "")
        await self._event_bus.publish(Event(
            event_id=f"evt-{uuid4().hex[:8]}",
            type="task.completed",
            source="worker_scheduler",
            tenant_id=job.get("tenant_id", ""),
            payload=(
                ("task_id", task_id),
                ("description", description),
                ("thread_id", job.get("thread_id", "")),
                ("worker_id", job.get("worker_id", "")),
                ("summary", result.get("content", "")[:500]),
            ),
        ))

    async def publish_isolated_run_completed(
        self,
        job: dict,
        result: dict,
    ) -> None:
        """Publish isolated_run.completed when a main session is attached."""
        main_session_key = self.extract_main_session_key(job)
        if self._event_bus is None or not main_session_key:
            return

        await self._event_bus.publish(Event(
            event_id=f"evt-{uuid4().hex[:8]}",
            type="isolated_run.completed",
            source="worker_scheduler",
            tenant_id=job.get("tenant_id", ""),
            payload=(
                ("main_session_key", main_session_key),
                ("run_id", result.get("run_id", "")),
                ("worker_id", job.get("worker_id", "")),
                ("task_id", result.get("task_id", "")),
                ("summary", result.get("content", "")[:500]),
            ),
        ))

    async def publish_isolated_run_failed(
        self,
        job: dict,
        error_message: str,
    ) -> None:
        """Publish isolated_run.failed when a main session is attached."""
        main_session_key = self.extract_main_session_key(job)
        if self._event_bus is None or not main_session_key:
            return

        manifest = job.get("manifest")
        await self._event_bus.publish(Event(
            event_id=f"evt-{uuid4().hex[:8]}",
            type="isolated_run.failed",
            source="worker_scheduler",
            tenant_id=job.get("tenant_id", ""),
            payload=(
                ("main_session_key", main_session_key),
                ("run_id", getattr(manifest, "run_id", "")),
                ("worker_id", job.get("worker_id", "")),
                ("task_id", getattr(manifest, "task_id", "")),
                ("error_message", error_message),
            ),
        ))

    async def publish_dead_lettered(
        self,
        job: dict,
        error_message: str,
        total_attempts: int,
    ) -> None:
        """Publish a terminal dead-letter event for observability."""
        if self._event_bus is None:
            return

        await self._event_bus.publish(Event(
            event_id=f"evt-{uuid4().hex[:8]}",
            type="task.dead_lettered",
            source="worker_scheduler",
            tenant_id=job.get("tenant_id", ""),
            payload=(
                ("worker_id", job.get("worker_id", "")),
                ("task_description", job.get("task", "")),
                ("error_message", error_message),
                ("total_attempts", total_attempts),
            ),
        ))

    async def send_to_dead_letter(
        self,
        job: dict,
        error_message: str,
        retry_count: int,
    ) -> None:
        """Persist a task that exhausted retries."""
        if self._dead_letter_store is None:
            return

        await self._dead_letter_store.add(DeadLetterEntry(
            entry_id=f"dl-{uuid4().hex[:8]}",
            worker_id=str(job.get("worker_id", "")),
            tenant_id=str(job.get("tenant_id", "")),
            task_description=str(job.get("task", "")),
            error_message=error_message,
            retry_count=retry_count,
            failed_at=utc_now_iso(),
            job_snapshot=self.snapshot_job(job),
        ))

    def snapshot_job(self, job: dict) -> tuple[tuple[str, object], ...]:
        """Capture a serializable subset of the failed job."""
        manifest = job.get("manifest")
        manifest_snapshot = {}
        if manifest is not None:
            for field_name in (
                "task_id",
                "task_description",
                "run_id",
                "worker_id",
                "tenant_id",
                "main_session_key",
            ):
                value = getattr(manifest, field_name, None)
                if value not in (None, ""):
                    manifest_snapshot[field_name] = value
        snapshot = {
            "task": job.get("task", ""),
            "tenant_id": job.get("tenant_id", ""),
            "worker_id": job.get("worker_id", ""),
            "session_id": job.get("session_id", ""),
            "thread_id": job.get("thread_id", ""),
            "retry_count": job.get("retry_count", 0),
            "main_session_key": job.get("main_session_key", ""),
        }
        if manifest_snapshot:
            snapshot["manifest"] = manifest_snapshot
        return tuple(sorted(snapshot.items()))

    def extract_main_session_key(self, job: dict) -> str:
        """Get the main session key from job or manifest metadata."""
        main_session_key = job.get("main_session_key")
        if main_session_key:
            return str(main_session_key)
        manifest = job.get("manifest")
        return getattr(manifest, "main_session_key", "") or ""
