# edition: baseline
"""
Tests for ToolPipeline - middleware chain, hooks, and execution flow.
"""
import asyncio
from typing import Any, Callable

import pytest

from src.tools.formatters import ToolResult
from src.tools.hooks import HookAction, HookResult, ToolHook
from src.tools.middlewares.sanitize import SanitizeMiddleware
from src.tools.mcp.tool import Tool
from src.tools.mcp.types import MCPCategory, RiskLevel, ToolType
from src.tools.middlewares.permission import PermissionMiddleware
from src.tools.middlewares.schema_validation import SchemaValidationMiddleware
from src.tools.middlewares.timeout import TimeoutMiddleware
from src.tools.pipeline import ToolCallContext, ToolPipeline
from src.tools.sandbox import ScopedToolExecutor


# --- Helpers ---

def _make_tool(name: str, handler=None, **kwargs) -> Tool:
    async def default_handler(**kw):
        return f"result from {name}"
    return Tool(
        name=name,
        description=f"Test {name}",
        handler=handler or default_handler,
        tool_type=ToolType.CUSTOM,
        category=MCPCategory.GLOBAL,
        risk_level=kwargs.pop("risk_level", RiskLevel.LOW),
        **kwargs,
    )


def _make_ctx(tool_name: str = "test_tool", risk_level: str = "low", **kwargs) -> ToolCallContext:
    defaults = dict(
        worker_id="w1",
        tenant_id="t1",
        skill_id="s1",
        step_name=None,
        tool_name=tool_name,
        tool_input={},
        risk_level=risk_level,
    )
    defaults.update(kwargs)
    return ToolCallContext(**defaults)


# --- Recording middleware for order tracking ---

class RecordingMiddleware:
    """Middleware that records its position in the chain."""
    def __init__(self, label: str, record: list):
        self.label = label
        self.record = record

    async def process(self, ctx: ToolCallContext, next_fn: Callable[[], Any]) -> ToolResult:
        self.record.append(f"pre_{self.label}")
        result = await next_fn()
        self.record.append(f"post_{self.label}")
        return result


# --- Recording hook ---

class RecordingHook:
    """Hook that records pre/post execution."""
    def __init__(self, label: str, record: list, action: HookAction = HookAction.ALLOW):
        self.label = label
        self.record = record
        self.action = action

    async def pre_execute(self, tool_name: str, tool_input: dict) -> HookResult:
        self.record.append(f"hook_pre_{self.label}")
        return HookResult(action=self.action, message=f"denied by {self.label}")

    async def post_execute(self, tool_name: str, tool_input: dict, result: ToolResult) -> None:
        self.record.append(f"hook_post_{self.label}")


# --- Denying hook ---

class DenyingHook:
    """Hook that always denies."""
    async def pre_execute(self, tool_name: str, tool_input: dict) -> HookResult:
        return HookResult(action=HookAction.DENY, message="blocked by security")

    async def post_execute(self, tool_name: str, tool_input: dict, result: ToolResult) -> None:
        pass


# --- Tests ---

