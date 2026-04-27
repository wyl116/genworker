# edition: baseline
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.memory.episodic.store import IndexFileLock, load_index
from src.worker.trust_gate import WorkerTrustGate
from src.worker.duty.duty_learning import handle_duty_post_execution
from src.worker.duty.models import Duty, DutyExecutionRecord, ExecutionPolicy


def _worker_dir(tmp_path: Path) -> Path:
    return tmp_path / "tenants" / "demo" / "workers" / "worker-1"


def _duty() -> Duty:
    return Duty(
        duty_id="duty-1",
        title="Daily Quality Check",
        status="active",
        triggers=(),
        execution_policy=ExecutionPolicy(default="standard"),
        action="Check quality signals.",
        quality_criteria=("No obvious failures",),
        skill_hint="analysis-skill",
    )


@pytest.mark.asyncio
async def test_duty_learning_writes_to_orchestrator(tmp_path: Path) -> None:
    worker_dir = _worker_dir(tmp_path)
    orchestrator = SimpleNamespace(on_memory_write=AsyncMock())

    await handle_duty_post_execution(
        record=DutyExecutionRecord(
            execution_id="exec-1",
            duty_id="duty-1",
            trigger_id="cron-1",
            depth="standard",
            executed_at="2026-04-17T00:00:00+00:00",
            duration_seconds=3.0,
            conclusion="Validated the latest quality output.",
        ),
        duty=_duty(),
        worker_dir=worker_dir,
        llm_client=None,
        episode_lock=IndexFileLock(),
        memory_orchestrator=orchestrator,
    )

    indices = load_index(worker_dir / "memory")
    assert len(indices) == 1

    orchestrator.on_memory_write.assert_awaited_once()
    event = orchestrator.on_memory_write.await_args.args[0]
    assert event.target == "episode"
    assert event.source_subsystem == "episodic"


@pytest.mark.asyncio
async def test_duty_learning_skips_episode_when_episodic_write_disabled(tmp_path: Path) -> None:
    worker_dir = _worker_dir(tmp_path)
    orchestrator = SimpleNamespace(on_memory_write=AsyncMock())

    await handle_duty_post_execution(
        record=DutyExecutionRecord(
            execution_id="exec-1",
            duty_id="duty-1",
            trigger_id="cron-1",
            depth="standard",
            executed_at="2026-04-17T00:00:00+00:00",
            duration_seconds=3.0,
            conclusion="Validated the latest quality output.",
        ),
        duty=_duty(),
        worker_dir=worker_dir,
        llm_client=None,
        episode_lock=IndexFileLock(),
        memory_orchestrator=orchestrator,
        trust_gate=WorkerTrustGate(episodic_write_enabled=False),
    )

    assert load_index(worker_dir / "memory") == ()
    orchestrator.on_memory_write.assert_not_awaited()


@pytest.mark.asyncio
async def test_duty_learning_keeps_local_episode_when_memory_mirror_fails(tmp_path: Path) -> None:
    worker_dir = _worker_dir(tmp_path)
    orchestrator = SimpleNamespace(on_memory_write=AsyncMock(side_effect=RuntimeError("boom")))

    await handle_duty_post_execution(
        record=DutyExecutionRecord(
            execution_id="exec-1",
            duty_id="duty-1",
            trigger_id="cron-1",
            depth="standard",
            executed_at="2026-04-17T00:00:00+00:00",
            duration_seconds=3.0,
            conclusion="Validated the latest quality output.",
        ),
        duty=_duty(),
        worker_dir=worker_dir,
        llm_client=None,
        episode_lock=IndexFileLock(),
        memory_orchestrator=orchestrator,
    )

    indices = load_index(worker_dir / "memory")
    assert len(indices) == 1
    orchestrator.on_memory_write.assert_awaited_once()
