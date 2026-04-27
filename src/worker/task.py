"""
TaskManifest and TaskStore - task persistence for worker executions.

TaskManifest tracks execution lifecycle: pending -> running -> completed | error.
TaskStore uses file-based storage under workspace/tenants/{tid}/workers/{wid}/tasks/.
"""
import json
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional, Sequence
from uuid import uuid4

from src.common.logger import get_logger
from src.worker.scripts.models import PreScript, deserialize_pre_script, serialize_pre_script

logger = get_logger()


class TaskStatus(str, Enum):
    """Task lifecycle status."""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    ERROR = "error"


def _now_iso() -> str:
    """ISO 8601 UTC timestamp."""
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class TaskProvenance:
    """Task provenance and lifecycle linkage metadata."""

    source_type: str = "manual"
    source_id: str = ""
    goal_id: str = ""
    goal_task_id: str = ""
    duty_id: str = ""
    trigger_id: str = ""
    suggestion_id: str = ""
    parent_task_id: str = ""


@dataclass(frozen=True)
class TaskManifest:
    """
    Immutable task execution manifest.

    Tracks a single execution from creation to completion.
    """
    task_id: str
    worker_id: str
    tenant_id: str
    skill_id: str = ""
    preferred_skill_ids: tuple[str, ...] = ()
    provenance: TaskProvenance = field(default_factory=TaskProvenance)
    gate_level: str = "gated"
    status: TaskStatus = TaskStatus.PENDING
    task_description: str = ""
    pre_script: PreScript | None = None
    result_summary: str = ""
    error_message: str = ""
    main_session_key: str | None = None
    created_at: str = field(default_factory=_now_iso)
    started_at: str = ""
    completed_at: str = ""
    run_id: str = ""

    def mark_pending(self) -> "TaskManifest":
        """Return a new manifest with status=PENDING."""
        return replace(
            self,
            status=TaskStatus.PENDING,
            started_at="",
            completed_at="",
            error_message="",
            result_summary="",
            run_id="",
        )

    def mark_running(self, run_id: str = "") -> "TaskManifest":
        """Return a new manifest with status=RUNNING."""
        return replace(
            self,
            status=TaskStatus.RUNNING,
            started_at=_now_iso(),
            run_id=run_id,
        )

    def mark_completed(self, result_summary: str = "") -> "TaskManifest":
        """Return a new manifest with status=COMPLETED."""
        return replace(
            self,
            status=TaskStatus.COMPLETED,
            completed_at=_now_iso(),
            result_summary=result_summary,
        )

    def mark_error(self, error_message: str) -> "TaskManifest":
        """Return a new manifest with status=ERROR."""
        return replace(
            self,
            status=TaskStatus.ERROR,
            completed_at=_now_iso(),
            error_message=error_message,
        )

    def to_dict(self) -> dict:
        """Serialize to dict for JSON storage."""
        data = asdict(self)
        data["status"] = self.status.value
        data["pre_script"] = serialize_pre_script(self.pre_script)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "TaskManifest":
        """Deserialize from dict."""
        status_raw = data.get("status", "pending")
        try:
            status = TaskStatus(status_raw)
        except ValueError:
            status = TaskStatus.ERROR
        provenance_raw = data.get("provenance", {})
        if not isinstance(provenance_raw, dict):
            provenance_raw = {}

        return cls(
            task_id=data.get("task_id", ""),
            worker_id=data.get("worker_id", ""),
            tenant_id=data.get("tenant_id", ""),
            skill_id=data.get("skill_id", ""),
            preferred_skill_ids=tuple(data.get("preferred_skill_ids", ())),
            provenance=TaskProvenance(
                source_type=str(provenance_raw.get("source_type", "manual") or "manual"),
                source_id=str(provenance_raw.get("source_id", "") or ""),
                goal_id=str(provenance_raw.get("goal_id", "") or ""),
                goal_task_id=str(provenance_raw.get("goal_task_id", "") or ""),
                duty_id=str(provenance_raw.get("duty_id", "") or ""),
                trigger_id=str(provenance_raw.get("trigger_id", "") or ""),
                suggestion_id=str(provenance_raw.get("suggestion_id", "") or ""),
                parent_task_id=str(provenance_raw.get("parent_task_id", "") or ""),
            ),
            gate_level=str(data.get("gate_level", "gated") or "gated"),
            status=status,
            task_description=data.get("task_description", ""),
            pre_script=deserialize_pre_script(data.get("pre_script")),
            result_summary=data.get("result_summary", ""),
            error_message=data.get("error_message", ""),
            main_session_key=data.get("main_session_key"),
            created_at=data.get("created_at", ""),
            started_at=data.get("started_at", ""),
            completed_at=data.get("completed_at", ""),
            run_id=data.get("run_id", ""),
        )


