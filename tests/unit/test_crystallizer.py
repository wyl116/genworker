# edition: baseline
from pathlib import Path

import pytest

from src.skills.parser import SkillParser
from src.worker.lifecycle.suggestion_store import SuggestionStore
from src.worker.rules.crystallizer import (
    identify_crystallization_candidates,
    crystallize_to_skill,
    run_crystallization_cycle,
)
from src.worker.rules.models import Rule, RuleScope, RuleSource
from src.worker.rules.rule_manager import load_rules
from src.worker.rules.models import rule_to_markdown


class _FakeMCPServer:
    def __init__(self) -> None:
        self.tools = []

    def register_tool(self, tool) -> None:
        self.tools.append(tool)


def _rule(
    rule_text: str = "Step 1 collect data. Step 2 validate schema. Step 3 write summary.",
    category: str = "strategy",
) -> Rule:
    return Rule(
        rule_id="rule-1",
        type="learned",
        category=category,
        status="active",
        rule=rule_text,
        reason="stable process",
        scope=RuleScope(),
        source=RuleSource(
            type="self_reflection",
            evidence="summary",
            created_at="2026-04-09T00:00:00+00:00",
        ),
        confidence=0.95,
        apply_count=25,
    )


def test_identify_skill_candidate():
    candidates = identify_crystallization_candidates((_rule(),))
    assert len(candidates) == 1
    assert candidates[0].target == "skill"


def test_preference_rule_not_crystallized():
    candidates = identify_crystallization_candidates((_rule(category="preference"),))
    assert candidates[0].target == "none"


@pytest.mark.asyncio
async def test_crystallize_to_skill_roundtrip(tmp_path: Path):
    candidate = identify_crystallization_candidates((_rule(),))[0]
    result = await crystallize_to_skill(candidate, tmp_path, llm_client=None)
    skill = SkillParser.parse(Path(result.artifact_path))

    assert result.success is True
    assert skill.skill_id.startswith("crystallized-")
    assert skill.name == skill.skill_id
    assert skill.source_format == "genworker_v2"
    assert skill.description
    assert skill.strategy.mode.value == "autonomous"
    assert skill.keywords


@pytest.mark.asyncio
async def test_run_crystallization_cycle_creates_rule_to_skill_suggestion_when_store_available(tmp_path: Path):
    rules_dir = tmp_path / "rules"
    skills_dir = tmp_path / "skills"
    learned_dir = rules_dir / "learned"
    learned_dir.mkdir(parents=True, exist_ok=True)
    (learned_dir / "rule-1.md").write_text(rule_to_markdown(_rule()), encoding="utf-8")
    suggestion_store = SuggestionStore(tmp_path)

    results = await run_crystallization_cycle(
        rules_dir=rules_dir,
        skills_dir=skills_dir,
        mcp_server=None,
        llm_client=None,
        suggestion_store=suggestion_store,
        tenant_id="tenant-1",
        worker_id="worker-1",
    )

    assert len(results) == 1
    assert results[0].success is True
    assert results[0].target == "skill"
    assert results[0].outcome == "queued"
    assert results[0].artifact_path == ""
    assert results[0].artifact_ref
    pending = suggestion_store.list_pending("tenant-1", "worker-1")
    assert len(pending) == 1
    assert pending[0].type == "rule_to_skill"
    assert pending[0].payload_dict["source_rule_id"] == "rule-1"
    assert results[0].artifact_ref == pending[0].suggestion_id
    skill_id = pending[0].payload_dict["skill_id"]
    assert not (skills_dir / skill_id / "SKILL.md").exists()
    loaded_rule = next(rule for rule in load_rules(rules_dir) if rule.rule_id == "rule-1")
    assert loaded_rule.status == "active"


