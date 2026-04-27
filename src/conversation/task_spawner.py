"""
TaskSpawner - spawn async tasks from conversations.

Provides:
- SpawnTaskInput / SpawnTaskResult frozen dataclasses
- TaskSpawner executor
- SPAWN_TASK_TOOL_SCHEMA for LLM tool registration
"""
from dataclasses import dataclass
from typing import Optional
from uuid import uuid4

from src.common.logger import get_logger
from src.events.models import Event, EventBusProtocol
from src.worker.lifecycle.detectors import resolve_gate_level
from src.worker.task import TaskManifest, TaskStore, create_task_manifest

logger = get_logger()


@dataclass(frozen=True)
class SpawnTaskInput:
    """Input parameters for the spawn_task tool."""
    task_description: str
    context: str = ""
    skill_hint: Optional[str] = None


@dataclass(frozen=True)
class SpawnTaskResult:
    """Result of a spawn_task execution."""
    task_id: str
    status: str          # "accepted" | "rejected"
    message: str


SPAWN_TASK_TOOL_SCHEMA = {
    "name": "spawn_task",
    "description": (
        "When the user's requirements are confirmed and the task will take "
        "a long time to execute, spawn an async task. The task runs in the "
        "background and the user can continue chatting or query results later."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_description": {
                "type": "string",
                "description": "Clear task description with all necessary info",
            },
            "context": {
                "type": "string",
                "description": "Key information and parameters from the conversation",
            },
            "skill_hint": {
                "type": "string",
                "description": "Optional: specify a Skill ID to skip auto-matching",
            },
        },
        "required": ["task_description", "context"],
    },
}


