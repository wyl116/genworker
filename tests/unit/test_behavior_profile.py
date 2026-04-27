# edition: baseline
from pathlib import Path

from src.memory.episodic.models import EpisodeIndex
from src.worker.duty.models import DutyExecutionRecord
from src.worker.profile.models import BehavioralTrend
from src.worker.profile.updater import (
    compute_behavior_profile,
    detect_behavioral_trends,
    load_profile,
    write_profile,
)
from src.worker.rules.models import Rule, RuleScope, RuleSource


def _rule(rule_id: str, confidence: float, apply_count: int) -> Rule:
    return Rule(
        rule_id=rule_id,
        type="learned",
        category="strategy",
        status="active",
        rule="Use tests",
        reason="quality",
        scope=RuleScope(),
        source=RuleSource(
            type="self_reflection",
            evidence="summary",
            created_at="2026-04-10T00:00:00+00:00",
        ),
        confidence=confidence,
        apply_count=apply_count,
    )


def _record(duration: float, escalated: bool = False) -> DutyExecutionRecord:
    return DutyExecutionRecord(
        execution_id="exec-1",
        duty_id="duty-1",
        trigger_id="t-1",
        depth="standard",
        executed_at="2026-04-10T00:00:00+00:00",
        duration_seconds=duration,
        conclusion="ok",
        escalated=escalated,
    )


def test_compute_profile_and_roundtrip(tmp_path: Path):
    episodes = (
        EpisodeIndex("ep-1", "2026-04-09T00:00:00+00:00", "Write summary for report", (), ("writing",), (), (), 0.9),
        EpisodeIndex("ep-2", "2026-04-08T00:00:00+00:00", "Analyze production issue", (), ("analysis",), (), (), 0.8),
        EpisodeIndex("ep-3", "2026-04-07T00:00:00+00:00", "Write another report", (), ("writing",), (), (), 0.7),
    )
    profile = compute_behavior_profile(
        worker_id="w1",
        episodes=episodes,
        rules=(_rule("r1", 0.9, 8), _rule("r2", 0.7, 3)),
        duty_records=(_record(10.0), _record(20.0)),
        current_date="2026-04-10T00:00:00+00:00",
    )

    assert profile.task_count_total == 3
    assert profile.skill_usage[0].skill_id == "writing"
    assert profile.avg_task_duration_seconds == 15.0

    write_profile(tmp_path, profile)
    loaded = load_profile(tmp_path)
    assert loaded == profile


def test_detect_behavioral_trends():
    previous = compute_behavior_profile(
        worker_id="w1",
        episodes=(
            EpisodeIndex("ep-1", "2026-03-01T00:00:00+00:00", "Write draft", (), ("writing",), (), (), 0.9),
            EpisodeIndex("ep-2", "2026-03-01T00:00:00+00:00", "Analyze logs", (), ("analysis",), (), (), 0.8),
            EpisodeIndex("ep-3", "2026-03-01T00:00:00+00:00", "Analyze report", (), ("analysis",), (), (), 0.8),
        ),
        rules=(_rule("r1", 0.6, 2),),
        duty_records=(_record(30.0), _record(30.0)),
        current_date="2026-03-31T00:00:00+00:00",
    )
    current = compute_behavior_profile(
        worker_id="w1",
        episodes=(
            EpisodeIndex("ep-4", "2026-04-01T00:00:00+00:00", "Write draft", (), ("writing",), (), (), 0.9),
            EpisodeIndex("ep-5", "2026-04-01T00:00:00+00:00", "Write spec", (), ("writing",), (), (), 0.9),
            EpisodeIndex("ep-6", "2026-04-01T00:00:00+00:00", "Write docs", (), ("writing",), (), (), 0.9),
            EpisodeIndex("ep-7", "2026-04-01T00:00:00+00:00", "Analyze small diff", (), ("analysis",), (), (), 0.8),
        ),
        rules=(_rule("r1", 0.9, 10), _rule("r2", 0.85, 8)),
        duty_records=(_record(20.0), _record(20.0)),
        current_date="2026-04-10T00:00:00+00:00",
    )

    trends = detect_behavioral_trends(current, previous)
    trend_types = {trend.trend_type for trend in trends}
    assert "skill_shift" in trend_types
    assert "efficiency_change" in trend_types
    assert "rule_convergence" in trend_types
