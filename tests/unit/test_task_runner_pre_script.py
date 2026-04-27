# edition: baseline
from __future__ import annotations

import pytest

from src.engine.state import WorkerContext
from src.skills.models import Skill, SkillStrategy, StrategyMode
from src.streaming.events import RunFinishedEvent, TextMessageEvent
from src.tools.mcp.tool import Tool
from src.tools.mcp.types import MCPCategory, RiskLevel, ToolType
from src.tools.pipeline import ToolPipeline
from src.tools.runtime_scope import ExecutionScope, ExecutionScopeProvider
from src.tools.sandbox import ScopedToolExecutor
from src.worker.scripts.models import InlineScript
from src.worker.task import create_task_manifest
from src.worker.task_runner import TaskRunner


class _TrustGate:
    trusted = True
    bash_enabled = True
    semantic_search_enabled = True


class _Store:
    def save(self, manifest):
        self.last_manifest = manifest


class _CapturingDispatcher:
    def __init__(self) -> None:
        self.tasks: list[str] = []

    async def dispatch(self, **kwargs):
        self.tasks.append(kwargs["task"])
        yield TextMessageEvent(run_id="run-1", content="done")
        yield RunFinishedEvent(run_id="run-1", success=True)


def _make_skill() -> Skill:
    return Skill(
        skill_id="skill-1",
        name="Skill",
        scope="system",
        strategy=SkillStrategy(mode=StrategyMode.AUTONOMOUS),
        keywords=(),
    )


def _make_scope() -> ExecutionScope:
    return ExecutionScope(
        tenant_id="tenant-1",
        worker_id="worker-1",
        skill_id="skill-1",
        trust_gate=_TrustGate(),
        allowed_tool_names=frozenset({"execute_code"}),
    )


def _make_scope_with_echo() -> ExecutionScope:
    return ExecutionScope(
        tenant_id="tenant-1",
        worker_id="worker-1",
        skill_id="skill-1",
        trust_gate=_TrustGate(),
        allowed_tool_names=frozenset({"execute_code", "echo_tool"}),
    )


@pytest.mark.asyncio
async def test_task_runner_injects_pre_script_output_into_task():
    from src.tools.builtin.execute_code_tool import create_execute_code_tool

    dispatcher = _CapturingDispatcher()
    runner = TaskRunner(
        engine_dispatcher=dispatcher,
        task_store=_Store(),
        tool_pipeline=ToolPipeline(
            executor=ScopedToolExecutor(
                allowed_tools={"execute_code": create_execute_code_tool()}
            ),
        ),
    )
    manifest = create_task_manifest(
        worker_id="worker-1",
        tenant_id="tenant-1",
        task_description="Analyze the data",
        pre_script=InlineScript(source="print('ready')"),
    )
    provider = ExecutionScopeProvider()

    async with provider.use(_make_scope()):
        async for _ in runner.execute(
            skill=_make_skill(),
            worker_context=WorkerContext(worker_id="worker-1", tenant_id="tenant-1"),
            task="Analyze the data",
            manifest=manifest,
        ):
            pass

    assert dispatcher.tasks
    assert "Pre-execution Script Output" in dispatcher.tasks[0]
    assert "ready" in dispatcher.tasks[0]
    assert dispatcher.tasks[0].endswith("Analyze the data")


@pytest.mark.asyncio
async def test_task_runner_honors_inline_pre_script_tool_call_limit():
    from src.tools.builtin.execute_code_tool import create_execute_code_tool

    async def echo_tool(text: str) -> str:
        return f"echo:{text}"

    dispatcher = _CapturingDispatcher()
    runner = TaskRunner(
        engine_dispatcher=dispatcher,
        task_store=_Store(),
        tool_pipeline=ToolPipeline(
            executor=ScopedToolExecutor(
                allowed_tools={
                    "execute_code": create_execute_code_tool(),
                    "echo_tool": Tool(
                        name="echo_tool",
                        description="Echo input text",
                        handler=echo_tool,
                        parameters={"text": {"type": "string"}},
                        required_params=("text",),
                        tool_type=ToolType.READ,
                        category=MCPCategory.GLOBAL,
                        risk_level=RiskLevel.LOW,
                    ),
                }
            ),
        ),
    )
    manifest = create_task_manifest(
        worker_id="worker-1",
        tenant_id="tenant-1",
        task_description="Analyze the data",
        pre_script=InlineScript(
            source=(
                "from genworker_tools import echo_tool\n"
                "print(echo_tool(text='one'), flush=True)\n"
                "print(echo_tool(text='two'), flush=True)\n"
            ),
            enabled_tools=("echo_tool",),
            max_tool_calls=1,
        ),
    )
    provider = ExecutionScopeProvider()

    async with provider.use(_make_scope_with_echo()):
        async for _ in runner.execute(
            skill=_make_skill(),
            worker_context=WorkerContext(worker_id="worker-1", tenant_id="tenant-1"),
            task="Analyze the data",
            manifest=manifest,
        ):
            pass

    assert dispatcher.tasks
    assert "Pre-execution Script Output" in dispatcher.tasks[0]
    assert "tool_call_limit_exceeded" in dispatcher.tasks[0]
