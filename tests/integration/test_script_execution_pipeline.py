# edition: baseline
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from src.common.tenant import Tenant, TenantLoader, TrustLevel
from src.skills.models import Skill, SkillKeyword, SkillScope, SkillStrategy, StrategyMode
from src.skills.registry import SkillRegistry
from src.streaming.events import RunFinishedEvent, TextMessageEvent
from src.tools.builtin.execute_code_tool import create_execute_code_tool
from src.tools.builtin.script_tool import build_script_tool
from src.tools.formatters import ToolResult
from src.tools.mcp.server import MCPServer
from src.tools.mcp.tool import Tool
from src.tools.mcp.types import MCPCategory, RiskLevel, ToolType
from src.tools.pipeline import ToolCallContext, ToolPipeline
from src.tools.runtime_scope import ExecutionScope, ExecutionScopeProvider
from src.tools.sandbox import ScopedToolExecutor
from src.worker.duty.duty_executor import DutyExecutor
from src.worker.duty.models import Duty, DutyTrigger, ExecutionPolicy
from src.worker.models import Worker, WorkerIdentity
from src.worker.registry import WorkerEntry, build_worker_registry
from src.worker.router import WorkerRouter
from src.worker.scripts.models import InlineScript, ScriptRef
from src.worker.task import TaskStore, create_task_manifest
from src.worker.task_runner import TaskRunner


@dataclass
class _AuditCollector:
    entries: list[dict] | None = None

    def __post_init__(self) -> None:
        if self.entries is None:
            self.entries = []

    def log(
        self,
        *,
        tenant_id: str,
        worker_id: str,
        tool_name: str,
        policy_decision: str,
        enforcement_result: str,
        error_message: str = "",
        execution_time_ms: int = 0,
    ) -> None:
        self.entries.append(
            {
                "tenant_id": tenant_id,
                "worker_id": worker_id,
                "tool_name": tool_name,
                "policy_decision": policy_decision,
                "enforcement_result": enforcement_result,
                "error_message": error_message,
                "execution_time_ms": execution_time_ms,
            }
        )


class _TrustGate:
    trusted = True
    bash_enabled = True
    semantic_search_enabled = True


class _CapturingDispatcher:
    def __init__(self) -> None:
        self.tasks: list[str] = []

    async def dispatch(self, **kwargs):
        self.tasks.append(kwargs["task"])
        yield TextMessageEvent(run_id="run-1", content="ok")
        yield RunFinishedEvent(run_id="run-1", success=True)


def _make_scope(*allowed_tools: str) -> ExecutionScope:
    return ExecutionScope(
        tenant_id="tenant-1",
        worker_id="worker-1",
        skill_id="skill-1",
        trust_gate=_TrustGate(),
        allowed_tool_names=frozenset(allowed_tools),
    )


def _make_skill() -> Skill:
    return Skill(
        skill_id="skill-1",
        name="Skill",
        scope=SkillScope.SYSTEM,
        strategy=SkillStrategy(mode=StrategyMode.AUTONOMOUS),
        keywords=(SkillKeyword(keyword="duty", weight=1.0),),
        default_skill=True,
    )


def _make_worker() -> Worker:
    return Worker(
        identity=WorkerIdentity(name="Worker", worker_id="worker-1"),
        default_skill="skill-1",
    )


@pytest.mark.asyncio
async def test_execute_code_rpc_uses_standard_tool_pipeline(monkeypatch):
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
    audit = _AuditCollector()
    pipeline = ToolPipeline(
        executor=ScopedToolExecutor(
            allowed_tools={
                "execute_code": execute_code,
                "echo_tool": helper_tool,
            }
        ),
        audit_logger=audit,
    )
    scope = _make_scope("execute_code", "echo_tool")
    provider = ExecutionScopeProvider()

    async with provider.use(scope):
        result = await pipeline.execute(
            ToolCallContext.from_scope(
                scope,
                tool_name="execute_code",
                tool_input={
                    "code": (
                        "from genworker_tools import echo_tool\n"
                        "print(echo_tool(text='hello'))\n"
                    ),
                    "enabled_tools": ["echo_tool"],
                },
                risk_level="high",
                tool=execute_code,
            )
        )

    assert result.is_error is False
    assert result.content.strip() == "echo:hello"
    assert result.metadata["tool_calls_made"] == 1
    assert [entry["tool_name"] for entry in audit.entries].count("execute_code") == 1
    assert [entry["tool_name"] for entry in audit.entries].count("echo_tool") == 1
    assert {entry["worker_id"] for entry in audit.entries} == {"worker-1"}
    assert {entry["tenant_id"] for entry in audit.entries} == {"tenant-1"}