@pytest.mark.asyncio
async def test_run_crystallization_cycle_falls_back_to_direct_write_without_store(tmp_path: Path):
    rules_dir = tmp_path / "rules"
    skills_dir = tmp_path / "skills"
    learned_dir = rules_dir / "learned"
    learned_dir.mkdir(parents=True, exist_ok=True)
    (learned_dir / "rule-1.md").write_text(rule_to_markdown(_rule()), encoding="utf-8")

    results = await run_crystallization_cycle(
        rules_dir=rules_dir,
        skills_dir=skills_dir,
        mcp_server=None,
        llm_client=None,
    )

    assert len(results) == 1
    assert results[0].success is True
    assert results[0].artifact_path
    assert Path(results[0].artifact_path).exists()
    loaded_rule = next(rule for rule in load_rules(rules_dir) if rule.rule_id == "rule-1")
    assert loaded_rule.status == "crystallized"


@pytest.mark.asyncio
async def test_tool_crystallization_unchanged_when_store_available(tmp_path: Path):
    rules_dir = tmp_path / "rules"
    skills_dir = tmp_path / "skills"
    learned_dir = rules_dir / "learned"
    learned_dir.mkdir(parents=True, exist_ok=True)
    tool_rule = _rule(
        rule_text="When report arrives, do summarize and notify.",
        category="strategy",
    )
    (learned_dir / "rule-1.md").write_text(rule_to_markdown(tool_rule), encoding="utf-8")
    suggestion_store = SuggestionStore(tmp_path)
    mcp_server = _FakeMCPServer()

    results = await run_crystallization_cycle(
        rules_dir=rules_dir,
        skills_dir=skills_dir,
        mcp_server=mcp_server,
        llm_client=None,
        suggestion_store=suggestion_store,
        tenant_id="tenant-1",
        worker_id="worker-1",
    )

    assert len(results) == 1
    assert results[0].success is True
    assert results[0].target == "tool"
    assert len(mcp_server.tools) == 1
    assert suggestion_store.list_pending("tenant-1", "worker-1") == ()
    loaded_rule = next(rule for rule in load_rules(rules_dir) if rule.rule_id == "rule-1")
    assert loaded_rule.status == "crystallized"


@pytest.mark.asyncio
async def test_run_crystallization_cycle_requires_context_when_store_available(tmp_path: Path):
    rules_dir = tmp_path / "rules"
    skills_dir = tmp_path / "skills"
    learned_dir = rules_dir / "learned"
    learned_dir.mkdir(parents=True, exist_ok=True)
    (learned_dir / "rule-1.md").write_text(rule_to_markdown(_rule()), encoding="utf-8")

    with pytest.raises(ValueError, match="tenant_id and worker_id"):
        await run_crystallization_cycle(
            rules_dir=rules_dir,
            skills_dir=skills_dir,
            mcp_server=None,
            llm_client=None,
            suggestion_store=SuggestionStore(tmp_path),
        )


@pytest.mark.asyncio
async def test_run_crystallization_cycle_treats_duplicate_pending_as_skipped_not_failure(tmp_path: Path):
    rules_dir = tmp_path / "rules"
    skills_dir = tmp_path / "skills"
    learned_dir = rules_dir / "learned"
    learned_dir.mkdir(parents=True, exist_ok=True)
    rule = _rule()
    (learned_dir / "rule-1.md").write_text(rule_to_markdown(rule), encoding="utf-8")
    suggestion_store = SuggestionStore(tmp_path)
    first = await run_crystallization_cycle(
        rules_dir=rules_dir,
        skills_dir=skills_dir,
        mcp_server=None,
        llm_client=None,
        suggestion_store=suggestion_store,
        tenant_id="tenant-1",
        worker_id="worker-1",
    )
    second = await run_crystallization_cycle(
        rules_dir=rules_dir,
        skills_dir=skills_dir,
        mcp_server=None,
        llm_client=None,
        suggestion_store=suggestion_store,
        tenant_id="tenant-1",
        worker_id="worker-1",
    )

    assert first[0].outcome == "queued"
    assert second[0].success is True
    assert second[0].outcome == "skipped"
    assert second[0].error == "duplicate pending suggestion"