def create_task_manifest(
    worker_id: str,
    tenant_id: str,
    skill_id: str = "",
    preferred_skill_ids: Sequence[str] = (),
    provenance: TaskProvenance | None = None,
    gate_level: str = "gated",
    task_description: str = "",
    pre_script: PreScript | None = None,
    main_session_key: str | None = None,
) -> TaskManifest:
    """Factory function to create a new pending TaskManifest."""
    return TaskManifest(
        task_id=uuid4().hex,
        worker_id=worker_id,
        tenant_id=tenant_id,
        skill_id=skill_id,
        preferred_skill_ids=tuple(
            str(item).strip()
            for item in preferred_skill_ids
            if str(item).strip()
        ),
        provenance=provenance or TaskProvenance(),
        gate_level=str(gate_level or "gated"),
        task_description=task_description,
        pre_script=pre_script,
        main_session_key=main_session_key,
    )


class TaskStore:
    """
    File-based task persistence.

    Stores task manifests as JSON in:
      workspace/tenants/{tid}/workers/{wid}/tasks/active/{task_id}.json
    """

    def __init__(self, workspace_root: Path) -> None:
        self._workspace_root = workspace_root

    def save(self, manifest: TaskManifest) -> None:
        """
        Save a task manifest to the filesystem.

        Creates directories as needed.
        """
        tasks_dir = self._active_dir(manifest.tenant_id, manifest.worker_id)
        tasks_dir.mkdir(parents=True, exist_ok=True)

        file_path = tasks_dir / f"{manifest.task_id}.json"
        data = json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2)

        try:
            file_path.write_text(data, encoding="utf-8")
        except OSError as exc:
            logger.error(f"[TaskStore] Failed to save task {manifest.task_id}: {exc}")
            raise

    def load(self, tenant_id: str, worker_id: str, task_id: str) -> Optional[TaskManifest]:
        """Load a single task manifest by ID."""
        file_path = self._active_dir(tenant_id, worker_id) / f"{task_id}.json"
        if not file_path.is_file():
            return None

        try:
            raw = file_path.read_text(encoding="utf-8")
            data = json.loads(raw)
            return TaskManifest.from_dict(data)
        except (OSError, json.JSONDecodeError) as exc:
            logger.error(f"[TaskStore] Failed to load task {task_id}: {exc}")
            return None

    def list_by_worker(
        self,
        tenant_id: str,
        worker_id: str,
    ) -> tuple[TaskManifest, ...]:
        """
        List all active tasks for a worker.

        Only scans the active/ directory.
        """
        tasks_dir = self._active_dir(tenant_id, worker_id)
        if not tasks_dir.is_dir():
            return ()

        manifests: list[TaskManifest] = []
        for file_path in sorted(tasks_dir.glob("*.json")):
            try:
                raw = file_path.read_text(encoding="utf-8")
                data = json.loads(raw)
                manifests.append(TaskManifest.from_dict(data))
            except (OSError, json.JSONDecodeError) as exc:
                logger.error(
                    f"[TaskStore] Failed to read {file_path}: {exc}"
                )
        return tuple(manifests)

    def _active_dir(self, tenant_id: str, worker_id: str) -> Path:
        """Resolve the active tasks directory for a worker."""
        return (
            self._workspace_root / "tenants" / tenant_id
            / "workers" / worker_id / "tasks" / "active"
        )
