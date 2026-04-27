# edition: baseline
import json
from types import SimpleNamespace

import pytest

from src.runtime.bootstrap_builders import build_tool_executor
from src.tools.builtin.bash_sandbox import ProcessSandbox, SandboxConfig
from src.tools.builtin.bash_security import BashSecurityHook
from src.tools.hooks import HookAction
from src.tools.mcp.tool import Tool
from src.tools.mcp.types import MCPCategory, RiskLevel, ToolType
from src.tools.pipeline import ToolCallContext, ToolPipeline
from src.tools.sandbox import ScopedToolExecutor


class FakeMCPServer:
    def __init__(self, tool: Tool) -> None:
        self._tool = tool

    def get_tool(self, name: str):
        return self._tool if name == self._tool.name else None

    def get_all_tools(self):
        return (self._tool,)


class FakeBootstrapContext:
    def __init__(self) -> None:
        self._state = {"tenant_id": "demo", "worker_id": "worker-1"}

    def get_state(self, key: str, default=None):
        return self._state.get(key, default)


@pytest.mark.asyncio
async def test_bash_security_hook_denies_blocked_command() -> None:
    hook = BashSecurityHook()

    result = await hook.pre_execute("bash_execute", {"command": "rm -rf /"})

    assert result.action == HookAction.DENY


@pytest.mark.asyncio
async def test_process_sandbox_subprocess_executes_command() -> None:
    sandbox = ProcessSandbox(SandboxConfig(timeout_seconds=5))

    result = await sandbox.execute("echo hello")

    assert result.exit_code == 0
    assert "hello" in result.stdout


@pytest.mark.asyncio
async def test_process_sandbox_passes_custom_env() -> None:
    sandbox = ProcessSandbox(SandboxConfig(timeout_seconds=5))

    result = await sandbox.execute(
        "python3 -c 'import os; print(os.getenv(\"LW_TEST_ENV\", \"\"))'",
        env={"LW_TEST_ENV": "sandboxed"},
    )

    assert result.exit_code == 0
    assert "sandboxed" in result.stdout


@pytest.mark.asyncio
async def test_build_tool_executor_routes_through_pipeline() -> None:
    async def handler(**kwargs):
        return "should not run"

    tool = Tool(
        name="bash_execute",
        description="bash",
        handler=handler,
        tool_type=ToolType.EXECUTE,
        category=MCPCategory.GLOBAL,
        risk_level=RiskLevel.MEDIUM,
    )
    executor = build_tool_executor(FakeMCPServer(tool), FakeBootstrapContext())

    result = await executor.execute("bash_execute", {"command": "rm -rf /"})

    assert result.is_error is True
    assert "not in the whitelist" in result.content
