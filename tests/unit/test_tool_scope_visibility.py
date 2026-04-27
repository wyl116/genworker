# edition: baseline
from src.common.tenant import Tenant, TrustLevel
from src.tools.mcp.tool import Tool
from src.tools.mcp.types import MCPCategory, RiskLevel, ToolType
from src.worker.models import WorkerToolPolicy
from src.worker.tool_scope import build_tool_runtime_bundle


class _TrustGate:
    trusted = True
    bash_enabled = True
    mcp_remote_enabled = False
    learned_rules_enabled = True
    episodic_write_enabled = True
    cross_worker_sharing_enabled = True
    semantic_search_enabled = False


class _Worker:
    worker_id = "worker-a"
    tool_policy = WorkerToolPolicy()


def _make_tool(name: str, *, hidden: bool = False) -> Tool:
    tags = frozenset({"hidden_from_llm"}) if hidden else frozenset()
    return Tool(
        name=name,
        description=f"tool {name}",
        handler=lambda: {"ok": True},
        tool_type=ToolType.READ,
        category=MCPCategory.GLOBAL,
        risk_level=RiskLevel.LOW,
        tags=tags,
    )


def test_hidden_tools_are_filtered_from_llm_schema_only():
    bundle = build_tool_runtime_bundle(
        worker=_Worker(),
        tenant=Tenant(tenant_id="tenant-1", name="Tenant", trust_level=TrustLevel.FULL),
        trust_gate=_TrustGate(),
        all_tools=(_make_tool("visible"), _make_tool("hidden", hidden=True)),
        worker_router=object(),
        subagent_executor=None,
        create_subagent_tool_fn=None,
        task_spawner=None,
        conversation_session=None,
        session_search_index=None,
        tool_whitelist=("visible", "hidden"),
        subagent_depth=0,
        parent_task_id="task-1",
    )

    assert {schema["function"]["name"] for schema in bundle.tool_schemas} == {"visible"}
    assert bundle.scope.allowed_tool_names == frozenset({"visible", "hidden"})
