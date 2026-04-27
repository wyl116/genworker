# edition: baseline
from pathlib import Path

import pytest

from src.autonomy.inbox import SessionInboxStore
from src.engine.langgraph.checkpointer import LangGraphCheckpointer
from src.engine.langgraph.engine import LangGraphEngine
from src.engine.router.engine_dispatcher import EngineDispatcher
from src.engine.protocols import LLMResponse, UsageInfo
from src.engine.state import WorkerContext
from src.skills.parser import SkillParser
from src.streaming.events import RunFinishedEvent, TextMessageEvent


class _MockLLM:
    async def invoke(self, messages, tools=None, tool_choice=None, system_blocks=None, intent=None):
        return LLMResponse(content="python-factory-ok", usage=UsageInfo(total_tokens=2))


class _MockTools:
    async def execute(self, tool_name, tool_input):
        from src.engine.protocols import ToolResult

        return ToolResult(content=f"tool:{tool_name}")


def _write_skill(tmp_path: Path) -> Path:
    base = tmp_path / "workspace" / "system" / "skills" / "demo_python_graph"
    base.mkdir(parents=True, exist_ok=True)
    (base / "SKILL.md").write_text(
        """\
---
name: "demo-python-graph"
metadata:
  genworker:
    strategy:
      mode: "langgraph"
      graph:
        module: "workspace.system.skills.demo_python_graph.graph"
        factory: "build_graph"
        state_schema_ref: "DemoState"
---

## instructions.summary
总结任务结果。
""",
        encoding="utf-8",
    )
    (base / "graph.py").write_text(
        """\
from __future__ import annotations

from dataclasses import dataclass

from langgraph.graph import END, StateGraph


@dataclass(frozen=True)
class DemoState:
    task: str = ""
    summary: str = ""


def build_graph(ctx):
    graph = StateGraph(DemoState)

    async def summarize(state: DemoState):
        response = await ctx.llm.invoke(
            messages=[
                {"role": "system", "content": ctx.instruction("summary")},
                {"role": "user", "content": state.task},
            ],
            intent=ctx.intent("summary"),
        )
        return {"summary": response.content}

    graph.add_node("summarize", summarize)
    graph.set_entry_point("summarize")
    graph.add_edge("summarize", END)
    return graph.compile(checkpointer=ctx.checkpointer)
""",
        encoding="utf-8",
    )
    return base / "SKILL.md"


def _write_broken_fallback_skill(tmp_path: Path) -> Path:
    base = tmp_path / "workspace" / "system" / "skills" / "demo_python_graph_broken"
    base.mkdir(parents=True, exist_ok=True)
    (base / "SKILL.md").write_text(
        """\
---
name: "demo-python-graph-broken"
metadata:
  genworker:
    strategy:
      mode: "langgraph"
      fallback:
        condition: "langgraph_unavailable"
        mode: "autonomous"
      graph:
        module: "workspace.system.skills.demo_python_graph_broken.missing_graph"
        factory: "build_graph"
        state_schema_ref: "DemoState"
---

## instructions.general
请直接回复 fallback-ok。
""",
        encoding="utf-8",
    )
    return base / "SKILL.md"


@pytest.mark.asyncio
async def test_langgraph_python_factory_flow(tmp_path: Path, monkeypatch):
    skill_path = _write_skill(tmp_path)
    skill = SkillParser.parse(skill_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    engine = LangGraphEngine(
        workspace_root=tmp_path,
        checkpointer=LangGraphCheckpointer(tmp_path),
        tool_executor=_MockTools(),
        llm_client=_MockLLM(),
        inbox_store=SessionInboxStore(fallback_dir=tmp_path),
    )

    events = []
    async for event in engine.execute(
        skill,
        WorkerContext(worker_id="worker-1", tenant_id="tenant-1"),
        "task-456",
        run_id="run-python",
    ):
        events.append(event)

    texts = [event.content for event in events if isinstance(event, TextMessageEvent)]
    assert "python-factory-ok" in texts


@pytest.mark.asyncio
async def test_langgraph_python_factory_failure_falls_back_to_autonomous(tmp_path: Path, monkeypatch):
    skill_path = _write_broken_fallback_skill(tmp_path)
    skill = SkillParser.parse(skill_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    dispatcher = EngineDispatcher(
        llm_client=_MockLLM(),
        tool_executor=_MockTools(),
        langgraph_engine=LangGraphEngine(
            workspace_root=tmp_path,
            checkpointer=LangGraphCheckpointer(tmp_path),
            tool_executor=_MockTools(),
            llm_client=_MockLLM(),
            inbox_store=SessionInboxStore(fallback_dir=tmp_path),
        ),
    )

    events = []
    async for event in dispatcher.dispatch(
        skill=skill,
        worker_context=WorkerContext(worker_id="worker-1", tenant_id="tenant-1"),
        task="task-789",
    ):
        events.append(event)

    texts = [event.content for event in events if isinstance(event, TextMessageEvent)]
    assert "python-factory-ok" in texts
    assert isinstance(events[-1], RunFinishedEvent)
    assert events[-1].success is True
