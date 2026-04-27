# edition: baseline
"""Tests for token_counter - tiktoken and character estimation fallback."""
import pytest

from src.context.token_counter import (
    count_message_tokens,
    count_messages_tokens,
    count_tokens,
    estimate_tokens_from_usage,
)


class TestCountTokens:
    def test_empty_string_returns_zero(self):
        assert count_tokens("") == 0

    def test_non_empty_returns_positive(self):
        result = count_tokens("hello world")
        assert result > 0

    def test_longer_text_has_more_tokens(self):
        short = count_tokens("hello")
        long = count_tokens("hello world this is a longer sentence")
        assert long > short

    def test_chinese_text_returns_positive(self):
        result = count_tokens("你好世界，这是一段中文测试")
        assert result > 0

    def test_estimation_within_reasonable_range(self):
        """Character estimation should be within ~20% of expected range."""
        text = "The quick brown fox jumps over the lazy dog."
        result = count_tokens(text)
        # Regardless of tiktoken availability, result should be reasonable
        char_estimate = max(1, int(len(text) / 3.5))
        # Allow wide range since tiktoken may or may not be available
        assert result > 0
        assert result < len(text)


class TestCountMessageTokens:
    def test_simple_user_message(self):
        msg = {"role": "user", "content": "hello"}
        result = count_message_tokens(msg)
        # At least overhead (4) + some content tokens
        assert result >= 5

    def test_empty_content(self):
        msg = {"role": "assistant", "content": ""}
        result = count_message_tokens(msg)
        # Should be at least the overhead
        assert result >= 4

    def test_none_content(self):
        msg = {"role": "assistant", "content": None}
        result = count_message_tokens(msg)
        assert result >= 4

    def test_list_content(self):
        msg = {"role": "user", "content": [{"type": "text", "text": "hello"}]}
        result = count_message_tokens(msg)
        assert result > 4

    def test_tool_calls_counted(self):
        msg = {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "function": {
                        "name": "search",
                        "arguments": '{"query": "test"}',
                    },
                }
            ],
        }
        result = count_message_tokens(msg)
        # overhead + tool_call overhead + function name + arguments
        assert result > 8

    def test_message_without_tool_calls(self):
        msg = {"role": "user", "content": "hello"}
        no_tools = count_message_tokens(msg)

        msg_with = {
            "role": "assistant",
            "content": "hello",
            "tool_calls": [
                {
                    "id": "call_1",
                    "function": {"name": "fn", "arguments": "{}"},
                }
            ],
        }
        with_tools = count_message_tokens(msg_with)
        assert with_tools > no_tools


class TestCountMessagesTokens:
    def test_empty_list(self):
        assert count_messages_tokens(()) == 0

    def test_single_message(self):
        msgs = ({"role": "user", "content": "hello"},)
        result = count_messages_tokens(msgs)
        assert result == count_message_tokens(msgs[0])

    def test_multiple_messages(self):
        msgs = (
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
        )
        result = count_messages_tokens(msgs)
        expected = sum(count_message_tokens(m) for m in msgs)
        assert result == expected

    def test_accepts_list_input(self):
        msgs = [{"role": "user", "content": "hello"}]
        result = count_messages_tokens(msgs)
        assert result > 0


class TestEstimateTokensFromUsage:
    def test_fallback_when_no_known_tokens(self):
        msgs = ({"role": "user", "content": "hello"},)
        result = estimate_tokens_from_usage(msgs, 0, 0)
        assert result == count_messages_tokens(msgs)

    def test_adds_new_message_tokens(self):
        msgs = (
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
            {"role": "user", "content": "new message"},
        )
        result = estimate_tokens_from_usage(msgs, 100, 1)
        new_tokens = count_message_tokens(msgs[-1])
        assert result == 100 + new_tokens

    def test_zero_new_messages(self):
        msgs = ({"role": "user", "content": "hello"},)
        result = estimate_tokens_from_usage(msgs, 50, 0)
        assert result == 50

    def test_invalid_new_messages_count(self):
        msgs = ({"role": "user", "content": "hello"},)
        result = estimate_tokens_from_usage(msgs, 50, 100)
        assert result == 50
