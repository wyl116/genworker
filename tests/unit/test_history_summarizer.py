# edition: baseline
"""Tests for compaction Layer 3 - LLM history summarization."""
import pytest

from src.context.compaction.history_summarizer import (
    _format_messages_for_summary,
    summarize_history,
)
from src.context.models import ContextWindowConfig
from src.engine.protocols import LLMResponse


class MockLLMClient:
    """Mock LLM client for testing summarization."""

    def __init__(self, response_content: str = "Summary of conversation."):
        self._response = response_content
        self.invoke_count = 0

    async def invoke(self, messages=None, tools=None, tool_choice=None, system_blocks=None, intent=None):
        self.invoke_count += 1
        return LLMResponse(content=self._response)


class FailingLLMClient:
    """Mock LLM client that always raises."""

    async def invoke(self, messages=None, tools=None, tool_choice=None, system_blocks=None, intent=None):
        raise RuntimeError("LLM unavailable")


def _msgs(*specs):
    return tuple({"role": r, "content": c} for r, c in specs)


class TestSummarizeHistory:
    @pytest.mark.asyncio
    async def test_successful_summarization(self):
        msgs = _msgs(
            ("system", "sys"),
            ("user", "task"),
            ("user", "q1"),
            ("assistant", "a1 " * 100),
            ("user", "q2"),
            ("assistant", "a2 " * 100),
        )
        client = MockLLMClient("Concise summary.")
        config = ContextWindowConfig()
        result, compaction = await summarize_history(msgs, client, config)

        assert compaction.layer == "history_summarize"
        assert compaction.success is True
        assert compaction.summary_generated == "Concise summary."
        # Should preserve system + first user + add summary
        assert result[0]["role"] == "system"
        assert result[1]["role"] == "user"
        assert "[Conversation history summary]" in result[2]["content"]
        assert len(result) == 3

    @pytest.mark.asyncio
    async def test_preserves_system_and_first_user(self):
        msgs = _msgs(
            ("system", "sys prompt"),
            ("user", "original task"),
            ("assistant", "response"),
        )
        client = MockLLMClient("Summary.")
        config = ContextWindowConfig()
        result, _ = await summarize_history(msgs, client, config)
        assert result[0]["content"] == "sys prompt"
        assert result[1]["content"] == "original task"

    @pytest.mark.asyncio
    async def test_circuit_breaker_after_3_failures(self):
        msgs = _msgs(
            ("system", "sys"),
            ("user", "task"),
            ("assistant", "resp"),
        )
        client = MockLLMClient("summary")
        config = ContextWindowConfig()
        result, compaction = await summarize_history(
            msgs, client, config, consecutive_failures=3,
        )
        assert compaction.success is False
        assert "Circuit breaker" in compaction.error
        assert client.invoke_count == 0  # Never called

    @pytest.mark.asyncio
    async def test_handles_llm_failure(self):
        msgs = _msgs(
            ("system", "sys"),
            ("user", "task"),
            ("assistant", "resp"),
        )
        client = FailingLLMClient()
        config = ContextWindowConfig()
        result, compaction = await summarize_history(msgs, client, config)
        assert compaction.success is False
        assert "LLM unavailable" in compaction.error
        assert result == msgs  # Original messages returned

    @pytest.mark.asyncio
    async def test_empty_summary_is_failure(self):
        msgs = _msgs(
            ("system", "sys"),
            ("user", "task"),
            ("assistant", "resp"),
        )
        client = MockLLMClient("")
        config = ContextWindowConfig()
        result, compaction = await summarize_history(msgs, client, config)
        assert compaction.success is False

    @pytest.mark.asyncio
    async def test_no_history_to_summarize(self):
        msgs = _msgs(
            ("system", "sys"),
            ("user", "task"),
        )
        client = MockLLMClient("summary")
        config = ContextWindowConfig()
        result, compaction = await summarize_history(msgs, client, config)
        assert compaction.success is True
        assert result == msgs

    @pytest.mark.asyncio
    async def test_tokens_decrease_after_summary(self):
        msgs = _msgs(
            ("system", "sys"),
            ("user", "task"),
            ("assistant", "long response " * 200),
            ("user", "follow up"),
            ("assistant", "another long response " * 200),
        )
        client = MockLLMClient("Brief summary.")
        config = ContextWindowConfig()
        _, compaction = await summarize_history(msgs, client, config)
        assert compaction.tokens_after < compaction.tokens_before


class TestFormatMessagesForSummary:
    def test_formats_basic_messages(self):
        msgs = _msgs(("user", "hello"), ("assistant", "hi"))
        result = _format_messages_for_summary(msgs)
        assert "[user] hello" in result
        assert "[assistant] hi" in result

    def test_formats_tool_calls(self):
        msgs = (
            {
                "role": "assistant",
                "content": "calling tool",
                "tool_calls": [
                    {"function": {"name": "search", "arguments": '{"q":"test"}'}}
                ],
            },
        )
        result = _format_messages_for_summary(msgs)
        assert "search" in result
        assert "test" in result

    def test_handles_none_content(self):
        msgs = ({"role": "assistant", "content": None},)
        result = _format_messages_for_summary(msgs)
        assert "[assistant]" in result

    def test_handles_list_content(self):
        msgs = ({"role": "user", "content": [{"type": "text", "text": "hi"}]},)
        result = _format_messages_for_summary(msgs)
        assert "[user]" in result