@pytest.mark.asyncio
async def test_duty_pre_script_flows_through_router_and_task_runner(
    tmp_path: Path,
    monkeypatch,
):
    async def _deny_unix_server(*args, **kwargs):
        raise PermissionError("unix sockets disabled")

    monkeypatch.setattr(
        "src.tools.builtin.code_rpc_bridge.asyncio.start_unix_server",
        _deny_unix_server,
    )

    execute_code = create_execute_code_tool()
    audit = _AuditCollector()
    pipeline = ToolPipeline(
        executor=ScopedToolExecutor(allowed_tools={"execute_code": execute_code}),
        audit_logger=audit,
    )
    dispatcher = _CapturingDispatcher()
    task_store = TaskStore(workspace_root=tmp_path)
    runner = TaskRunner(
        engine_dispatcher=dispatcher,
        task_store=task_store,
        tool_pipeline=pipeline,
    )

    tenant_loader = TenantLoader(tmp_path)
    tenant_loader._cache["tenant-1"] = Tenant(
        tenant_id="tenant-1",
        name="Tenant",
        trust_level=TrustLevel.STANDARD,
        default_worker="worker-1",
    )
    registry = build_worker_registry(
        entries=[
            WorkerEntry(
                worker=_make_worker(),
                skill_registry=SkillRegistry.from_skills((_make_skill(),)),
            )
        ],
        default_worker_id="worker-1",
    )
    mcp_server = MCPServer(name="test")
    mcp_server.register_tool(execute_code)
    scope_provider = ExecutionScopeProvider()
    router = WorkerRouter(
        worker_registry=registry,
        tenant_loader=tenant_loader,
        task_runner=runner,
        mcp_server=mcp_server,
        workspace_root=tmp_path,
        execution_scope_provider=scope_provider,
    )
    duty_executor = DutyExecutor(worker_router=router, execution_log_dir=tmp_path)
    duty = Duty(
        duty_id="duty-1",
        title="Daily Duty",
        status="active",
        triggers=(DutyTrigger(id="manual", type="manual"),),
        execution_policy=ExecutionPolicy(default="standard"),
        action="Summarize the current state.",
        quality_criteria=("Provide one concise summary",),
        pre_script=InlineScript(source="print('prefetched context')"),
    )

    record = await duty_executor.execute(
        duty=duty,
        trigger=duty.triggers[0],
        tenant_id="tenant-1",
        worker_id="worker-1",
    )

    assert record.conclusion == "ok"
    assert dispatcher.tasks
    assert "## Pre-execution Script Output" in dispatcher.tasks[0]
    assert "prefetched context" in dispatcher.tasks[0]
    assert "[Duty Execution] Daily Duty" in dispatcher.tasks[0]
    assert [entry["tool_name"] for entry in audit.entries].count("execute_code") == 1


@pytest.mark.asyncio
async def test_script_ref_pre_script_executes_registered_script_tool(
    tmp_path: Path,
    monkeypatch,
):
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
    audit = _AuditCollector()
    pipeline = ToolPipeline(
        executor=ScopedToolExecutor(
            allowed_tools={
                "fetch_metrics": script_tool,
                "echo_tool": helper_tool,
            }
        ),
        audit_logger=audit,
    )
    dispatcher = _CapturingDispatcher()
    task_store = TaskStore(workspace_root=tmp_path)
    runner = TaskRunner(
        engine_dispatcher=dispatcher,
        task_store=task_store,
        tool_pipeline=pipeline,
    )

    tenant_loader = TenantLoader(tmp_path)
    tenant_loader._cache["tenant-1"] = Tenant(
        tenant_id="tenant-1",
        name="Tenant",
        trust_level=TrustLevel.STANDARD,
        default_worker="worker-1",
    )
    registry = build_worker_registry(
        entries=[
            WorkerEntry(
                worker=_make_worker(),
                skill_registry=SkillRegistry.from_skills((_make_skill(),)),
            )
        ],
        default_worker_id="worker-1",
    )
    mcp_server = MCPServer(name="test")
    mcp_server.register_tool(script_tool)
    mcp_server.register_tool(helper_tool)
    scope_provider = ExecutionScopeProvider()
    router = WorkerRouter(
        worker_registry=registry,
        tenant_loader=tenant_loader,
        task_runner=runner,
        mcp_server=mcp_server,
        workspace_root=tmp_path,
        execution_scope_provider=scope_provider,
    )

    manifest = create_task_manifest(
        worker_id="worker-1",
        tenant_id="tenant-1",
        skill_id="skill-1",
        task_description="Run reusable pre-script",
        pre_script=ScriptRef(
            tool_name="fetch_metrics",
            tool_input=(("text", "prod"),),
        ),
    )

    async for _ in router.route_stream(
        task="Run reusable pre-script",
        tenant_id="tenant-1",
        worker_id="worker-1",
        manifest=manifest,
    ):
        pass

    assert dispatcher.tasks
    assert "echo:prod" in dispatcher.tasks[0]
    assert [entry["tool_name"] for entry in audit.entries].count("fetch_metrics") == 1
    assert [entry["tool_name"] for entry in audit.entries].count("echo_tool") == 1