class TestToolPipeline:
    """Tests for ToolPipeline three-layer execution."""

    @pytest.fixture
    def tool(self):
        return _make_tool("test_tool")

    @pytest.fixture
    def executor(self, tool):
        return ScopedToolExecutor(allowed_tools={"test_tool": tool})

    @pytest.mark.asyncio
    async def test_middleware_execution_order(self, executor):
        """Middlewares should execute in order: pre_A -> pre_B -> executor -> post_B -> post_A."""
        record = []
        mw_a = RecordingMiddleware("A", record)
        mw_b = RecordingMiddleware("B", record)

        pipeline = ToolPipeline(
            executor=executor,
            middlewares=[mw_a, mw_b],
        )

        ctx = _make_ctx()
        result = await pipeline.execute(ctx)

        assert not result.is_error
        assert record == ["pre_A", "pre_B", "post_B", "post_A"]

    @pytest.mark.asyncio
    async def test_hooks_run_before_and_after_middlewares(self, executor):
        """Hooks should wrap the entire middleware chain."""
        record = []
        hook = RecordingHook("H", record)
        mw = RecordingMiddleware("M", record)

        pipeline = ToolPipeline(
            executor=executor,
            middlewares=[mw],
            hooks=[hook],
        )

        ctx = _make_ctx()
        await pipeline.execute(ctx)

        assert record == ["hook_pre_H", "pre_M", "post_M", "hook_post_H"]

    @pytest.mark.asyncio
    async def test_hook_deny_short_circuits(self, executor):
        """Deny hook should prevent middleware chain and executor from running."""
        record = []
        deny_hook = DenyingHook()
        mw = RecordingMiddleware("M", record)

        pipeline = ToolPipeline(
            executor=executor,
            middlewares=[mw],
            hooks=[deny_hook],
        )

        ctx = _make_ctx()
        result = await pipeline.execute(ctx)

        assert result.is_error
        assert "blocked by security" in result.content
        assert record == []  # Middleware never ran

    @pytest.mark.asyncio
    async def test_permission_denial_returns_data_object(self):
        """Permission middleware should return ToolResult, not raise exception."""
        tool = _make_tool("risky_tool", risk_level=RiskLevel.CRITICAL)
        executor = ScopedToolExecutor(allowed_tools={"risky_tool": tool})

        # Worker only allowed up to LOW risk
        perm_mw = PermissionMiddleware(max_risk_level="low")

        pipeline = ToolPipeline(
            executor=executor,
            middlewares=[perm_mw],
        )

        ctx = _make_ctx(tool_name="risky_tool", risk_level="critical")
        result = await pipeline.execute(ctx)

        assert isinstance(result, ToolResult)
        assert result.is_error
        assert "risk_level" in result.content

    @pytest.mark.asyncio
    async def test_schema_validation_rejects_invalid_params(self):
        """Schema validation should reject missing required params."""
        tool = _make_tool(
            "validated_tool",
            parameters={"query": {"type": "string", "description": "SQL query"}},
            required_params=("query",),
        )
        executor = ScopedToolExecutor(allowed_tools={"validated_tool": tool})

        schema_mw = SchemaValidationMiddleware(
            tool_registry={"validated_tool": tool}
        )

        pipeline = ToolPipeline(
            executor=executor,
            middlewares=[schema_mw],
        )

        # Missing required 'query' param
        ctx = _make_ctx(tool_name="validated_tool", tool_input={})
        result = await pipeline.execute(ctx)

        assert result.is_error
        assert "Missing required parameter" in result.content
        assert "query" in result.content

    @pytest.mark.asyncio
    async def test_schema_validation_passes_valid_params(self):
        """Schema validation should pass valid params through."""
        tool = _make_tool(
            "validated_tool",
            parameters={"query": {"type": "string"}},
            required_params=("query",),
        )
        executor = ScopedToolExecutor(allowed_tools={"validated_tool": tool})
        schema_mw = SchemaValidationMiddleware(
            tool_registry={"validated_tool": tool}
        )

        pipeline = ToolPipeline(
            executor=executor,
            middlewares=[schema_mw],
        )

        ctx = _make_ctx(
            tool_name="validated_tool",
            tool_input={"query": "SELECT 1"},
        )
        result = await pipeline.execute(ctx)
        assert not result.is_error

    @pytest.mark.asyncio
    async def test_timeout_returns_error(self):
        """Timeout middleware should return error on timeout."""
        async def slow_handler(**kw):
            await asyncio.sleep(10)
            return "should not reach"

        tool = _make_tool("slow_tool", handler=slow_handler)
        executor = ScopedToolExecutor(allowed_tools={"slow_tool": tool})
        timeout_mw = TimeoutMiddleware(default_timeout=0.1)

        pipeline = ToolPipeline(
            executor=executor,
            middlewares=[timeout_mw],
        )

        ctx = _make_ctx(tool_name="slow_tool")
        result = await pipeline.execute(ctx)

        assert result.is_error
        assert "timed out" in result.content

    @pytest.mark.asyncio
    async def test_pipeline_without_middlewares(self, executor):
        """Pipeline should work with no middlewares (direct to executor)."""
        pipeline = ToolPipeline(executor=executor)
        ctx = _make_ctx()
        result = await pipeline.execute(ctx)
        assert not result.is_error
        assert "result from test_tool" in result.content

    @pytest.mark.asyncio
    async def test_sanitize_middleware_redacts_sensitive_output(self):
        """Sanitize middleware should redact obvious credentials in tool output."""

        async def sensitive_handler(**kw):
            return "token=abc123 password=secret"

        tool = _make_tool("sensitive_tool", handler=sensitive_handler)
        executor = ScopedToolExecutor(allowed_tools={"sensitive_tool": tool})
        pipeline = ToolPipeline(
            executor=executor,
            middlewares=[SanitizeMiddleware()],
        )

        result = await pipeline.execute(_make_ctx(tool_name="sensitive_tool"))

        assert not result.is_error
        assert "[REDACTED]" in result.content
        assert "abc123" not in result.content
        assert "secret" not in result.content

    @pytest.mark.asyncio
    async def test_multiple_hooks_all_run(self, executor):
        """Multiple hooks should all run in order."""
        record = []
        h1 = RecordingHook("H1", record)
        h2 = RecordingHook("H2", record)

        pipeline = ToolPipeline(executor=executor, hooks=[h1, h2])
        ctx = _make_ctx()
        await pipeline.execute(ctx)

        assert record == [
            "hook_pre_H1", "hook_pre_H2",
            "hook_post_H1", "hook_post_H2",
        ]
