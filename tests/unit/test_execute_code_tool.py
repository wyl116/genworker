# edition: baseline
from __future__ import annotations

import pytest

from src.common.settings import get_settings
from src.tools.builtin.execute_code_tool import create_execute_code_tool
from src.tools.mcp.tool import Tool
from src.tools.mcp.types import MCPCategory, RiskLevel, ToolType
from src.tools.pipeline import ToolCallContext, ToolPipeline
from src.tools.runtime_scope import ExecutionScope, ExecutionScopeProvider
from src.tools.sandbox import ScopedToolExecutor


class _TrustGate:
    trusted = True
    bash_enabled = True
    semantic_search_enabled = True


def _make_scope() -> ExecutionScope:
    return ExecutionScope(
        tenant_id="tenant-1",
        worker_id="worker-1",
        skill_id="skill-1",
        trust_gate=_TrustGate(),
        allowed_tool_names=frozenset({"execute_code"}),
    )


@pytest.mark.asyncio
async def test_execute_code_requires_scope_and_pipeline():
    tool = create_execute_code_tool()

    result = await tool.handler(code="print('hello')")

    assert result.is_error is True
    assert "requires an active execution scope" in result.content


@pytest.mark.asyncio
async def test_execute_code_returns_plain_stdout_and_scrubs_env(monkeypatch):
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "top-secret-value")
    tool = create_execute_code_tool()
    pipeline = ToolPipeline(
        executor=ScopedToolExecutor(allowed_tools={"execute_code": tool}),
    )
    scope = _make_scope()
    provider = ExecutionScopeProvider()

    async with provider.use(scope):
        result = await pipeline.execute(
            ToolCallContext.from_scope(
                scope,
                tool_name="execute_code",
                tool_input={
                    "code": (
                        "import os\n"
                        "print('hello')\n"
                        "print(os.getenv('AWS_SECRET_ACCESS_KEY', 'missing'))\n"
                    )
                },
                risk_level="high",
                tool=tool,
            )
        )

    assert result.is_error is False
    assert result.content.splitlines()[0] == "hello"
    assert "top-secret-value" not in result.content
    assert "missing" in result.content
    assert result.metadata["status"] == "success"


@pytest.mark.asyncio
async def test_execute_code_uses_file_bridge_fallback_for_rpc(monkeypatch):
    async def _deny_unix_server(*args, **kwargs):
        raise PermissionError("unix sockets disabled")

    monkeypatch.setattr(
        "src.tools.builtin.code_rpc_bridge.asyncio.start_unix_server",
        _deny_unix_server,
    )

    async def echo_tool(text: str) -> str:
        return f"echo:{text}"

    execute_code = create_execute_code_tool()
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
    pipeline = ToolPipeline(
        executor=ScopedToolExecutor(
            allowed_tools={
                "execute_code": execute_code,
                "echo_tool": helper_tool,
            }
        ),
    )
    scope = ExecutionScope(
        tenant_id="tenant-1",
        worker_id="worker-1",
        skill_id="skill-1",
        trust_gate=_TrustGate(),
        allowed_tool_names=frozenset({"execute_code", "echo_tool"}),
    )
    provider = ExecutionScopeProvider()

    async with provider.use(scope):
        result = await pipeline.execute(
            ToolCallContext.from_scope(
                scope,
                tool_name="execute_code",
                tool_input={
                    "code": (
                        "from genworker_tools import echo_tool\n"
                        "print(echo_tool(text='ok'))\n"
                    ),
                    "enabled_tools": ["echo_tool"],
                },
                risk_level="high",
                tool=execute_code,
            )
        )

    assert result.is_error is False
    assert result.content.strip() == "echo:ok"
    assert result.metadata["tool_calls_made"] == 1


@pytest.mark.asyncio
async def test_execute_code_timeout_returns_timeout_status():
    tool = create_execute_code_tool()
    pipeline = ToolPipeline(
        executor=ScopedToolExecutor(allowed_tools={"execute_code": tool}),
    )
    scope = _make_scope()
    provider = ExecutionScopeProvider()

    async with provider.use(scope):
        result = await pipeline.execute(
            ToolCallContext.from_scope(
                scope,
                tool_name="execute_code",
                tool_input={
                    "code": (
                        "import time\n"
                        "time.sleep(2)\n"
                        "print('done')\n"
                    ),
                    "timeout_seconds": 1,
                },
                risk_level="high",
                tool=tool,
            )
        )

    assert result.is_error is True
    assert result.metadata["status"] == "timeout"
    assert "timed out" in result.metadata["stderr_tail"].lower()


@pytest.mark.asyncio
async def test_execute_code_enforces_tool_call_limit(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "code_exec_max_tool_calls", 1, raising=False)

    async def echo_tool(text: str) -> str:
        return f"echo:{text}"

    execute_code = create_execute_code_tool()
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
    pipeline = ToolPipeline(
        executor=ScopedToolExecutor(
            allowed_tools={
                "execute_code": execute_code,
                "echo_tool": helper_tool,
            }
        ),
    )
    scope = ExecutionScope(
        tenant_id="tenant-1",
        worker_id="worker-1",
        skill_id="skill-1",
        trust_gate=_TrustGate(),
        allowed_tool_names=frozenset({"execute_code", "echo_tool"}),
    )
    provider = ExecutionScopeProvider()

    async with provider.use(scope):
        result = await pipeline.execute(
            ToolCallContext.from_scope(
                scope,
                tool_name="execute_code",
                tool_input={
                    "code": (
                        "from genworker_tools import echo_tool\n"
                        "print(echo_tool(text='one'), flush=True)\n"
                        "print(echo_tool(text='two'), flush=True)\n"
                    ),
                    "enabled_tools": ["echo_tool"],
                },
                risk_level="high",
                tool=execute_code,
            )
        )

    assert result.is_error is True
    assert result.metadata["status"] == "error"
    assert result.metadata["tool_calls_made"] == 2
    assert "tool_call_limit_exceeded" in result.metadata["stderr_tail"]


@pytest.mark.asyncio
async def test_execute_code_honors_internal_max_tool_calls_override(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "code_exec_max_tool_calls", 5, raising=False)

    async def echo_tool(text: str) -> str:
        return f"echo:{text}"

    execute_code = create_execute_code_tool()
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
    pipeline = ToolPipeline(
        executor=ScopedToolExecutor(
            allowed_tools={
                "execute_code": execute_code,
                "echo_tool": helper_tool,
            }
        ),
    )
    scope = ExecutionScope(
        tenant_id="tenant-1",
        worker_id="worker-1",
        skill_id="skill-1",
        trust_gate=_TrustGate(),
        allowed_tool_names=frozenset({"execute_code", "echo_tool"}),
    )
    provider = ExecutionScopeProvider()

    async with provider.use(scope):
        result = await pipeline.execute(
            ToolCallContext.from_scope(
                scope,
                tool_name="execute_code",
                tool_input={
                    "code": (
                        "from genworker_tools import echo_tool\n"
                        "print(echo_tool(text='one'), flush=True)\n"
                        "print(echo_tool(text='two'), flush=True)\n"
                    ),
                    "enabled_tools": ["echo_tool"],
                    "max_tool_calls": 1,
                },
                risk_level="high",
                tool=execute_code,
            )
        )

    assert result.is_error is True
    assert result.metadata["status"] == "error"
    assert result.metadata["tool_calls_made"] == 2
    assert "tool_call_limit_exceeded" in result.metadata["stderr_tail"]
