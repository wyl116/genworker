# edition: baseline
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.engine.state import WorkerContext
from src.memory.episodic.store import IndexFileLock
from src.runtime.task_hooks import build_post_run_handler
from src.worker.rules.models import RuleCandidate, RuleScope, RuleSource
from src.worker.rules.rule_generator import validate_and_create_rule
from src.worker.rules.rule_manager import load_rules
from src.worker.task import create_task_manifest
from src.worker.task_runner import PostRunExtraction


def _write_persona(worker_dir: Path) -> None:
    worker_dir.mkdir(parents=True, exist_ok=True)
    (worker_dir / "PERSONA.md").write_text(
        "---\nidentity:\n  worker_id: worker-1\n  name: Worker 1\nprinciples:\n  - Be accurate\n---\n",
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_post_run_handler_emits_preference_and_decision_sources(tmp_path: Path) -> None:
    _write_persona(tmp_path / "tenants" / "demo" / "workers" / "worker-1")
    orchestrator = SimpleNamespace(on_memory_write=AsyncMock())
    handler = build_post_run_handler(
        workspace_root=tmp_path,
        llm_client=None,
        episode_lock=IndexFileLock(),
        memory_orchestrator=orchestrator,
    )
    manifest = create_task_manifest(
        worker_id="worker-1",
        tenant_id="demo",
        task_description="我喜欢表格格式的报告。最终使用 Redis 作为共享存储。",
    ).mark_completed("done")

    await handler(
        manifest,
        WorkerContext(worker_id="worker-1", tenant_id="demo"),
        PostRunExtraction(
            episode_summary="完成了任务。",
            key_findings=(),
            tool_names_used=(),
            rule_candidates=(),
            applied_rule_ids=(),
        ),
    )

    events = [call.args[0] for call in orchestrator.on_memory_write.await_args_list]
    preference_event = next(event for event in events if event.target == "preference")
    decision_event = next(event for event in events if event.target == "decision")

    assert preference_event.source_subsystem == "preference"
    assert decision_event.source_subsystem == "decision"


@pytest.mark.asyncio
async def test_rule_creation_emits_rule_source_subsystem(tmp_path: Path) -> None:
    orchestrator = SimpleNamespace(on_memory_write=AsyncMock())
    rules_dir = tmp_path / "rules"

    await validate_and_create_rule(
        candidate=RuleCandidate(
            rule="Validate inputs before analysis.",
            reason="Prevent malformed input from propagating.",
            category="strategy",
            scope=RuleScope(),
            source=RuleSource(
                type="self_reflection",
                evidence="task summary",
                created_at="2026-04-17T00:00:00+00:00",
            ),
        ),
        rules_dir=rules_dir,
        principles=(),
        existing_rules=(),
        memory_orchestrator=orchestrator,
        tenant_id="demo",
        worker_id="worker-1",
    )

    orchestrator.on_memory_write.assert_awaited_once()
    event = orchestrator.on_memory_write.await_args.args[0]
    assert event.target == "semantic_fact"
    assert event.source_subsystem == "rule"


@pytest.mark.asyncio
async def test_rule_creation_without_context_skips_mirror_but_keeps_local_rule(tmp_path: Path) -> None:
    orchestrator = SimpleNamespace(on_memory_write=AsyncMock())
    rules_dir = tmp_path / "rules"

    result = await validate_and_create_rule(
        candidate=RuleCandidate(
            rule="Validate inputs before analysis.",
            reason="Prevent malformed input from propagating.",
            category="strategy",
            scope=RuleScope(),
            source=RuleSource(
                type="self_reflection",
                evidence="task summary",
                created_at="2026-04-17T00:00:00+00:00",
            ),
        ),
        rules_dir=rules_dir,
        principles=(),
        existing_rules=(),
        memory_orchestrator=orchestrator,
    )

    assert result.rule == "Validate inputs before analysis."
    orchestrator.on_memory_write.assert_not_awaited()
    loaded = load_rules(rules_dir)
    assert len(loaded) == 1
    assert loaded[0].rule == "Validate inputs before analysis."


@pytest.mark.asyncio
async def test_rule_creation_keeps_local_success_when_mirror_fails(tmp_path: Path) -> None:
    orchestrator = SimpleNamespace(on_memory_write=AsyncMock(side_effect=RuntimeError("boom")))
    rules_dir = tmp_path / "rules"

    result = await validate_and_create_rule(
        candidate=RuleCandidate(
            rule="Validate inputs before analysis.",
            reason="Prevent malformed input from propagating.",
            category="strategy",
            scope=RuleScope(),
            source=RuleSource(
                type="self_reflection",
                evidence="task summary",
                created_at="2026-04-17T00:00:00+00:00",
            ),
        ),
        rules_dir=rules_dir,
        principles=(),
        existing_rules=(),
        memory_orchestrator=orchestrator,
        tenant_id="demo",
        worker_id="worker-1",
    )

    assert result.rule == "Validate inputs before analysis."
    orchestrator.on_memory_write.assert_awaited_once()
    loaded = load_rules(rules_dir)
    assert len(loaded) == 1
    assert loaded[0].rule == "Validate inputs before analysis."
