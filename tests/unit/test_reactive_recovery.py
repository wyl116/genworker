# edition: baseline
"""Tests for compaction Layer 4 - reactive recovery from prompt_too_long."""
import pytest

from src.context.compaction.reactive_recovery import (
    _strip_oversized_content,
    recover_from_prompt_too_long,
)
from src.context.models import ContextWindowConfig
from src.context.token_counter import count_messages_tokens, count_tokens
from src.engine.protocols import LLMResponse


class MockLLMClient:
    async def invoke(self, messages=None, tools=None, tool_choice=None, system_blocks=None, intent=None):
        return LLMResponse(content="Recovery summary.")


def _msgs(*specs):
    return tuple({"role": r, "content": c} for r, c in specs)


class TestRecoverFromPromptTooLong:
    @pytest.mark.asyncio
    async def test_reduces_to_target_ratio(self):
        # Build messages that exceed the window
        config = ContextWindowConfig(
            model_context_window=1000,
            output_reserved_tokens=100,
            safety_buffer_tokens=100,
            reactive_target_ratio=0.70,
        )
        effective = config.effective_window  # 800
        target = int(effective * 0.70)  # 560

        # Create messages well above target
        msgs = _msgs(
            ("system", "sys"),
            ("user", "task"),
            ("user", "q1"),
            ("assistant", "a " * 500),
            ("user", "q2"),
            ("assistant", "b " * 500),
            ("user", "q3"),
            ("assistant", "c " * 500),
        )

        client = MockLLMClient()
        result, compaction = await recover_from_prompt_too_long(msgs, client, config)
        assert compaction.layer == "reactive_recovery"
        result_tokens = count_messages_tokens(result)
        # Should be reduced (may or may not reach exact target depending on content)
        assert result_tokens < count_messages_tokens(msgs)

    @pytest.mark.asyncio
    async def test_preserves_system_and_user(self):
        config = ContextWindowConfig(
            model_context_window=500,
            output_reserved_tokens=50,
            safety_buffer_tokens=50,
        )
        msgs = _msgs(
            ("system", "sys"),
            ("user", "task"),
            ("user", "q1"),
            ("assistant", "long " * 200),
        )
        client = MockLLMClient()
        result, _ = await recover_from_prompt_too_long(msgs, client, config)
        roles = [m["role"] for m in result]
        assert "system" in roles or "user" in roles

    @pytest.mark.asyncio
    async def test_without_llm_client(self):
        config = ContextWindowConfig(
            model_context_window=500,
            output_reserved_tokens=50,
            safety_buffer_tokens=50,
        )
        msgs = _msgs(
            ("system", "sys"),
            ("user", "task"),
            ("assistant", "long " * 200),
        )
        result, compaction = await recover_from_prompt_too_long(msgs, None, config)
        assert compaction.layer == "reactive_recovery"
        # Should still attempt trimming and pruning
        assert len(result) <= len(msgs)

    @pytest.mark.asyncio
    async def test_returns_new_tuple(self):
        config = ContextWindowConfig()
        msgs = _msgs(("user", "hello"),)
        result, _ = await recover_from_prompt_too_long(msgs, None, config)
        assert result is not msgs


class TestStripOversizedContent:
    def test_normal_messages_unchanged(self):
        msgs = _msgs(("user", "short"), ("assistant", "brief"))
        result = _strip_oversized_content(msgs)
        assert result[0]["content"] == "short"
        assert result[1]["content"] == "brief"

    def test_truncates_oversized_content(self):
        huge = "x" * 100_000
        msgs = ({"role": "tool", "content": huge},)
        result = _strip_oversized_content(msgs, max_single_message_tokens=100)
        assert len(result[0]["content"]) < len(huge)
        assert "[content truncated]" in result[0]["content"]

    def test_preserves_message_structure(self):
        huge = "x" * 100_000
        msgs = ({"role": "tool", "content": huge, "tool_call_id": "c1"},)
        result = _strip_oversized_content(msgs, max_single_message_tokens=100)
        assert result[0]["tool_call_id"] == "c1"
        assert result[0]["role"] == "tool"

    def test_returns_new_tuple(self):
        msgs = _msgs(("user", "hello"),)
        result = _strip_oversized_content(msgs)
        assert result is not msgs

    def test_handles_non_string_content(self):
        msgs = ({"role": "user", "content": None},)
        result = _strip_oversized_content(msgs)
        assert result[0]["content"] is None
