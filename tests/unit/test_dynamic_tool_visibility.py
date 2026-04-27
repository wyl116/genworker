# edition: baseline
from pathlib import Path

import pytest

from src.common.tenant import Tenant, TenantLoader, TrustLevel
from src.skills.models import Skill, SkillKeyword, SkillScope, SkillStrategy, StrategyMode
from src.skills.registry import SkillRegistry
from src.tools.mcp.server import MCPServer
from src.tools.mcp.tool import Tool
from src.tools.mcp.types import MCPCategory, RiskLevel, ToolType
from src.worker.models import Worker, WorkerIdentity
from src.worker.registry import WorkerEntry, build_worker_registry
from src.worker.router import WorkerRouter


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


def _make_skill() -> Skill:
    return Skill(
        skill_id="skill-1",
        name="Skill 1",
        scope=SkillScope.SYSTEM,
        strategy=SkillStrategy(mode=StrategyMode.AUTONOMOUS),
        keywords=(SkillKeyword(keyword="task", weight=1.0),),
        default_skill=True,
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


@pytest.mark.asyncio
async def test_router_sees_newly_registered_tools_without_rebuild(tmp_path: Path):
    runner = _CapturingRunner()
    tenant = Tenant(tenant_id="demo", name="Demo", trust_level=TrustLevel.STANDARD)
    tenant_loader = TenantLoader(tmp_path)
    tenant_loader._cache["demo"] = tenant
    registry = build_worker_registry(
        [WorkerEntry(worker=_make_worker(), skill_registry=SkillRegistry.from_skills((_make_skill(),)))],
        default_worker_id="w1",
    )
    mcp_server = MCPServer(name="test")
    mcp_server.register_tool(_make_tool("tool_a"))
    router = WorkerRouter(
        worker_registry=registry,
        tenant_loader=tenant_loader,
        task_runner=runner,
        mcp_server=mcp_server,
        workspace_root=tmp_path,
    )

    async for _ in router.route_stream(task="task", tenant_id="demo", worker_id="w1"):
        pass
    first_tools = {item["function"]["name"] for item in runner.calls[-1]["available_tools"]}
    assert "tool_a" in first_tools

    mcp_server.register_tool(_make_tool("tool_b"))
    async for _ in router.route_stream(task="task", tenant_id="demo", worker_id="w1"):
        pass
    second_tools = {item["function"]["name"] for item in runner.calls[-1]["available_tools"]}
    assert "tool_b" in second_tools
