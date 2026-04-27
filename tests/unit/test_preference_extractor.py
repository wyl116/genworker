# edition: baseline
from __future__ import annotations

from src.memory.preferences.extractor import (
    extract_decisions,
    extract_preferences,
    merge_preferences,
    supersede_decisions,
)


def test_extract_chinese_preference():
    results = extract_preferences("我喜欢表格格式的报告")

    assert len(results) == 1
    assert results[0].category == "format"
    assert "表格格式" in results[0].content


def test_extract_decision_direct():
    results = extract_decisions("就用 Redis 作为共享存储吧")

    assert len(results) == 1
    assert results[0].topic == "storage"
    assert "Redis" in results[0].decision


def test_merge_duplicate_preference_boosts_confidence():
    first = extract_preferences("我喜欢简洁的报告")[0]
    second = extract_preferences("我喜欢简洁的报告")[0]

    merged = merge_preferences((first,), (second,))

    assert len(merged) == 1
    assert merged[0].confidence > first.confidence


def test_supersede_same_topic_decision():
    first = extract_decisions("就用 Redis 作为共享存储吧")[0]
    second = extract_decisions("最终改成 PostgreSQL 作为共享存储")[0]

    merged = supersede_decisions((first,), (second,))
    old = next(item for item in merged if item.decision_id == first.decision_id)
    new = next(item for item in merged if item.decision_id == second.decision_id)

    assert old.superseded_by == new.decision_id
    assert new.superseded_by == ""


def test_supersede_same_topic_same_decision_is_noop():
    first = extract_decisions("就用 Redis 作为共享存储吧")[0]
    second = extract_decisions("就用 Redis 作为共享存储吧")[0]

    merged = supersede_decisions((first,), (second,))

    assert merged == (first,)
