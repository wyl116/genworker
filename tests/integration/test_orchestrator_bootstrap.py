# edition: baseline
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from src.bootstrap import ApiWiringInitializer
from src.bootstrap.context import BootstrapContext
from src.channels.commands.approval_events import approval_event_types
from src.events.bus import EventBus
from src.memory.backends.openviking import OpenVikingClient


class _MCPServer:
    def get_all_tools(self):
        return ()

    def get_tool(self, name):
        return None


@pytest.mark.asyncio
async def test_api_wiring_initializes_memory_orchestrator(tmp_path: Path):
    skill_path = tmp_path / "system" / "skills" / "order-approval" / "SKILL.md"
    skill_path.parent.mkdir(parents=True, exist_ok=True)
    skill_path.write_text(
        """\
---
skill_id: "order-approval"
strategy:
  mode: "langgraph"
  graph:
    state_schema:
      task: "str"
    entry: "human_approval"
    nodes:
      - name: "human_approval"
        kind: "interrupt"
        prompt_ref: "approval_prompt"
        inbox_event_type: "bootstrap_order_approval"
    edges: []
---

## instructions.approval_prompt
审批 {task}
""",
        encoding="utf-8",
    )

    context = BootstrapContext(
        settings=SimpleNamespace(
            environment="development",
            openviking_scope_prefix="viking://",
        ),
    )
    context.set_state("workspace_root", tmp_path)
    context.set_state("worker_registry", object())
    context.set_state("mcp_server", _MCPServer())
    context.set_state(
        "openviking_client",
        OpenVikingClient(
            endpoint="http://openviking.local",
            http_client=None,
        ),
    )
    context.set_state("event_bus", EventBus())

    initializer = ApiWiringInitializer()
    success = await initializer.initialize(context)

    assert success is True
    orchestrator = context.get_state("memory_orchestrator")
    router = context.get_state("worker_router")
    dispatcher = context.get_state("engine_dispatcher")
    assert orchestrator is not None
    assert tuple(provider.name for provider in orchestrator.providers) == (
        "semantic",
        "episodic",
        "preference",
    )
    assert router._memory_orchestrator is orchestrator
    assert context.get_state("subagent_executor") is not None
    assert context.get_state("enhanced_planning_executor") is not None
    assert router._subagent_executor is context.get_state("subagent_executor")
    assert dispatcher._enhanced_planning_executor is context.get_state(
        "enhanced_planning_executor"
    )
    assert "bootstrap_order_approval" in approval_event_types()
