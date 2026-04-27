# edition: baseline
"""Integration tests for context window management."""
import pytest

from src.context.integration import (
    build_managed_context,
    context_config_from_worker,
    handle_prompt_too_long,
)
from src.context.models import ContextWindowConfig
from src.engine.prompt_builder import PromptBuilder
from src.engine.protocols import LLMResponse
from src.engine.state import GraphState, WorkerContext


class MockLLMClient:
    async def invoke(self, messages=None, tools=None, tool_choice=None, system_blocks=None, intent=None):
        return LLMResponse(content="Summary of conversation.")


def _make_context(**overrides):
    defaults = {
        "worker_id": "w1",
        "tenant_id": "t1",
        "identity": "You are a data analyst.",
        "principles": "Be accurate and thorough.",
        "constraints": "No PII exposure.",
        "directives": "Follow all guidelines.",
        "learned_rules": "Rule 1: always validate data.",
        "task_context": "Analyze quarterly report.",
        "tool_names": ("sql_executor",),
    }
    defaults.update(overrides)
    return WorkerContext(**defaults)


class TestContextConfigFromWorker:
    def test_creates_config_with_defaults(self):
        ctx = _make_context()
        config = context_config_from_worker(ctx)
        assert config.model_context_window == 128_000
        assert config.effective_window > 0

    def test_custom_model_window(self):
        ctx = _make_context()
        config = context_config_from_worker(ctx, model_context_window=200_000)
        assert config.model_context_window == 200_000


class TestBuildManagedContext:
    @pytest.mark.asyncio
    async def test_basic_managed_context(self):
        ctx = _make_context()
        config = ContextWindowConfig()
        msgs = (
            {"role": "user", "content": "Analyze the data."},
            {"role": "assistant", "content": "Looking at the data..."},
        )
        result = await build_managed_context(ctx, msgs, config)
        assert result.system_prompt != ""
        assert "data analyst" in result.system_prompt
        assert result.total_tokens > 0
        assert len(result.segments) > 0

    @pytest.mark.asyncio
    async def test_includes_phase7_context(self):
        ctx = _make_context()
        config = ContextWindowConfig()
        msgs = ({"role": "user", "content": "task"},)
        result = await build_managed_context(
            ctx, msgs, config,
            episodic_context="Past: found data anomaly",
            duty_context="Daily quality check",
            goal_context="Improve data quality by 20%",
        )
        assert "data anomaly" in result.system_prompt
        assert "quality check" in result.system_prompt
        assert "Improve data quality" in result.system_prompt
        assert isinstance(result.system_prompt, str)

    @pytest.mark.asyncio
    async def test_compaction_with_large_history(self):
        ctx = _make_context()
        config = ContextWindowConfig(
            model_context_window=500,
            output_reserved_tokens=50,
            safety_buffer_tokens=50,
        )
        msgs = tuple(
            {"role": "user" if i % 2 == 0 else "assistant",
             "content": f"message {i} " * 50}
            for i in range(10)
        )
        result = await build_managed_context(ctx, msgs, config)
        assert len(result.compactions_applied) > 0


class TestHandlePromptTooLong:
    @pytest.mark.asyncio
    async def test_returns_new_graph_state(self):
        ctx = _make_context()
        msgs = tuple(
            {"role": "user" if i % 2 == 0 else "assistant",
             "content": f"msg {i} " * 200}
            for i in range(10)
        )
        state = GraphState(messages=msgs, worker_context=ctx)
        config = ContextWindowConfig(
            model_context_window=500,
            output_reserved_tokens=50,
            safety_buffer_tokens=50,
        )
        client = MockLLMClient()
        new_state = await handle_prompt_too_long(state, client, config)
        assert new_state is not state
        assert new_state.worker_context == ctx
        # Messages should be compressed
        from src.context.token_counter import count_messages_tokens
        assert count_messages_tokens(new_state.messages) <= count_messages_tokens(msgs)

    @pytest.mark.asyncio
    async def test_preserves_worker_context(self):
        ctx = _make_context()
        state = GraphState(
            messages=({"role": "user", "content": "hello"},),
            worker_context=ctx,
            thread_id="thread-123",
        )
        config = ContextWindowConfig()
        client = MockLLMClient()
        new_state = await handle_prompt_too_long(state, client, config)
        assert new_state.worker_context == ctx
        assert new_state.thread_id == "thread-123"


