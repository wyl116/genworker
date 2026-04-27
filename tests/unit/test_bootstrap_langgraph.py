# edition: baseline
from pathlib import Path
from types import SimpleNamespace

from src.engine.router.engine_dispatcher import EngineDispatcher
from src.runtime.api_wiring import (
    build_engine_dispatcher,
    build_engine_registry,
    register_langgraph_approval_event_types,
)
from src.runtime.bootstrap_builders import build_langgraph_stack


class _MockLLM:
    async def invoke(self, messages, tools=None, tool_choice=None, system_blocks=None, intent=None):
        from src.engine.protocols import LLMResponse

        return LLMResponse(content="ok")


class _MockTools:
    async def execute(self, tool_name, tool_input):
        from src.engine.protocols import ToolResult

        return ToolResult(content="ok")


def test_build_langgraph_stack_returns_checkpointer_and_engine(tmp_path: Path):
    checkpointer, engine = build_langgraph_stack(
        workspace_root=tmp_path,
        tool_executor=_MockTools(),
        llm_client=_MockLLM(),
        inbox_store=SimpleNamespace(write=None),
    )

    assert checkpointer is not None
    assert engine is not None
    assert engine._checkpointer is checkpointer


def test_build_langgraph_stack_gracefully_degrades_when_import_fails(tmp_path: Path, monkeypatch):
    import importlib

    original_import_module = importlib.import_module

    def _fake_import_module(name: str, package=None):
        if name == "src.engine.langgraph.checkpointer":
            raise ImportError("langgraph missing")
        return original_import_module(name, package)

    monkeypatch.setattr(importlib, "import_module", _fake_import_module)

    checkpointer, engine = build_langgraph_stack(
        workspace_root=tmp_path,
        tool_executor=_MockTools(),
        llm_client=_MockLLM(),
        inbox_store=SimpleNamespace(write=None),
    )

    assert checkpointer is None
    assert engine is None


def test_build_engine_dispatcher_injects_langgraph_engine():
    langgraph_engine = object()

    dispatcher = build_engine_dispatcher(
        llm_client=_MockLLM(),
        tool_executor=_MockTools(),
        mcp_server=None,
        memory_flush_callback=None,
        enhanced_planning_executor=None,
        state_checkpointer=None,
        langgraph_engine=langgraph_engine,
    )

    assert isinstance(dispatcher, EngineDispatcher)
    assert dispatcher.langgraph_engine is langgraph_engine


def test_build_engine_registry_aggregates_all_engine_statuses():
    registry = build_engine_registry(
        llm_client=object(),
        tool_executor=object(),
        enhanced_planning_executor=object(),
        langgraph_checkpointer=object(),
        langgraph_engine=object(),
    )

    assert registry["autonomous"]["ready"] is True
    assert registry["deterministic"]["ready"] is True
    assert registry["hybrid"]["ready"] is True
    assert registry["planning"]["ready"] is True
    assert registry["langgraph"]["import_ok"] is True
    assert registry["langgraph"]["checkpointer_ok"] is True


def test_register_langgraph_approval_event_types_scans_workspace(tmp_path: Path):
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
        inbox_event_type: "order_approval_bootstrap"
    edges: []
---

## instructions.approval_prompt
审批 {task}
""",
        encoding="utf-8",
    )

    registered = register_langgraph_approval_event_types(tmp_path)

    assert "order_approval_bootstrap" in registered
