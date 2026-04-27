# edition: baseline
from pathlib import Path

import pytest

from src.engine.state import WorkerContext
from src.memory.episodic.linkage import load_linkage
from src.memory.episodic.store import load_episode
from src.memory.episodic.store import IndexFileLock, load_index
from src.runtime.task_hooks import build_error_feedback_handler
from src.worker.task import TaskProvenance, create_task_manifest
from src.worker.rules.models import Rule, RuleScope, RuleSource, rule_to_markdown
from src.worker.rules.rule_manager import load_rules
from src.worker.trust_gate import WorkerTrustGate


def _rule(rule_id: str) -> Rule:
    return Rule(
        rule_id=rule_id,
        type="learned",
        category="strategy",
        status="active",
        rule="Validate inputs",
        reason="test",
        scope=RuleScope(),
        source=RuleSource(
            type="self_reflection",
            evidence="summary",
            created_at="2026-04-10T00:00:00+00:00",
        ),
        confidence=0.5,
        apply_count=1,
    )


@pytest.mark.asyncio
async def test_error_feedback_creates_failure_episode_and_penalty(tmp_path: Path):
    worker_dir = tmp_path / "tenants" / "demo" / "workers" / "w1"
    rules_dir = worker_dir / "rules" / "learned"
    rules_dir.mkdir(parents=True, exist_ok=True)
    (rules_dir / "r1.md").write_text(rule_to_markdown(_rule("r1")), encoding="utf-8")

    handler = build_error_feedback_handler(tmp_path, IndexFileLock())
    manifest = create_task_manifest(
        worker_id="w1",
        tenant_id="demo",
        skill_id="skill-1",
        task_description="do task",
    ).mark_error("boom")

    await handler(manifest, WorkerContext(worker_id="w1", tenant_id="demo"), ("r1",))

    memory_dir = worker_dir / "memory"
    links = load_linkage(memory_dir)
    assert len(links) == 1
    episode = load_episode(memory_dir, links[0].episode_id)
    assert episode.source.type == "task_failure"
    updated_rule = next(rule for rule in load_rules(worker_dir / "rules") if rule.rule_id == "r1")
    assert updated_rule.confidence < 0.5


@pytest.mark.asyncio
async def test_error_feedback_for_suggestion_preview_does_not_penalize_rules(tmp_path: Path):
    worker_dir = tmp_path / "tenants" / "demo" / "workers" / "w1"
    rules_dir = worker_dir / "rules" / "learned"
    rules_dir.mkdir(parents=True, exist_ok=True)
    (rules_dir / "r1.md").write_text(rule_to_markdown(_rule("r1")), encoding="utf-8")

    handler = build_error_feedback_handler(tmp_path, IndexFileLock())
    manifest = create_task_manifest(
        worker_id="w1",
        tenant_id="demo",
        skill_id="skill-1",
        task_description="preview task",
        provenance=TaskProvenance(
            source_type="suggestion_preview",
            source_id="sugg-1",
            suggestion_id="sugg-1",
        ),
    ).mark_error("preview failed")

    await handler(manifest, WorkerContext(worker_id="w1", tenant_id="demo"), ("r1",))

    memory_dir = worker_dir / "memory"
    links = load_linkage(memory_dir)
    assert links == ()
    index = load_index(memory_dir)
    assert len(index) == 1
    episode = load_episode(memory_dir, index[0].id)
    assert episode.source.type == "suggestion_preview_failure"
    updated_rule = next(rule for rule in load_rules(worker_dir / "rules") if rule.rule_id == "r1")
    assert updated_rule.confidence == 0.5


@pytest.mark.asyncio
async def test_error_feedback_skips_episode_when_episodic_write_disabled(tmp_path: Path):
    worker_dir = tmp_path / "tenants" / "demo" / "workers" / "w1"
    rules_dir = worker_dir / "rules" / "learned"
    rules_dir.mkdir(parents=True, exist_ok=True)
    (rules_dir / "r1.md").write_text(rule_to_markdown(_rule("r1")), encoding="utf-8")

    handler = build_error_feedback_handler(tmp_path, IndexFileLock())
    manifest = create_task_manifest(
        worker_id="w1",
        tenant_id="demo",
        skill_id="skill-1",
        task_description="do task",
    ).mark_error("boom")

    await handler(
        manifest,
        WorkerContext(
            worker_id="w1",
            tenant_id="demo",
            trust_gate=WorkerTrustGate(episodic_write_enabled=False),
        ),
        ("r1",),
    )

    memory_dir = worker_dir / "memory"
    assert load_index(memory_dir) == ()
    assert load_linkage(memory_dir) == ()
    updated_rule = next(rule for rule in load_rules(worker_dir / "rules") if rule.rule_id == "r1")
    assert updated_rule.confidence == 0.5
