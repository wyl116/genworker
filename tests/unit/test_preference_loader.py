# edition: baseline
from __future__ import annotations

from pathlib import Path

from src.memory.preferences.extractor import (
    UserDecision,
    UserPreference,
    load_active_decisions,
    load_decisions,
    load_preferences,
    save_decisions,
    save_preferences,
)


def test_save_and_load_preferences(tmp_path: Path):
    path = tmp_path / "preferences.jsonl"
    preferences = (
        UserPreference(
            preference_id="pref-1",
            category="format",
            content="表格格式",
            confidence=0.7,
            extracted_from="我喜欢表格格式",
            extracted_at="2026-04-10T00:00:00+00:00",
        ),
    )

    save_preferences(path, preferences)

    loaded = load_preferences(path)
    assert loaded == preferences


def test_load_active_decisions_filters_superseded(tmp_path: Path):
    path = tmp_path / "decisions.jsonl"
    decisions = (
        UserDecision(
            decision_id="dec-1",
            topic="storage",
            decision="使用 Redis",
            confidence=0.7,
            decided_at="2026-04-10T00:00:00+00:00",
            context="就用 Redis",
            superseded_by="dec-2",
        ),
        UserDecision(
            decision_id="dec-2",
            topic="storage",
            decision="使用 PostgreSQL",
            confidence=0.7,
            decided_at="2026-04-11T00:00:00+00:00",
            context="改成 PostgreSQL",
        ),
    )

    save_decisions(path, decisions)

    assert tuple(item.decision_id for item in load_decisions(path)) == ("dec-2", "dec-1")
    assert tuple(item.decision_id for item in load_active_decisions(path)) == ("dec-2",)
