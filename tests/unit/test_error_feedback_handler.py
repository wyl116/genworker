# edition: baseline
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.engine.state import WorkerContext
from src.memory.episodic.store import IndexFileLock
from src.memory.orchestrator import MemoryOrchestrator
from src.memory.provider import EpisodicMemoryProvider
from src.runtime.task_hooks import build_error_feedback_handler
from src.worker.task import create_task_manifest
from src.worker.trust_gate import WorkerTrustGate


class _RecordingOpenVikingClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def index(
        self,
        *,
        scope: str,
        content: str,
        metadata: dict[str, object] | None = None,
        item_id: str | None = None,
        level: str = "L1",
    ) -> str:
        self.calls.append({
            "scope": scope,
            "content": content,
            "metadata": metadata or {},
            "item_id": item_id,
            "level": level,
        })
        return str(item_id or "ep-1")


@pytest.mark.asyncio
async def test_error_feedback_persists_episode_and_mirrors_via_orchestrator(tmp_path: Path) -> None:
    viking_client = _RecordingOpenVikingClient()
    orchestrator = MemoryOrchestrator((
        EpisodicMemoryProvider(viking_client),
    ))
    handler = build_error_feedback_handler(
        workspace_root=tmp_path,
        episode_lock=IndexFileLock(),
        memory_orchestrator=orchestrator,
        openviking_client=viking_client,
    )
    manifest = create_task_manifest(
        worker_id="worker-1",
        tenant_id="demo",
        skill_id="analysis-skill",
        task_description="Investigate failure",
    ).mark_error("boom")

    await handler(manifest, WorkerContext(worker_id="worker-1", tenant_id="demo"), ())

    episodes_dir = tmp_path / "tenants" / "demo" / "workers" / "worker-1" / "memory" / "episodes"
    assert any(episodes_dir.glob("*.md"))
    assert len(viking_client.calls) == 1
    assert viking_client.calls[0]["scope"] == "viking://tenant/demo/worker/worker-1/memories/episodic"
    assert str(viking_client.calls[0]["item_id"]).startswith("ep-")
    assert viking_client.calls[0]["metadata"]["source_type"] == "task_failure"


@pytest.mark.asyncio
async def test_error_feedback_falls_back_to_direct_index_when_orchestrator_has_no_episode_provider(
    tmp_path: Path,
) -> None:
    orchestrator = SimpleNamespace(on_memory_write=AsyncMock())
    viking_client = _RecordingOpenVikingClient()
    handler = build_error_feedback_handler(
        workspace_root=tmp_path,
        episode_lock=IndexFileLock(),
        memory_orchestrator=orchestrator,
        openviking_client=viking_client,
    )
    manifest = create_task_manifest(
        worker_id="worker-1",
        tenant_id="demo",
        skill_id="analysis-skill",
        task_description="Investigate failure",
    ).mark_error("boom")

    await handler(manifest, WorkerContext(worker_id="worker-1", tenant_id="demo"), ())

    assert len(viking_client.calls) == 1
    orchestrator.on_memory_write.assert_awaited_once()


@pytest.mark.asyncio
async def test_error_feedback_falls_back_to_direct_index_when_episode_provider_backend_is_unavailable(
    tmp_path: Path,
) -> None:
    viking_client = _RecordingOpenVikingClient()
    orchestrator = MemoryOrchestrator((
        EpisodicMemoryProvider(None),
    ))
    handler = build_error_feedback_handler(
        workspace_root=tmp_path,
        episode_lock=IndexFileLock(),
        memory_orchestrator=orchestrator,
        openviking_client=viking_client,
    )
    manifest = create_task_manifest(
        worker_id="worker-1",
        tenant_id="demo",
        skill_id="analysis-skill",
        task_description="Investigate failure",
    ).mark_error("boom")

    await handler(manifest, WorkerContext(worker_id="worker-1", tenant_id="demo"), ())

    assert len(viking_client.calls) == 1


@pytest.mark.asyncio
async def test_error_feedback_skips_remote_mirror_when_episodic_write_disabled(tmp_path: Path) -> None:
    viking_client = _RecordingOpenVikingClient()
    orchestrator = MemoryOrchestrator((
        EpisodicMemoryProvider(viking_client),
    ))
    handler = build_error_feedback_handler(
        workspace_root=tmp_path,
        episode_lock=IndexFileLock(),
        memory_orchestrator=orchestrator,
        openviking_client=viking_client,
    )
    manifest = create_task_manifest(
        worker_id="worker-1",
        tenant_id="demo",
        skill_id="analysis-skill",
        task_description="Investigate failure",
    ).mark_error("boom")

    await handler(
        manifest,
        WorkerContext(
            worker_id="worker-1",
            tenant_id="demo",
            trust_gate=WorkerTrustGate(episodic_write_enabled=False),
        ),
        (),
    )

    episodes_dir = tmp_path / "tenants" / "demo" / "workers" / "worker-1" / "memory" / "episodes"
    assert not episodes_dir.exists()
    assert viking_client.calls == []
