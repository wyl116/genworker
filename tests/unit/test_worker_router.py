# edition: baseline
from pathlib import Path

import pytest

import src.worker.router as worker_router_module
from src.common.tenant import Tenant, TenantLoader, TrustLevel
from src.skills.models import Skill, SkillKeyword, SkillScope, SkillStrategy, StrategyMode
from src.skills.registry import SkillRegistry
from src.tools.mcp.server import MCPServer
from src.tools.mcp.tool import Tool
from src.tools.mcp.types import MCPCategory, RiskLevel, ToolType
from src.worker.models import Worker, WorkerIdentity
from src.worker.registry import WorkerEntry, build_worker_registry
from src.worker.router import WorkerRouter
from src.worker.task import create_task_manifest


class _CapturingRunner:
    def __init__(self):
        self.calls = []

    async def execute(self, **kwargs):
        self.calls.append(kwargs)
        if False:
            yield None
        return


def _make_worker() -> Worker:
    return Worker(
        identity=WorkerIdentity(name="Test", worker_id="w1"),
        default_skill="skill-1",
    )


def _make_skill(
    skill_id: str = "skill-1",
    *,
    keyword: str = "task",
    default_skill: bool = False,
) -> Skill:
    return Skill(
        skill_id=skill_id,
        name=f"Skill {skill_id}",
        scope=SkillScope.SYSTEM,
        strategy=SkillStrategy(mode=StrategyMode.AUTONOMOUS),
        keywords=(SkillKeyword(keyword=keyword, weight=1.0),),
        default_skill=default_skill,
    )


def _make_tool(name: str) -> Tool:
    return Tool(
        name=name,
        description=f"tool {name}",
        handler=lambda: {"ok": True},
        tool_type=ToolType.READ,
        category=MCPCategory.GLOBAL,
        risk_level=RiskLevel.LOW,
    )


def _make_router(
    tmp_path: Path,
    *,
    skills: tuple[Skill, ...] | None = None,
) -> tuple[WorkerRouter, _CapturingRunner]:
    runner = _CapturingRunner()
    tenant = Tenant(tenant_id="demo", name="Demo", trust_level=TrustLevel.STANDARD)
    tenant_loader = TenantLoader(tmp_path)
    tenant_loader._cache["demo"] = tenant
    skill_set = skills or (_make_skill(default_skill=True),)
    registry = build_worker_registry(
        [WorkerEntry(worker=_make_worker(), skill_registry=SkillRegistry.from_skills(skill_set))],
        default_worker_id="w1",
    )
    mcp_server = MCPServer(name="test")
    mcp_server.register_tool(_make_tool("tool_a"))
    mcp_server.register_tool(_make_tool("tool_b"))
    router = WorkerRouter(
        worker_registry=registry,
        tenant_loader=tenant_loader,
        task_runner=runner,
        mcp_server=mcp_server,
        workspace_root=tmp_path,
        subagent_executor=object(),
    )
    return router, runner


@pytest.mark.asyncio
async def test_route_stream_filters_tools_with_whitelist(tmp_path: Path, monkeypatch):
    router, runner = _make_router(tmp_path)
    monkeypatch.setattr(worker_router_module, "_SUBAGENT_AVAILABLE", False)

    async for _ in router.route_stream(
        task="task",
        tenant_id="demo",
        worker_id="w1",
        tool_whitelist=("tool_a",),
    ):
        pass

    tool_names = {item["function"]["name"] for item in runner.calls[-1]["available_tools"]}
    assert "tool_a" in tool_names
    assert "tool_b" not in tool_names
    assert "task_create" not in tool_names
    assert "task_list" not in tool_names
    assert "task_update" not in tool_names


@pytest.mark.asyncio
async def test_subagent_depth_disables_spawn_tool(tmp_path: Path, monkeypatch):
    router, runner = _make_router(tmp_path)
    monkeypatch.setattr(worker_router_module, "_SUBAGENT_AVAILABLE", True)
    monkeypatch.setattr(
        worker_router_module,
        "create_spawn_subagents_tool",
        lambda **kwargs: _make_tool("spawn_subagents"),
    )

    async for _ in router.route_stream(
        task="task",
        tenant_id="demo",
        worker_id="w1",
        subagent_depth=1,
    ):
        pass

    tool_names = {item["function"]["name"] for item in runner.calls[-1]["available_tools"]}
    assert "spawn_subagents" not in tool_names


@pytest.mark.asyncio
async def test_route_stream_uses_explicit_skill_id(tmp_path: Path, monkeypatch):
    router, runner = _make_router(
        tmp_path,
        skills=(
            _make_skill("skill-1", keyword="alpha", default_skill=True),
            _make_skill("skill-2", keyword="beta"),
        ),
    )
    monkeypatch.setattr(worker_router_module, "_SUBAGENT_AVAILABLE", False)

    async for _ in router.route_stream(
        task="task with no keyword match",
        tenant_id="demo",
        worker_id="w1",
        skill_id="skill-2",
    ):
        pass

    assert runner.calls[-1]["skill"].skill_id == "skill-2"


@pytest.mark.asyncio
async def test_route_stream_uses_manifest_skill_id(tmp_path: Path, monkeypatch):
    router, runner = _make_router(
        tmp_path,
        skills=(
            _make_skill("skill-1", keyword="alpha", default_skill=True),
            _make_skill("skill-2", keyword="beta"),
        ),
    )
    monkeypatch.setattr(worker_router_module, "_SUBAGENT_AVAILABLE", False)
    manifest = create_task_manifest(
        worker_id="w1",
        tenant_id="demo",
        skill_id="skill-2",
        task_description="manifest skill route",
    )

    async for _ in router.route_stream(
        task="task with no keyword match",
        tenant_id="demo",
        worker_id="w1",
        manifest=manifest,
    ):
        pass

    assert runner.calls[-1]["skill"].skill_id == "skill-2"


@pytest.mark.asyncio
async def test_route_stream_prefers_soft_preferred_skill_ids(tmp_path: Path, monkeypatch):
    router, runner = _make_router(
        tmp_path,
        skills=(
            _make_skill("skill-1", keyword="task"),
            _make_skill("skill-2", keyword="task"),
        ),
    )
    monkeypatch.setattr(worker_router_module, "_SUBAGENT_AVAILABLE", False)

    async for _ in router.route_stream(
        task="task",
        tenant_id="demo",
        worker_id="w1",
        preferred_skill_ids=("skill-2",),
    ):
        pass

    assert runner.calls[-1]["skill"].skill_id == "skill-2"


@pytest.mark.asyncio
async def test_route_stream_manifest_prefers_soft_preferred_skill_ids(tmp_path: Path, monkeypatch):
    router, runner = _make_router(
        tmp_path,
        skills=(
            _make_skill("skill-1", keyword="task"),
            _make_skill("skill-2", keyword="task"),
        ),
    )
    monkeypatch.setattr(worker_router_module, "_SUBAGENT_AVAILABLE", False)
    manifest = create_task_manifest(
        worker_id="w1",
        tenant_id="demo",
        preferred_skill_ids=("skill-2",),
        task_description="soft preference route",
    )

    async for _ in router.route_stream(
        task="task",
        tenant_id="demo",
        worker_id="w1",
        manifest=manifest,
    ):
        pass

    assert runner.calls[-1]["skill"].skill_id == "skill-2"
