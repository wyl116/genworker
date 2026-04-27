# edition: baseline
"""Tests for compaction Layer 2 - history pruning by API round."""
import pytest

from src.context.compaction.history_pruner import (
    compute_group_metrics,
    group_by_api_round,
    prune_oldest_rounds,
)
from src.context.models import ContextWindowConfig
from src.context.token_counter import count_messages_tokens


def _msgs(*specs):
    """Build message list from (role, content) specs."""
    return tuple({"role": r, "content": c} for r, c in specs)


class TestPruneOldestRounds:
    def test_within_budget_unchanged(self):
        msgs = _msgs(
            ("system", "sys"),
            ("user", "task"),
            ("assistant", "hi"),
        )
        result, compaction = prune_oldest_rounds(msgs, 100000, ContextWindowConfig())
        assert len(result) == len(msgs)
        assert compaction.success is True

    def test_system_and_first_user_always_preserved(self):
        msgs = _msgs(
            ("system", "sys"),
            ("user", "task"),
            ("user", "q1"),
            ("assistant", "a1 " * 500),
            ("user", "q2"),
            ("assistant", "a2 " * 500),
        )
        # Target very low to force pruning
        result, _ = prune_oldest_rounds(msgs, 50, ContextWindowConfig())
        assert result[0]["role"] == "system"
        assert result[0]["content"] == "sys"
        assert result[1]["role"] == "user"
        assert result[1]["content"] == "task"

    def test_oldest_rounds_pruned_first(self):
        msgs = _msgs(
            ("system", "sys"),
            ("user", "task"),
            ("user", "q1"),
            ("assistant", "a1 " * 100),
            ("user", "q2"),
            ("assistant", "a2 " * 100),
            ("user", "q3"),
            ("assistant", "a3 " * 100),
        )
        tokens = count_messages_tokens(msgs)
        # Target removes ~1 round
        target = tokens - 200
        result, compaction = prune_oldest_rounds(msgs, target, ContextWindowConfig())
        assert len(result) < len(msgs)
        # Latest content should still be present
        contents = [m["content"] for m in result]
        assert any("a3" in c for c in contents)

    def test_returns_new_tuple(self):
        msgs = _msgs(
            ("system", "sys"),
            ("user", "task"),
            ("assistant", "a" * 1000),
        )
        result, _ = prune_oldest_rounds(msgs, 10, ContextWindowConfig())
        assert result is not msgs

    def test_empty_messages(self):
        result, compaction = prune_oldest_rounds((), 100, ContextWindowConfig())
        assert result == ()
        assert compaction.success is True


class TestGroupByApiRound:
    def test_preserves_system_and_first_user(self):
        msgs = _msgs(
            ("system", "sys"),
            ("user", "task"),
            ("assistant", "a1"),
        )
        preserved, groups = group_by_api_round(msgs)
        assert len(preserved) == 2
        assert preserved[0]["content"] == "sys"
        assert preserved[1]["content"] == "task"

    def test_groups_by_user_boundary(self):
        msgs = _msgs(
            ("system", "sys"),
            ("user", "task"),
            ("user", "q1"),
            ("assistant", "a1"),
            ("user", "q2"),
            ("assistant", "a2"),
        )
        preserved, groups = group_by_api_round(msgs)
        assert len(groups) == 2
        assert groups[0][0]["content"] == "q1"
        assert groups[1][0]["content"] == "q2"

    def test_no_remaining_messages(self):
        msgs = _msgs(
            ("system", "sys"),
            ("user", "task"),
        )
        preserved, groups = group_by_api_round(msgs)
        assert len(preserved) == 2
        assert groups == ()

    def test_empty_messages(self):
        preserved, groups = group_by_api_round(())
        assert preserved == ()
        assert groups == ()

    def test_tool_messages_grouped_with_assistant(self):
        msgs = (
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "task"},
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]},
            {"role": "tool", "content": "result", "tool_call_id": "c1"},
        )
        preserved, groups = group_by_api_round(msgs)
        assert len(groups) == 1
        assert len(groups[0]) == 3  # user + assistant + tool


class TestComputeGroupMetrics:
    def test_basic_metrics(self):
        groups = (
            _msgs(("user", "q"), ("assistant", "a")),
            _msgs(("user", "q2"), ("assistant", "a2")),
        )
        metrics = compute_group_metrics(groups, 5)
        assert len(metrics) == 2
        assert metrics[0].group_index == 0
        assert metrics[0].message_count == 2
        assert metrics[0].token_count > 0
        assert metrics[0].round_age == 5

    def test_detects_tool_calls(self):
        groups = (
            (
                {"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]},
                {"role": "tool", "content": "result"},
            ),
        )
        metrics = compute_group_metrics(groups, 1)
        assert metrics[0].has_tool_calls is True

    def test_no_tool_calls(self):
        groups = (_msgs(("user", "q"), ("assistant", "a")),)
        metrics = compute_group_metrics(groups, 1)
        assert metrics[0].has_tool_calls is False
