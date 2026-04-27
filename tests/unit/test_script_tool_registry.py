# edition: baseline
from __future__ import annotations

import textwrap

import pytest

from src.tools.builtin.script_tool import build_script_tool
from src.tools.builtin.script_tool_registry import ScriptToolRegistry
from src.tools.mcp.server import MCPServer
from src.tools.mcp.tool import Tool
from src.tools.mcp.types import MCPCategory, RiskLevel, ToolType
from src.tools.pipeline import ToolCallContext, ToolPipeline
from src.tools.runtime_scope import ExecutionScope, ExecutionScopeProvider
from src.tools.sandbox import ScopedToolExecutor
from src.worker.tool_scope import LLM_HIDDEN_TAG


class _TrustGate:
    trusted = True
    bash_enabled = True
    semantic_search_enabled = True


def _make_scope(*allowed: str) -> ExecutionScope:
    return ExecutionScope(
        tenant_id="tenant-1",
        worker_id="worker-1",
        skill_id="skill-1",
        trust_gate=_TrustGate(),
        allowed_tool_names=frozenset(allowed),
    )


def test_script_tool_registry_loads_and_refreshes_yaml(tmp_path):
    tool_file = tmp_path / "fetch_metrics.yaml"
    tool_file.write_text(
        textwrap.dedent(
            """
            name: fetch_metrics
            description: Fetch metrics
            script_source: |
              print("v1")
            enabled_rpc_tools: []
            parameters: {}
            visible_to_llm: false
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )

    server = MCPServer(name="test")
    registry = ScriptToolRegistry(directory=tmp_path)
    server.register_refresh_hook(registry.sync_to_server)

    tool = server.get_tool("fetch_metrics")
    assert tool is not None
    assert LLM_HIDDEN_TAG in tool.tags

    tool_file.write_text(
        textwrap.dedent(
            """
            name: fetch_metrics
            description: Fetch metrics
            script_source: |
              print("v2")
            enabled_rpc_tools: []
            parameters: {}
            visible_to_llm: true
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )

    refreshed = server.get_tool("fetch_metrics")
    assert refreshed is not None
    assert LLM_HIDDEN_TAG not in refreshed.tags


@pytest.mark.asyncio
async def test_script_tool_executes_with_script_inputs_and_rpc(monkeypatch):
    async def _deny_unix_server(*args, **kwargs):
        raise PermissionError("unix sockets disabled")

    monkeypatch.setattr(
        "src.tools.builtin.code_rpc_bridge.asyncio.start_unix_server",
        _deny_unix_server,
    )

    async def echo_tool(text: str) -> str:
        return f"echo:{text}"

    helper_tool = Tool(
        name="echo_tool",
        description="Echo input text",
        handler=echo_tool,
        parameters={"text": {"type": "string"}},
        required_params=("text",),
        tool_type=ToolType.READ,
        category=MCPCategory.GLOBAL,
        risk_level=RiskLevel.LOW,
    )
    script_tool = build_script_tool(
        name="fetch_metrics",
        script_source=(
            "from genworker_tools import echo_tool\n"
            "print(echo_tool(text=SCRIPT_INPUTS['text']))\n"
        ),
        enabled_rpc_tools=("echo_tool",),
        parameters={"text": {"type": "string"}},
        visible_to_llm=False,
    )
    pipeline = ToolPipeline(
        executor=ScopedToolExecutor(
            allowed_tools={
                "fetch_metrics": script_tool,
                "echo_tool": helper_tool,
            }
        ),
    )
    scope = _make_scope("fetch_metrics", "echo_tool")
    provider = ExecutionScopeProvider()

    async with provider.use(scope):
        result = await pipeline.execute(
            ToolCallContext.from_scope(
                scope,
                tool_name="fetch_metrics",
                tool_input={"text": "ok"},
                risk_level="high",
                tool=script_tool,
            )
        )

    assert result.is_error is False
    assert result.content.strip() == "echo:ok"
    assert result.metadata["tool_calls_made"] == 1
