# edition: baseline
import pytest

from src.streaming.events import TextMessageEvent
from src.common.tenant import Tenant, TrustLevel
from src.tools.mcp.tool import Tool
from src.tools.mcp.types import MCPCategory, RiskLevel, ToolType
from src.worker.models import WorkerToolPolicy
from src.worker.tool_scope import build_tool_runtime_bundle, create_delegate_to_worker_tool


class _TrustGate:
    trusted = True
    bash_enabled = True
    mcp_remote_enabled = False
    learned_rules_enabled = True
    episodic_write_enabled = True
    cross_worker_sharing_enabled = True
    semantic_search_enabled = True


class _CapturingRouter:
    def __init__(self) -> None:
        self.calls = []

    async def route_stream(self, **kwargs):
        self.calls.append(kwargs)
        yield TextMessageEvent(run_id="run-1", content="delegated ok")


class _Worker:
    worker_id = "worker-a"
    tool_policy = WorkerToolPolicy()


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
async def test_delegate_tool_preserves_self_in_whitelist_for_next_hop():
    router = _CapturingRouter()
    tool = create_delegate_to_worker_tool(
        worker_router=router,
        tenant_id="tenant-1",
        source_worker_id="worker-a",
        inherited_tool_names=("tool_a", "delegate_to_worker", "spawn_task"),
        delegation_depth=0,
    )

    result = await tool.handler(
        target_worker="worker-b",
        task="analyze report",
        context="ctx",
    )

    assert result.content == "delegated ok"
    assert len(router.calls) == 1
    assert router.calls[0]["tool_whitelist"] == (
        "tool_a",
        "delegate_to_worker",
        "spawn_task",
    )
    assert router.calls[0]["subagent_depth"] == 1


@pytest.mark.asyncio
async def test_build_bundle_applies_whitelist_to_runtime_injected_tools():
    router = _CapturingRouter()
    bundle = build_tool_runtime_bundle(
        worker=_Worker(),
        tenant=Tenant(tenant_id="tenant-1", name="Tenant", trust_level=TrustLevel.FULL),
        trust_gate=_TrustGate(),
        all_tools=(_make_tool("tool_a"), _make_tool("tool_b")),
        worker_router=router,
        subagent_executor=None,
        create_subagent_tool_fn=None,
        task_spawner=None,
        conversation_session=None,
        session_search_index=object(),
        tool_whitelist=("tool_a", "delegate_to_worker"),
        subagent_depth=0,
        parent_task_id="task-1",
    )

    tool_names = {tool["function"]["name"] for tool in bundle.tool_schemas}
    assert tool_names == {"tool_a", "delegate_to_worker"}

    delegate_tool = bundle.scope.scoped_tools["delegate_to_worker"]
    result = await delegate_tool.handler(
        target_worker="worker-b",
        task="analyze report",
        context="ctx",
    )

    assert result.content == "delegated ok"
    assert len(router.calls) == 1
    assert router.calls[0]["tool_whitelist"] == ("tool_a", "delegate_to_worker")
