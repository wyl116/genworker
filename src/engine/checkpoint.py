"""Run-scoped checkpoint persistence for engine recovery."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


@dataclass(frozen=True)
class CheckpointRef:
    checkpoint_id: str
    tenant_id: str
    worker_id: str
    task_id: str
    run_id: str
    thread_id: str
    engine_type: str
    round_number: int
    created_at: str
    message_count: int = 0
    token_usage: int = 0
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class CheckpointHandle:
    tenant_id: str
    worker_id: str
    task_id: str
    run_id: str
    thread_id: str
    engine_type: str = ""


@dataclass(frozen=True)
class ExecutionSnapshot:
    checkpoint_ref: CheckpointRef
    budget: dict[str, int]
    worker_context: dict[str, Any]
    messages: tuple[dict[str, Any], ...] = ()
    step_results: tuple[dict[str, Any], ...] = ()
    current_step: str = ""
    handoff_payload: dict[str, Any] | None = None


@dataclass(frozen=True)
class EngineHandoff:
    source_engine: str
    target_engine: str
    payload: dict[str, Any]


class StateCheckpointer:
    """Persist immutable execution snapshots under worker run directories."""

    def __init__(self, workspace_root: Path) -> None:
        self._workspace_root = Path(workspace_root)

    async def save(self, snapshot: ExecutionSnapshot) -> CheckpointRef:
        run_dir = self._run_dir(
            snapshot.checkpoint_ref.tenant_id,
            snapshot.checkpoint_ref.worker_id,
            snapshot.checkpoint_ref.task_id,
            snapshot.checkpoint_ref.run_id,
        )
        run_dir.mkdir(parents=True, exist_ok=True)
        filename = f"checkpoint_{snapshot.checkpoint_ref.round_number:04d}_{snapshot.checkpoint_ref.checkpoint_id}.json"
        payload = asdict(snapshot)
        (run_dir / filename).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return snapshot.checkpoint_ref

    async def load_latest(
        self,
        tenant_id: str,
        worker_id: str,
        task_id: str,
        run_id: str,
    ) -> ExecutionSnapshot | None:
        refs = await self.list_checkpoints(tenant_id, worker_id, task_id, run_id)
        if not refs:
            return None
        latest = refs[-1]
        path = self._checkpoint_file(latest)
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return _snapshot_from_dict(data)

    async def list_checkpoints(
        self,
        tenant_id: str,
        worker_id: str,
        task_id: str,
        run_id: str,
    ) -> tuple[CheckpointRef, ...]:
        run_dir = self._run_dir(tenant_id, worker_id, task_id, run_id)
        if not run_dir.is_dir():
            return ()
        refs: list[CheckpointRef] = []
        for path in sorted(run_dir.glob("checkpoint_*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                refs.append(_checkpoint_ref_from_dict(data.get("checkpoint_ref", {})))
            except Exception:
                continue
        refs.sort(key=lambda item: (item.round_number, item.created_at, item.checkpoint_id))
        return tuple(refs)

    def _run_dir(self, tenant_id: str, worker_id: str, task_id: str, run_id: str) -> Path:
        return (
            self._workspace_root
            / "tenants"
            / tenant_id
            / "workers"
            / worker_id
            / "runs"
            / task_id
            / run_id
        )

    def _checkpoint_file(self, ref: CheckpointRef) -> Path:
        return self._run_dir(ref.tenant_id, ref.worker_id, ref.task_id, ref.run_id) / (
            f"checkpoint_{ref.round_number:04d}_{ref.checkpoint_id}.json"
        )


def make_checkpoint_ref(
    handle: CheckpointHandle,
    *,
    round_number: int,
    message_count: int = 0,
    token_usage: int = 0,
    metadata: dict[str, str] | None = None,
) -> CheckpointRef:
    return CheckpointRef(
        checkpoint_id=uuid4().hex,
        tenant_id=handle.tenant_id,
        worker_id=handle.worker_id,
        task_id=handle.task_id,
        run_id=handle.run_id,
        thread_id=handle.thread_id,
        engine_type=handle.engine_type,
        round_number=round_number,
        created_at=datetime.now(timezone.utc).isoformat(),
        message_count=message_count,
        token_usage=token_usage,
        metadata=dict(metadata or {}),
    )


def with_engine(handle: CheckpointHandle | None, engine_type: str) -> CheckpointHandle | None:
    if handle is None:
        return None
    return replace(handle, engine_type=engine_type)


def _checkpoint_ref_from_dict(data: dict[str, Any]) -> CheckpointRef:
    return CheckpointRef(
        checkpoint_id=str(data.get("checkpoint_id", "")),
        tenant_id=str(data.get("tenant_id", "")),
        worker_id=str(data.get("worker_id", "")),
        task_id=str(data.get("task_id", "")),
        run_id=str(data.get("run_id", "")),
        thread_id=str(data.get("thread_id", "")),
        engine_type=str(data.get("engine_type", "")),
        round_number=int(data.get("round_number", 0) or 0),
        created_at=str(data.get("created_at", "")),
        message_count=int(data.get("message_count", 0) or 0),
        token_usage=int(data.get("token_usage", 0) or 0),
        metadata=dict(data.get("metadata", {})),
    )


def _snapshot_from_dict(data: dict[str, Any]) -> ExecutionSnapshot:
    return ExecutionSnapshot(
        checkpoint_ref=_checkpoint_ref_from_dict(data.get("checkpoint_ref", {})),
        budget=dict(data.get("budget", {})),
        worker_context=dict(data.get("worker_context", {})),
        messages=tuple(data.get("messages", ()) or ()),
        step_results=tuple(data.get("step_results", ()) or ()),
        current_step=str(data.get("current_step", "")),
        handoff_payload=data.get("handoff_payload"),
    )