class TestPromptBuilderManaged:
    def test_build_autonomous_managed(self):
        ctx = _make_context()
        config = ContextWindowConfig()
        prompt = PromptBuilder.build_autonomous_managed(ctx, config)
        assert "data analyst" in prompt
        assert "accurate" in prompt

    def test_includes_phase7_context(self):
        ctx = _make_context()
        config = ContextWindowConfig()
        prompt = PromptBuilder.build_autonomous_managed(
            ctx, config,
            episodic_context="Previous episode data",
            duty_context="Monitoring duty",
            goal_context="Achieve 99% uptime",
        )
        assert "Previous episode data" in prompt
        assert "Monitoring duty" in prompt
        assert "99% uptime" in prompt

    def test_empty_context_still_works(self):
        ctx = WorkerContext()
        config = ContextWindowConfig()
        prompt = PromptBuilder.build_autonomous_managed(ctx, config)
        # Should return empty or minimal prompt
        assert isinstance(prompt, str)


class TestEngineDispatcherContextConfig:
    def test_dispatch_accepts_context_config(self):
        """Verify EngineDispatcher.dispatch() accepts context_config parameter."""
        from src.engine.router.engine_dispatcher import EngineDispatcher
        import inspect

        sig = inspect.signature(EngineDispatcher.dispatch)
        params = list(sig.parameters.keys())
        assert "context_config" in params

    def test_context_config_is_optional(self):
        """Verify context_config defaults to None."""
        from src.engine.router.engine_dispatcher import EngineDispatcher
        import inspect

        sig = inspect.signature(EngineDispatcher.dispatch)
        param = sig.parameters["context_config"]
        assert param.default is None


class TestImmutability:
    @pytest.mark.asyncio
    async def test_assembled_context_is_frozen(self):
        ctx = _make_context()
        config = ContextWindowConfig()
        msgs = ({"role": "user", "content": "hi"},)
        result = await build_managed_context(ctx, msgs, config)
        with pytest.raises(AttributeError):
            result.total_tokens = 0

    def test_context_window_config_is_frozen(self):
        config = ContextWindowConfig()
        with pytest.raises(AttributeError):
            config.model_context_window = 999

    def test_context_segment_is_frozen(self):
        from src.context.models import ContextSegment
        seg = ContextSegment("test", "content", 10, 0)
        with pytest.raises(AttributeError):
            seg.content = "new"


class TestEndToEndCompactionThresholds:
    """Verify compaction threshold behavior end-to-end."""

    @pytest.mark.asyncio
    async def test_below_085_no_compaction(self):
        config = ContextWindowConfig(model_context_window=200_000)
        msgs = ({"role": "user", "content": "short"},)
        result = await build_managed_context(
            _make_context(), msgs, config,
        )
        assert result.budget_utilization < 0.85
        assert len(result.compactions_applied) == 0

    @pytest.mark.asyncio
    async def test_above_085_triggers_l1_l2(self):
        config = ContextWindowConfig(
            model_context_window=200,
            output_reserved_tokens=10,
            safety_buffer_tokens=10,
            history_prune_threshold=0.85,
            summarize_threshold=0.99,  # disable L3
        )
        msgs = tuple(
            {"role": "user" if i % 2 == 0 else "assistant",
             "content": f"m{i} " * 40}
            for i in range(8)
        )
        result = await build_managed_context(
            _make_context(identity="x", principles="y", constraints="z"),
            msgs, config,
        )
        layers = [c.layer for c in result.compactions_applied]
        # Should have L1 and L2, but not L3
        assert "tool_trim" in layers
        assert "history_prune" in layers
        assert "history_summarize" not in layers

    @pytest.mark.asyncio
    async def test_above_092_triggers_l1_l2_l3(self):
        config = ContextWindowConfig(
            model_context_window=200,
            output_reserved_tokens=10,
            safety_buffer_tokens=10,
            history_prune_threshold=0.50,
            summarize_threshold=0.60,
        )
        msgs = tuple(
            {"role": "user" if i % 2 == 0 else "assistant",
             "content": f"m{i} " * 80}
            for i in range(10)
        )
        client = MockLLMClient()
        result = await build_managed_context(
            _make_context(identity="x", principles="y", constraints="z"),
            msgs, config, llm_client=client,
        )
        layers = [c.layer for c in result.compactions_applied]
        assert "history_summarize" in layers
