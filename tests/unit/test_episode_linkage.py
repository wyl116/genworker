# edition: baseline
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.memory.episodic.linkage import (
    apply_outcome_feedback,
    compute_rule_effectiveness,
    create_links,
    load_linkage,
    write_linkage,
)
from src.memory.episodic.models import Episode, EpisodeIndex, EpisodeSource
from src.memory.episodic.store import write_episode
from src.worker.rules.models import Rule, RuleScope, RuleSource, rule_to_markdown


def _rule(rule_id: str) -> Rule:
    return Rule(
        rule_id=rule_id,
        type="learned",
        category="strategy",
        status="active",
        rule="Validate inputs",
        reason="Because quality",
        scope=RuleScope(),
        source=RuleSource(
            type="self_reflection",
            evidence="test",
            created_at="2026-04-09T00:00:00+00:00",
        ),
        confidence=0.5,
        apply_count=1,
    )


@pytest.mark.asyncio
async def test_write_load_and_feedback_roundtrip(tmp_path):
    memory_dir = tmp_path / "memory"
    rules_dir = tmp_path / "rules"
    (rules_dir / "learned").mkdir(parents=True, exist_ok=True)
    (rules_dir / "learned" / "r1.md").write_text(
        rule_to_markdown(_rule("r1")),
        encoding="utf-8",
    )
    episode = Episode(
        episode_id="ep-1",
        created_at=datetime.now(timezone.utc).isoformat(),
        source=EpisodeSource(type="task_completion", skill_used="s1"),
        summary="Task completed successfully",
        key_findings=(),
        related_entities=(),
    )
    write_episode(memory_dir, episode)
    links = create_links("ep-1", ("r1",), episode.created_at)
    write_linkage(memory_dir, links)

    assert load_linkage(memory_dir) == links

    updated_rule_ids = await apply_outcome_feedback("ep-1", "success", memory_dir, rules_dir)
    assert updated_rule_ids == ("r1",)

    # Repeating the same outcome should not double-apply.
    repeated = await apply_outcome_feedback("ep-1", "success", memory_dir, rules_dir)
    assert repeated == ()


def test_compute_rule_effectiveness():
    links = create_links("ep-1", ("r1",), "2026-04-09T00:00:00+00:00") + create_links(
        "ep-2", ("r1",), "2026-04-09T00:00:00+00:00",
    )
    episodes = (
        EpisodeIndex("ep-1", "2026-04-09T00:00:00+00:00", "Task completed", (), (), (), (), 0.9),
        EpisodeIndex("ep-2", "2026-04-09T00:00:00+00:00", "Task failed: bad", (), (), (), (), 0.5),
    )
    effectiveness = compute_rule_effectiveness(links, episodes)
    assert effectiveness["r1"] == 0.5
