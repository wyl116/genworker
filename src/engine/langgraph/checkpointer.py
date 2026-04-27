"""File-backed langgraph checkpoint saver."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, AsyncIterator
from uuid import uuid4

from langgraph.checkpoint.base import BaseCheckpointSaver, CheckpointTuple

from src.common.time import utc_now_iso

from .models import LangGraphCheckpointRecord


class LangGraphCheckpointer(BaseCheckpointSaver):
    """Persist langgraph checkpoints under a dedicated workspace path."""

    def __init__(self, workspace_root: Path) -> None:
        super().__init__()
        self._workspace_root = Path(workspace_root)

    async def aget_tuple(self, config) -> CheckpointTuple | None:
        thread_id = self._thread_id_from_config(config)
        if not thread_id:
            return None
        record = self._load_record(
            thread_id=thread_id,
            checkpoint_id=str(config.get("configurable", {}).get("checkpoint_id", "") or ""),
        )
        if record is None:
            return None
        return CheckpointTuple(
            config={
                "configurable": {
                    "thread_id": record.thread_id,
                    "checkpoint_ns": "",
                    "checkpoint_id": record.checkpoint_id,
                }
            },
            checkpoint=dict(record.lg_checkpoint),
            metadata=dict(record.lg_metadata),
            parent_config=None,
            pending_writes=None,
        )

    async def aput(self, config, checkpoint, metadata, new_versions) -> dict[str, Any]:
        configurable = dict(config.get("configurable", {}))
        thread_id = self._thread_id_from_config(config)
        if not thread_id:
            raise ValueError("LangGraph checkpoint requires configurable.thread_id")
        tenant_id = str(configurable.get("tenant_id", "") or "")
        worker_id = str(configurable.get("worker_id", "") or "")
        skill_id = str(configurable.get("skill_id", "") or "")
        source_path = str(configurable.get("source_path", "") or "")
        if tenant_id and worker_id and skill_id:
            await self.register_thread(
                thread_id=thread_id,
                tenant_id=tenant_id,
                worker_id=worker_id,
                skill_id=skill_id,
                source_path=source_path,
            )
        info = self._thread_info(thread_id)
        if info is None:
            raise ValueError(f"Unknown langgraph thread '{thread_id}'")
        round_number = self._next_round(thread_id)
        checkpoint_id = str(checkpoint.get("id", "") or uuid4().hex)
        whitelist = tuple(str(item) for item in configurable.get("state_whitelist", ()) if str(item))
        record = {
            "checkpoint_id": checkpoint_id,
            "thread_id": thread_id,
            "skill_id": info["skill_id"],
            "round_number": round_number,
            "created_at": utc_now_iso(),
            "lg_checkpoint": checkpoint,
            "lg_metadata": metadata,
            "state_digest": str(configurable.get("state_digest", "") or ""),
            "whitelist": list(whitelist),
            "source_path": info.get("source_path", ""),
        }
        thread_dir = self._thread_dir(
            tenant_id=info["tenant_id"],
            worker_id=info["worker_id"],
            thread_id=thread_id,
        )
        thread_dir.mkdir(parents=True, exist_ok=True)
        (thread_dir / "index.json").write_text(
            json.dumps(
                {
                    "tenant_id": info["tenant_id"],
                    "worker_id": info["worker_id"],
                    "skill_id": info["skill_id"],
                    "source_path": info.get("source_path", ""),
                    "created_at": info["created_at"],
                    "updated_at": record["created_at"],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        filename = f"checkpoint_{round_number:04d}_{checkpoint_id}.json"
        (thread_dir / filename).write_text(
            json.dumps(record, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": "",
                "checkpoint_id": checkpoint_id,
                "tenant_id": info["tenant_id"],
                "worker_id": info["worker_id"],
                "skill_id": info["skill_id"],
                "source_path": info.get("source_path", ""),
                "state_whitelist": list(whitelist),
            }
        }

    async def aput_writes(self, config, writes, task_id, task_path="") -> None:
        del config, writes, task_id, task_path
        return None

    def put_writes(self, config, writes, task_id, task_path="") -> None:
        del config, writes, task_id, task_path
        return None

    async def alist(
        self,
        config,
        *,
        filter=None,
        before=None,
        limit=None,
    ) -> AsyncIterator[CheckpointTuple]:
        if config is None:
            return
        thread_id = self._thread_id_from_config(config)
        if not thread_id:
            return
        records = self._list_records(thread_id)
        if before is not None:
            before_id = str(before.get("configurable", {}).get("checkpoint_id", "") or "")
            if before_id:
                records = [record for record in records if record.checkpoint_id < before_id]
        if limit is not None:
            records = records[:max(int(limit or 0), 0)]
        for record in records:
            yield CheckpointTuple(
                config={
                    "configurable": {
                        "thread_id": record.thread_id,
                        "checkpoint_ns": "",
                        "checkpoint_id": record.checkpoint_id,
                    }
                },
                checkpoint=dict(record.lg_checkpoint),
                metadata=dict(record.lg_metadata),
                parent_config=None,
                pending_writes=None,
            )

    async def load_by_thread(self, thread_id: str) -> LangGraphCheckpointRecord | None:
        return self._load_record(thread_id=str(thread_id).strip())

    async def register_thread(
        self,
        *,
        thread_id: str,
        tenant_id: str,
        worker_id: str,
        skill_id: str,
        source_path: str = "",
    ) -> None:
        if not thread_id:
            return
        index_path = self._index_path()
        index_path.parent.mkdir(parents=True, exist_ok=True)
        index = self._load_json(index_path, default={})
        entry = dict(index.get(thread_id, {}))
        created_at = str(entry.get("created_at", "") or utc_now_iso())
        index[thread_id] = {
            "tenant_id": tenant_id,
            "worker_id": worker_id,
            "skill_id": skill_id,
            "source_path": source_path,
            "created_at": created_at,
        }
        index_path.write_text(
            json.dumps(index, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    async def annotate_thread(
        self,
        *,
        thread_id: str,
        state_digest: str,
        whitelist: tuple[str, ...],
    ) -> None:
        record = self._load_record(thread_id=thread_id)
        if record is None:
            return
        path = self._checkpoint_path(thread_id, record.checkpoint_id)
        payload = self._load_json(path, default={})
        payload["state_digest"] = state_digest
        payload["whitelist"] = list(whitelist)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _load_record(
        self,
        *,
        thread_id: str,
        checkpoint_id: str = "",
    ) -> LangGraphCheckpointRecord | None:
        info = self._thread_info(thread_id)
        if info is None:
            return None
        thread_dir = self._thread_dir(
            tenant_id=info["tenant_id"],
            worker_id=info["worker_id"],
            thread_id=thread_id,
        )
        if not thread_dir.is_dir():
            return None
        candidates = sorted(thread_dir.glob("checkpoint_*.json"))
        if checkpoint_id:
            candidates = [path for path in candidates if path.stem.endswith(checkpoint_id)]
        if not candidates:
            return None
        data = self._load_json(candidates[-1], default={})
        return LangGraphCheckpointRecord(
            tenant_id=info["tenant_id"],
            worker_id=info["worker_id"],
            skill_id=str(data.get("skill_id", "") or info["skill_id"]),
            thread_id=str(data.get("thread_id", "") or thread_id),
            checkpoint_id=str(data.get("checkpoint_id", "")),
            round_number=int(data.get("round_number", 0) or 0),
            created_at=str(data.get("created_at", "")),
            lg_checkpoint=dict(data.get("lg_checkpoint", {})),
            lg_metadata=dict(data.get("lg_metadata", {})),
            state_digest=str(data.get("state_digest", "") or ""),
            whitelist=tuple(str(item) for item in data.get("whitelist", ()) if str(item)),
            source_path=str(data.get("source_path", "") or info.get("source_path", "")),
        )

    def _list_records(self, thread_id: str) -> list[LangGraphCheckpointRecord]:
        info = self._thread_info(thread_id)
        if info is None:
            return []
        thread_dir = self._thread_dir(
            tenant_id=info["tenant_id"],
            worker_id=info["worker_id"],
            thread_id=thread_id,
        )
        records: list[LangGraphCheckpointRecord] = []
        for path in sorted(thread_dir.glob("checkpoint_*.json")):
            data = self._load_json(path, default={})
            records.append(
                LangGraphCheckpointRecord(
                    tenant_id=info["tenant_id"],
                    worker_id=info["worker_id"],
                    skill_id=str(data.get("skill_id", "") or info["skill_id"]),
                    thread_id=str(data.get("thread_id", "") or thread_id),
                    checkpoint_id=str(data.get("checkpoint_id", "")),
                    round_number=int(data.get("round_number", 0) or 0),
                    created_at=str(data.get("created_at", "")),
                    lg_checkpoint=dict(data.get("lg_checkpoint", {})),
                    lg_metadata=dict(data.get("lg_metadata", {})),
                    state_digest=str(data.get("state_digest", "") or ""),
                    whitelist=tuple(str(item) for item in data.get("whitelist", ()) if str(item)),
                    source_path=str(data.get("source_path", "") or info.get("source_path", "")),
                )
            )
        records.sort(key=lambda item: (item.round_number, item.created_at, item.checkpoint_id))
        return records

    def _thread_info(self, thread_id: str) -> dict[str, Any] | None:
        index = self._load_json(self._index_path(), default={})
        info = index.get(thread_id)
        return dict(info) if isinstance(info, dict) else None

    def _next_round(self, thread_id: str) -> int:
        record = self._load_record(thread_id=thread_id)
        if record is None:
            return 1
        return record.round_number + 1

    def _thread_dir(self, *, tenant_id: str, worker_id: str, thread_id: str) -> Path:
        return (
            self._workspace_root
            / "tenants"
            / tenant_id
            / "workers"
            / worker_id
            / "langgraph_threads"
            / thread_id
        )

    def _checkpoint_path(self, thread_id: str, checkpoint_id: str) -> Path:
        info = self._thread_info(thread_id)
        if info is None:
            raise ValueError(f"Unknown langgraph thread '{thread_id}'")
        return next(
            path
            for path in self._thread_dir(
                tenant_id=info["tenant_id"],
                worker_id=info["worker_id"],
                thread_id=thread_id,
            ).glob(f"checkpoint_*_{checkpoint_id}.json")
        )

    def _index_path(self) -> Path:
        return self._workspace_root / "langgraph_index" / "threads.json"

    def _thread_id_from_config(self, config: dict[str, Any]) -> str:
        return str(config.get("configurable", {}).get("thread_id", "") or "")

    def _load_json(self, path: Path, *, default: Any) -> Any:
        if not path.is_file():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default
