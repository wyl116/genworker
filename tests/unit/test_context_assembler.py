# edition: baseline
"""Tests for context assembler - segment building, prompt assembly, and full pipeline."""
import pytest

from src.context.assembler import assemble_context, assemble_system_prompt, build_segments
from src.context.models import (
    ContextSegment,
    ContextWindowConfig,
    SegmentPriority,
)
from src.engine.protocols import LLMResponse


_PRIORITY = SegmentPriority()


class MockLLMClient:
    async def invoke(self, messages=None, tools=None, tool_choice=None, system_blocks=None, intent=None):
        return LLMResponse(
            content="\n".join((
                "**目标**：完成任务",
                "- **进展**：整理上下文",
                "- **决策**：保留关键事实",
                "- **待办**：继续验证",
                "- **关键上下文**：需要结构化摘要",
            )),
        )


class MockMemoryOrchestrator:
    def __init__(self):
        self.calls = 0

    async def on_pre_compress(self, messages):
        self.calls += 1


class TestBuildSegments:
    def test_creates_all_segments(self):
        config = ContextWindowConfig()
        segments = build_segments(
            identity="I am an assistant",
            principles="Be helpful",
            constraints="No PII",
            directives="Follow rules",
            contact_context="Alice from Finance",
            learned_rules="Rule 1",
            episodic_context="Past event",
            duty_context="Daily check",
            goal_context="Improve quality",
            task_context="Current task",
            config=config,
        )
        names = tuple(s.name for s in segments)
        assert "identity" in names
        assert "principles" in names
        assert "constraints" in names
        assert "directives" in names
        assert "contact_context" in names
        assert "learned_rules" in names
        assert "episodic_memory" in names
        assert "duty_context" in names
        assert "goal_context" in names
        assert "task_context" in names

    def test_empty_content_zero_tokens(self):
        config = ContextWindowConfig()
        segments = build_segments(
            identity="", principles="", constraints="",
            directives="", contact_context="", learned_rules="", episodic_context="",
            duty_context="", goal_context="", task_context="",
            config=config,
        )
        for seg in segments:
            assert seg.token_count == 0

    def test_non_empty_positive_tokens(self):
        config = ContextWindowConfig()
        segments = build_segments(
            identity="I am a bot", principles="", constraints="",
            directives="", contact_context="", learned_rules="", episodic_context="",
            duty_context="", goal_context="", task_context="",
            config=config,
        )
        identity_seg = next(s for s in segments if s.name == "identity")
        assert identity_seg.token_count > 0

    def test_priority_assignment(self):
        config = ContextWindowConfig()
        segments = build_segments(
            identity="id", principles="pr", constraints="co",
            directives="di", contact_context="cc", learned_rules="lr", episodic_context="ep",
            duty_context="du", goal_context="go", task_context="ta",
            config=config,
        )
        seg_map = {s.name: s for s in segments}
        assert seg_map["identity"].priority == _PRIORITY.IDENTITY
        assert seg_map["contact_context"].priority == _PRIORITY.CONTACT_CONTEXT
        assert seg_map["episodic_memory"].priority == _PRIORITY.EPISODIC_MEMORY
        assert seg_map["learned_rules"].priority == _PRIORITY.LEARNED_RULES

    def test_compressible_flags(self):
        config = ContextWindowConfig()
        segments = build_segments(
            identity="id", principles="pr", constraints="co",
            directives="di", contact_context="cc", learned_rules="lr", episodic_context="ep",
            duty_context="du", goal_context="go", task_context="ta",
            config=config,
        )
        seg_map = {s.name: s for s in segments}
        # Fixed segments are not compressible
        assert seg_map["identity"].compressible is False
        assert seg_map["principles"].compressible is False
        # Elastic segments are compressible
        assert seg_map["contact_context"].compressible is True
        assert seg_map["learned_rules"].compressible is True
        assert seg_map["episodic_memory"].compressible is True

    def test_max_tokens_from_config(self):
        config = ContextWindowConfig(identity_max_tokens=999)
        segments = build_segments(
            identity="id", principles="", constraints="",
            directives="", contact_context="", learned_rules="", episodic_context="",
            duty_context="", goal_context="", task_context="",
            config=config,
        )
        identity_seg = next(s for s in segments if s.name == "identity")
        assert identity_seg.max_tokens == 999


