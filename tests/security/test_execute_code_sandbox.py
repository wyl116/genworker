# edition: baseline
from __future__ import annotations

import pytest

from src.tools.builtin.execute_code_tool import create_execute_code_tool
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


async def _run_execute_code(
    *,
    scope: ExecutionScope,
    tool_input: dict[str, object],
    allowed_tools: dict[str, Tool],
):
    provider = ExecutionScopeProvider()
    pipeline = ToolPipeline(executor=ScopedToolExecutor(allowed_tools=allowed_tools))
    execute_code = allowed_tools["execute_code"]
    async with provider.use(scope):
        return await pipeline.execute(
            ToolCallContext.from_scope(
                scope,
                tool_name="execute_code",
                tool_input=tool_input,
                risk_level="high",
                tool=execute_code,
            )
        )


@pytest.mark.asyncio
async def test_execute_code_scrubs_secrets_from_env_and_output(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-top-secret")

    result = await _run_execute_code(
        scope=_make_scope("execute_code"),
        tool_input={
            "code": (
                "import os\n"
                "print(os.getenv('OPENAI_API_KEY', 'missing'))\n"
                "print('Bearer super-secret-token')\n"
                "print('password=abc123')\n"
            )
        },
        allowed_tools={"execute_code": create_execute_code_tool()},
    )

    assert result.is_error is False
    assert "sk-top-secret" not in result.content
    assert "missing" in result.content
    assert "Bearer [REDACTED]" in result.content
    assert "password=[REDACTED]" in result.content


@pytest.mark.asyncio
async def test_execute_code_blocks_hidden_tools_inside_script_runtime():
    calls: list[str] = []

    async def hidden_tool() -> str:
        calls.append("hidden_tool")
        return "should-not-run"

    execute_code = create_execute_code_tool()
    hidden = Tool(
        name="hidden_tool",
        description="Hidden helper",
        handler=hidden_tool,
        parameters={},
        required_params=(),
        tool_type=ToolType.EXECUTE,
        category=MCPCategory.SPECIALIZED,
        risk_level=RiskLevel.HIGH,
        tags=frozenset({LLM_HIDDEN_TAG, "script"}),
    )
    result = await _run_execute_code(
        scope=_make_scope("execute_code", "hidden_tool"),
        tool_input={
            "code": (
                "from genworker_tools import hidden_tool\n"
                "print(hidden_tool())\n"
            ),
            "enabled_tools": ["hidden_tool"],
        },
        allowed_tools={
            "execute_code": execute_code,
            "hidden_tool": hidden,
        },
    )

    assert result.is_error is True
    assert calls == []
    assert result.metadata["tool_calls_made"] == 0
    assert "cannot import name 'hidden_tool'" in result.metadata["stderr_tail"]


@pytest.mark.asyncio
async def test_execute_code_enforces_timeout_for_long_running_scripts():
    result = await _run_execute_code(
        scope=_make_scope("execute_code"),
        tool_input={
            "code": (
                "import time\n"
                "time.sleep(2)\n"
                "print('done')\n"
            ),
            "timeout_seconds": 1,
        },
        allowed_tools={"execute_code": create_execute_code_tool()},
    )

    assert result.is_error is True
    assert result.metadata["status"] == "timeout"
    assert "timed out" in result.metadata["stderr_tail"].lower()


@pytest.mark.asyncio
async def test_execute_code_uses_short_unix_socket_path(monkeypatch):
    observed_paths: list[str] = []

    async def _capture_short_path(*args, **kwargs):
        path = kwargs["path"]
        observed_paths.append(path)
        assert len(path.encode("utf-8")) <= 104
        raise PermissionError("unix sockets disabled")

    monkeypatch.setattr(
        "src.tools.builtin.code_rpc_bridge.asyncio.start_unix_server",
        _capture_short_path,
    )

    async def echo_tool(text: str) -> str:
        return f"echo:{text}"

    execute_code = create_execute_code_tool()
    echo = Tool(
        name="echo_tool",
        description="Echo helper",
        handler=echo_tool,
        parameters={"text": {"type": "string"}},
        required_params=("text",),
        tool_type=ToolType.READ,
        category=MCPCategory.GLOBAL,
        risk_level=RiskLevel.LOW,
    )
    result = await _run_execute_code(
        scope=_make_scope("execute_code", "echo_tool"),
        tool_input={
            "code": (
                "from genworker_tools import echo_tool\n"
                "print(echo_tool(text='ok'))\n"
            ),
            "enabled_tools": ["echo_tool"],
        },
        allowed_tools={
            "execute_code": execute_code,
            "echo_tool": echo,
        },
    )

    assert observed_paths
    assert result.is_error is False
    assert result.content.strip() == "echo:ok"
