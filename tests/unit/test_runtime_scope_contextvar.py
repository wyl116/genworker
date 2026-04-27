# edition: baseline
from __future__ import annotations

import pytest

from src.tools.pipeline import ToolCallContext, ToolPipeline
from src.tools.runtime_scope import (
    ExecutionScope,
    ExecutionScopeProvider,
    current_execution_scope,
    current_tool_pipeline,
    require_execution_scope,
)
from src.tools.mcp.tool import Tool
from src.tools.mcp.types import MCPCategory, RiskLevel, ToolType
from src.tools.sandbox import ScopedToolExecutor


class _TrustGate:
    trusted = True
    bash_enabled = True
    semantic_search_enabled = True


def _make_scope(worker_id: str) -> ExecutionScope:
    return ExecutionScope(
        tenant_id="tenant-1",
        worker_id=worker_id,
        skill_id="skill-1",
        trust_gate=_TrustGate(),
        allowed_tool_names=frozenset({"echo"}),
    )


@pytest.mark.asyncio
async def test_execution_scope_provider_restores_outer_scope_when_nested():
    provider = ExecutionScopeProvider()
    outer = _make_scope("worker-outer")
    inner = _make_scope("worker-inner")

    assert current_execution_scope() is None

    async with provider.use(outer):
        assert current_execution_scope() == outer
        assert require_execution_scope() == outer

        async with provider.use(inner):
            assert current_execution_scope() == inner
            assert require_execution_scope() == inner

        assert current_execution_scope() == outer
        assert require_execution_scope() == outer

    assert current_execution_scope() is None


@pytest.mark.asyncio
async def test_current_tool_pipeline_is_only_bound_during_pipeline_execution():
    seen_pipelines: list[ToolPipeline | None] = []
    scope = _make_scope("worker-1")
    provider = ExecutionScopeProvider()

    async def echo(value: str) -> str:
        seen_pipelines.append(current_tool_pipeline())
        return value

    tool = Tool(
        name="echo",
        description="Echo input",
        handler=echo,
        parameters={"value": {"type": "string"}},
        required_params=("value",),
        tool_type=ToolType.READ,
        category=MCPCategory.GLOBAL,
        risk_level=RiskLevel.LOW,
    )
    pipeline = ToolPipeline(
        executor=ScopedToolExecutor(allowed_tools={"echo": tool}),
    )

    assert current_tool_pipeline() is None

    async with provider.use(scope):
        result = await pipeline.execute(
            ToolCallContext.from_scope(
                scope,
                tool_name="echo",
                tool_input={"value": "ok"},
                risk_level="low",
                tool=tool,
            )
        )

    assert result.is_error is False
    assert result.content == "ok"
    assert seen_pipelines == [pipeline]
    assert current_tool_pipeline() is None