class TaskSpawner:
    """
    Executor for the spawn_task built-in tool.

    Creates a TaskManifest and hands it to the worker scheduler,
    returning a SpawnTaskResult to the conversation.
    """

    def __init__(
        self,
        task_store: TaskStore,
        skill_registry: Optional[object] = None,
        worker_registry: Optional[object] = None,
        worker_schedulers: Optional[dict[str, object]] = None,
        event_bus: EventBusProtocol | None = None,
    ) -> None:
        self._task_store = task_store
        self._skill_registry = skill_registry
        self._worker_registry = worker_registry
        self._worker_schedulers = worker_schedulers or {}
        self._event_bus = event_bus

    def replace_worker_schedulers(
        self,
        worker_schedulers: Optional[dict[str, object]],
    ) -> None:
        """Refresh scheduler bindings after runtime reload."""
        self._worker_schedulers = worker_schedulers or {}

    def replace_worker_registry(
        self,
        worker_registry: Optional[object],
    ) -> None:
        """Refresh worker registry after runtime reload."""
        self._worker_registry = worker_registry

    async def execute(
        self,
        input_data: SpawnTaskInput,
        session: "ConversationSession",
    ) -> SpawnTaskResult:
        """
        Validate input and create a task manifest.

        Args:
            input_data: The spawn_task tool input.
            session: Current conversation session (for tenant/worker context).

        Returns:
            SpawnTaskResult with accepted/rejected status.
        """
        # Validate: empty description
        if not input_data.task_description.strip():
            return SpawnTaskResult(
                task_id="",
                status="rejected",
                message="Task description must not be empty",
            )

        # Validate: invalid skill_hint
        if input_data.skill_hint:
            if self._skill_registry is None and self._worker_registry is None:
                skill = None
            else:
                skill = self._resolve_skill(input_data.skill_hint, session.worker_id)
            if skill is None and (
                self._skill_registry is not None or self._worker_registry is not None
            ):
                return SpawnTaskResult(
                    task_id="",
                    status="rejected",
                    message=f"Skill '{input_data.skill_hint}' not found",
                )

        # Create task manifest
        try:
            full_description = input_data.task_description
            if input_data.context:
                full_description = (
                    f"{input_data.task_description}\n\n"
                    f"Context: {input_data.context}"
                )

            matched_skill = None
            if input_data.skill_hint:
                matched_skill = self._resolve_skill(
                    input_data.skill_hint,
                    session.worker_id,
                )
            manifest = create_task_manifest(
                worker_id=session.worker_id,
                tenant_id=session.tenant_id,
                skill_id=input_data.skill_hint or "",
                gate_level=resolve_gate_level(
                    skill=matched_skill,
                    task_description=full_description,
                ),
                task_description=full_description,
                main_session_key=session.main_session_key,
            )
            self._task_store.save(manifest)

            scheduler = self._worker_schedulers.get(session.worker_id)
            if scheduler is None:
                self._task_store.save(
                    manifest.mark_error("Worker scheduler is not available")
                )
                await self._publish_task_failed(
                    session_id=session.session_id,
                    thread_id=session.thread_id,
                    task_id=manifest.task_id,
                    tenant_id=session.tenant_id,
                    error_message="Worker scheduler is not available",
                    error_code="SCHEDULER_NOT_AVAILABLE",
                )
                return SpawnTaskResult(
                    task_id="",
                    status="rejected",
                    message="Failed to schedule task: worker scheduler is not available",
                )

            accepted = await scheduler.submit_task(
                {
                    "task": full_description,
                    "tenant_id": session.tenant_id,
                    "worker_id": session.worker_id,
                    "manifest": manifest,
                    "session_id": session.session_id,
                    "thread_id": session.thread_id,
                    "main_session_key": session.main_session_key or "",
                },
                priority=20,
            )
            if not accepted:
                self._task_store.save(
                    manifest.mark_error("Scheduler quota exhausted")
                )
                await self._publish_task_failed(
                    session_id=session.session_id,
                    thread_id=session.thread_id,
                    task_id=manifest.task_id,
                    tenant_id=session.tenant_id,
                    error_message="Scheduler quota exhausted",
                    error_code="QUOTA_EXHAUSTED",
                )
                return SpawnTaskResult(
                    task_id="",
                    status="rejected",
                    message="Failed to schedule task: quota exhausted",
                )

            logger.info(
                f"[TaskSpawner] Spawned task {manifest.task_id} "
                f"from session {session.session_id}"
            )

            return SpawnTaskResult(
                task_id=manifest.task_id,
                status="accepted",
                message=f"Task created with ID: {manifest.task_id}",
            )

        except Exception as exc:
            logger.error(
                f"[TaskSpawner] Failed to create task: {exc}",
                exc_info=True,
            )
            return SpawnTaskResult(
                task_id="",
                status="rejected",
                message=f"Failed to create task: {exc}",
            )

    async def _publish_task_failed(
        self,
        session_id: str,
        thread_id: str,
        task_id: str,
        tenant_id: str,
        error_message: str,
        error_code: str,
    ) -> None:
        """Publish immediate task.failed when spawn scheduling fails."""
        if self._event_bus is None:
            return

        event = Event(
            event_id=f"evt-{uuid4().hex[:8]}",
            type="task.failed",
            source="task_spawner",
            tenant_id=tenant_id,
            payload=(
                ("session_id", session_id),
                ("thread_id", thread_id),
                ("task_id", task_id),
                ("error_message", error_message),
                ("error_code", error_code),
            ),
        )
        await self._event_bus.publish(event)

    def _resolve_skill(self, skill_id: str, worker_id: str):
        """Resolve a skill with worker-local registry preferred over global."""
        registry = self._skill_registry_for_worker(worker_id)
        if registry is None:
            return None
        return getattr(registry, "get", lambda x: None)(skill_id)

    def _skill_registry_for_worker(self, worker_id: str):
        """Return the most specific skill registry available for one worker."""
        if self._worker_registry is not None:
            entry = getattr(self._worker_registry, "get", lambda x: None)(worker_id)
            if entry is not None:
                return getattr(entry, "skill_registry", None)
        return self._skill_registry