class TestAssembleSystemPrompt:
    def test_joins_non_empty_segments(self):
        segments = (
            ContextSegment("a", "Content A", 10, 0),
            ContextSegment("b", "Content B", 5, 1),
        )
        result = assemble_system_prompt(segments)
        assert "Content A" in result
        assert "Content B" in result
        assert "\n\n" in result

    def test_skips_empty_segments(self):
        segments = (
            ContextSegment("a", "Content A", 10, 0),
            ContextSegment("b", "", 0, 1),
            ContextSegment("c", "Content C", 8, 2),
        )
        result = assemble_system_prompt(segments)
        assert "Content A" in result
        assert "Content C" in result
        # Should not have extra blank separators
        assert "\n\n\n\n" not in result

    def test_all_empty_returns_empty(self):
        segments = (
            ContextSegment("a", "", 0, 0),
            ContextSegment("b", "", 0, 1),
        )
        result = assemble_system_prompt(segments)
        assert result == ""


class TestAssembleContext:
    @pytest.mark.asyncio
    async def test_basic_assembly(self):
        config = ContextWindowConfig()
        msgs = (
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        )
        result = await assemble_context(
            identity="I am a bot",
            principles="Be helpful",
            constraints="No PII",
            directives="",
            contact_context="Alice from Finance",
            learned_rules="",
            episodic_context="",
            duty_context="",
            goal_context="",
            task_context="",
            messages=msgs,
            config=config,
        )
        assert result.system_prompt != ""
        assert "I am a bot" in result.system_prompt
        assert result.total_tokens > 0
        assert result.budget_utilization > 0
        assert len(result.segments) > 0

    @pytest.mark.asyncio
    async def test_no_compaction_below_threshold(self):
        config = ContextWindowConfig()
        msgs = ({"role": "user", "content": "hello"},)
        result = await assemble_context(
            identity="bot", principles="", constraints="",
            directives="", contact_context="", learned_rules="", episodic_context="",
            duty_context="", goal_context="", task_context="",
            messages=msgs,
            config=config,
        )
        assert result.budget_utilization < 0.85
        assert len(result.compactions_applied) == 0

    @pytest.mark.asyncio
    async def test_compaction_triggered_above_threshold(self):
        # Use a very small window to force compression
        config = ContextWindowConfig(
            model_context_window=200,
            output_reserved_tokens=20,
            safety_buffer_tokens=20,
            history_prune_threshold=0.85,
        )
        msgs = tuple(
            {"role": "user" if i % 2 == 0 else "assistant",
             "content": f"msg {i} " * 50}
            for i in range(10)
        )
        result = await assemble_context(
            identity="bot", principles="p", constraints="c",
            directives="", contact_context="", learned_rules="", episodic_context="",
            duty_context="", goal_context="", task_context="",
            messages=msgs,
            config=config,
        )
        assert len(result.compactions_applied) > 0

    @pytest.mark.asyncio
    async def test_layer3_triggered_above_summarize_threshold(self):
        config = ContextWindowConfig(
            model_context_window=300,
            output_reserved_tokens=20,
            safety_buffer_tokens=20,
            history_prune_threshold=0.50,
            summarize_threshold=0.60,
        )
        msgs = tuple(
            {"role": "user" if i % 2 == 0 else "assistant",
             "content": f"msg {i} " * 100}
            for i in range(10)
        )
        client = MockLLMClient()
        result = await assemble_context(
            identity="bot", principles="p", constraints="c",
            directives="", contact_context="", learned_rules="", episodic_context="",
            duty_context="", goal_context="", task_context="",
            messages=msgs,
            config=config,
            llm_client=client,
        )
        layers = [c.layer for c in result.compactions_applied]
        assert "history_summarize" in layers

    @pytest.mark.asyncio
    async def test_calls_on_pre_compress(self):
        config = ContextWindowConfig(
            model_context_window=220,
            output_reserved_tokens=20,
            safety_buffer_tokens=20,
            history_prune_threshold=0.50,
        )
        orchestrator = MockMemoryOrchestrator()
        msgs = tuple(
            {"role": "user" if i % 2 == 0 else "assistant", "content": f"msg {i} " * 60}
            for i in range(8)
        )
        await assemble_context(
            identity="bot", principles="p", constraints="c",
            directives="", contact_context="", learned_rules="", episodic_context="",
            duty_context="", goal_context="", task_context="",
            messages=msgs,
            config=config,
            memory_orchestrator=orchestrator,
        )
        assert orchestrator.calls == 1

    @pytest.mark.asyncio
    async def test_returns_immutable_result(self):
        config = ContextWindowConfig()
        msgs = ({"role": "user", "content": "hello"},)
        result = await assemble_context(
            identity="bot", principles="", constraints="",
            directives="", contact_context="", learned_rules="", episodic_context="",
            duty_context="", goal_context="", task_context="",
            messages=msgs,
            config=config,
        )
        # AssembledContext is frozen
        with pytest.raises(AttributeError):
            result.total_tokens = 999
