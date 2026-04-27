# edition: baseline
"""Tests for compaction Layer 1 - tool result trimming."""
import pytest

from src.context.compaction.tool_trimmer import (
    _identify_round_boundaries,
    trim_old_tool_results,
)
from src.context.models import ContextWindowConfig


def _make_messages(*specs):
    """Build messages from (role, content, extra_dict?) specs."""
    msgs = []
    for spec in specs:
        if len(spec) == 3:
            role, content, extra = spec
            msgs.append({"role": role, "content": content, **extra})
        else:
            role, content = spec
            msgs.append({"role": role, "content": content})
    return tuple(msgs)


class TestTrimOldToolResults:
    def test_no_tool_messages_unchanged(self):
        msgs = _make_messages(
            ("system", "You are helpful"),
            ("user", "hello"),
            ("assistant", "hi there"),
        )
        config = ContextWindowConfig()
        result, compaction = trim_old_tool_results(msgs, 5, config)
        assert len(result) == len(msgs)
        assert compaction.layer == "tool_trim"
        assert compaction.success is True

    def test_recent_tool_results_preserved(self):
        msgs = _make_messages(
            ("system", "sys"),
            ("user", "task"),
            ("assistant", "", {"tool_calls": [{"id": "c1", "function": {"name": "f", "arguments": "{}"}}]}),
            ("tool", "result data", {"tool_call_id": "c1"}),
        )
        config = ContextWindowConfig(tool_trim_age_rounds=3)
        result, _ = trim_old_tool_results(msgs, 1, config)
        # Round 0 < cutoff (1 - 3 = -2), so it should NOT be trimmed
        assert result[3]["content"] == "result data"

    def test_old_tool_results_trimmed(self):
        msgs = _make_messages(
            ("system", "sys"),
            ("user", "task"),
            ("assistant", "resp1", {"tool_calls": [{"id": "c1", "function": {"name": "f", "arguments": "{}"}}]}),
            ("tool", "old result", {"tool_call_id": "c1"}),
            ("user", "next"),
            ("assistant", "resp2", {"tool_calls": [{"id": "c2", "function": {"name": "g", "arguments": "{}"}}]}),
            ("tool", "new result", {"tool_call_id": "c2"}),
        )
        config = ContextWindowConfig(tool_trim_age_rounds=1)
        result, compaction = trim_old_tool_results(msgs, 5, config)
        # Round 0 (assistant at idx 2) should be trimmed (round 0 < 5-1=4)
        assert result[3]["content"] == config.tool_trim_placeholder
        assert result[3]["tool_call_id"] == "c1"  # tool_call_id preserved

    def test_tool_call_id_preserved(self):
        msgs = _make_messages(
            ("assistant", "", {"tool_calls": [{"id": "c1", "function": {"name": "f", "arguments": "{}"}}]}),
            ("tool", "data", {"tool_call_id": "c1"}),
        )
        config = ContextWindowConfig(tool_trim_age_rounds=0)
        result, _ = trim_old_tool_results(msgs, 5, config)
        assert result[1]["tool_call_id"] == "c1"

    def test_message_count_unchanged(self):
        msgs = _make_messages(
            ("system", "sys"),
            ("assistant", "", {"tool_calls": [{"id": "c1", "function": {"name": "f", "arguments": "{}"}}]}),
            ("tool", "data", {"tool_call_id": "c1"}),
            ("assistant", "", {"tool_calls": [{"id": "c2", "function": {"name": "g", "arguments": "{}"}}]}),
            ("tool", "data2", {"tool_call_id": "c2"}),
        )
        config = ContextWindowConfig(tool_trim_age_rounds=0)
        result, _ = trim_old_tool_results(msgs, 10, config)
        assert len(result) == len(msgs)

    def test_tokens_decrease_after_trim(self):
        long_result = "x" * 10000
        msgs = _make_messages(
            ("assistant", "", {"tool_calls": [{"id": "c1", "function": {"name": "f", "arguments": "{}"}}]}),
            ("tool", long_result, {"tool_call_id": "c1"}),
        )
        config = ContextWindowConfig(tool_trim_age_rounds=0)
        _, compaction = trim_old_tool_results(msgs, 5, config)
        assert compaction.tokens_after < compaction.tokens_before

    def test_already_trimmed_not_retrimmed(self):
        config = ContextWindowConfig(tool_trim_age_rounds=0)
        msgs = _make_messages(
            ("assistant", ""),
            ("tool", config.tool_trim_placeholder, {"tool_call_id": "c1"}),
        )
        result, _ = trim_old_tool_results(msgs, 5, config)
        assert result[1]["content"] == config.tool_trim_placeholder

    def test_returns_new_tuple(self):
        msgs = _make_messages(
            ("user", "hello"),
            ("assistant", "hi"),
        )
        config = ContextWindowConfig()
        result, _ = trim_old_tool_results(msgs, 1, config)
        assert result is not msgs


class TestIdentifyRoundBoundaries:
    def test_empty_messages(self):
        assert _identify_round_boundaries(()) == ()

    def test_single_round(self):
        msgs = _make_messages(
            ("assistant", "resp"),
            ("tool", "data"),
        )
        result = _identify_round_boundaries(msgs)
        assert result == ((0, 2),)

    def test_multiple_rounds(self):
        msgs = _make_messages(
            ("user", "q1"),
            ("assistant", "a1"),
            ("tool", "d1"),
            ("user", "q2"),
            ("assistant", "a2"),
        )
        result = _identify_round_boundaries(msgs)
        assert len(result) == 2
        assert result[0] == (1, 3)
        assert result[1] == (4, 5)

    def test_no_tool_messages(self):
        msgs = _make_messages(
            ("user", "q"),
            ("assistant", "a"),
        )
        result = _identify_round_boundaries(msgs)
        assert result == ((1, 2),)
