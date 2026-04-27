"""Isolated run orchestration for heartbeat-triggered deep work."""
from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

from src.events.models import Event, EventBusProtocol
from src.worker.task import (
    TaskManifest,
    TaskProvenance,
    TaskStore,
    create_task_manifest,
)
from src.worker.scripts.models import PreScript


class IsolatedRunManager:
    """Create background runs that flow results back to a main session."""

    def __init__(
        self,
        *,
        task_store: TaskStore,
        worker_schedulers: dict[str, object],
        event_bus: EventBusProtocol | None = None,
    ) -> None:
        self._task_store = task_store
        self._worker_schedulers = worker_schedulers
        self._event_bus = event_bus

    def replace_worker_schedulers(
        self,
        worker_schedulers: dict[str, object] | None,
    ) -> None:
        """Refresh scheduler bindings after runtime reload."""
        self._worker_schedulers = worker_schedulers or {}

    async def create_run(
        self,
        *,
        tenant_id: str,
        worker_id: str,
        task_description: str,
        main_session_key: str,
        preferred_skill_ids: tuple[str, ...] = (),
        provenance: TaskProvenance | None = None,
        pre_script: PreScript | None = None,
        gate_level: str = "gated",
    ) -> TaskManifest:
        manifest = create_task_manifest(
            worker_id=worker_id,
            tenant_id=tenant_id,
            preferred_skill_ids=preferred_skill_ids,
            provenance=provenance,
            gate_level=gate_level,
            task_description=task_description,
            pre_script=pre_script,
            main_session_key=main_session_key,
        )
        manifest = replace(manifest, main_session_key=main_session_key)
        self._task_store.save(manifest)

        scheduler = self._worker_schedulers.get(worker_id)
        if scheduler is None:
            manifest = manifest.mark_error("Worker scheduler is not available")
            self._task_store.save(manifest)
            await self._publish_run_failed(
                tenant_id=tenant_id,
                worker_id=worker_id,
                manifest=manifest,
                main_session_key=main_session_key,
                error_message="Worker scheduler is not available",
            )
            return manifest

        accepted = await scheduler.submit_task(
            {
                "task": task_description,
                "tenant_id": tenant_id,
                "worker_id": worker_id,
                "manifest": manifest,
                "main_session_key": main_session_key,
                "preferred_skill_ids": preferred_skill_ids,
            },
            priority=15,
        )
        if not accepted:
            manifest = manifest.mark_error("Scheduler quota exhausted")
            self._task_store.save(manifest)
            await self._publish_run_failed(
                tenant_id=tenant_id,
                worker_id=worker_id,
                manifest=manifest,
                main_session_key=main_session_key,
                error_message="Scheduler quota exhausted",
            )
        return manifest

    async def _publish_run_failed(
        self,
        *,
        tenant_id: str,
        worker_id: str,
        manifest: TaskManifest,
        main_session_key: str,
        error_message: str,
    ) -> None:
        if self._event_bus is None:
            return
        await self._event_bus.publish(Event(
            event_id=f"evt-{uuid4().hex[:8]}",
            type="isolated_run.failed",
            source="isolated_run_manager",
            tenant_id=tenant_id,
            payload=(
                ("main_session_key", main_session_key),
                ("run_id", getattr(manifest, "run_id", "")),
                ("worker_id", worker_id),
                ("task_id", manifest.task_id),
                ("error_message", error_message),
            ),
        ))
