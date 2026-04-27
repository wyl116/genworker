# edition: baseline
from pathlib import Path

import pytest

from src.autonomy.inbox import SessionInboxStore
from src.engine.langgraph.builder import build_graph_bundle
from src.engine.langgraph.checkpointer import LangGraphCheckpointer
from src.engine.langgraph.engine import LangGraphEngine
from src.engine.langgraph.models import BudgetTracker
from src.engine.protocols import LLMResponse, ToolResult, UsageInfo
from src.engine.state import WorkerContext
from src.skills.parser import SkillParser
from src.streaming.events import ApprovalPendingEvent, ErrorEvent, RunFinishedEvent, TextMessageEvent


class _MockLLM:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    async def invoke(self, messages, tools=None, tool_choice=None, system_blocks=None, intent=None):
        response = self._responses[self.calls]
        self.calls += 1
        return response


class _MockTools:
    async def execute(self, tool_name, tool_input):
        if tool_name == "lookup":
            return ToolResult(content="lookup-ok", metadata={"task": tool_input.get("task", ""), "status": "needs_approval"})
        return ToolResult(content=f"tool:{tool_name}")


class _RecordingTools:
    def __init__(self) -> None:
        self.calls = []

    async def execute(self, tool_name, tool_input):
        self.calls.append((tool_name, dict(tool_input)))
        if tool_name == "lookup":
            return ToolResult(
                content="lookup-ok",
                metadata={"task": tool_input.get("task", ""), "status": "needs_approval"},
            )
        return ToolResult(content=f"tool:{tool_name}")


def _write_skill(tmp_path: Path) -> Path:
    path = tmp_path / "skills" / "approval" / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        """\
---
skill_id: "approval-flow"
strategy:
  mode: "langgraph"
  graph:
    state_schema:
      task: "str"
      status: "str"
      notify: "str"
    entry: "lookup"
    nodes:
      - name: "lookup"
        kind: "tool"
        tool: "lookup"
      - name: "gate"
        kind: "condition"
        route:
          needs_approval: "human_approval"
      - name: "human_approval"
        kind: "interrupt"
        prompt_ref: "approval_prompt"
      - name: "notify"
        kind: "llm"
        instruction_ref: "notify"
    edges:
      - { from: "lookup", to: "gate" }
      - { from: "gate", to: "human_approval", cond: "needs_approval" }
      - { from: "human_approval", to: "notify" }
      - { from: "notify", to: "END" }
---

## instructions.approval_prompt
请审批任务 {task}

## instructions.notify
输出完成通知。
""",
        encoding="utf-8",
    )
    return path


def _write_resume_visibility_skill(tmp_path: Path) -> Path:
    path = tmp_path / "skills" / "approval_visibility" / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        """\
---
skill_id: "approval-visibility"
strategy:
  mode: "langgraph"
  graph:
    state_schema:
      task: "str"
      status: "str"
      done: "str"
    entry: "lookup"
    nodes:
      - name: "lookup"
        kind: "tool"
        tool: "lookup"
      - name: "gate"
        kind: "condition"
        route:
          needs_approval: "human_approval"
      - name: "human_approval"
        kind: "interrupt"
        prompt_ref: "approval_prompt"
      - name: "apply"
        kind: "tool"
        tool: "apply"
    edges:
      - { from: "lookup", to: "gate" }
      - { from: "gate", to: "human_approval", cond: "needs_approval" }
      - { from: "human_approval", to: "apply" }
      - { from: "apply", to: "END" }
---

## instructions.approval_prompt
请审批任务 {task}
""",
        encoding="utf-8",
    )
    return path


@pytest.mark.asyncio
async def test_langgraph_engine_interrupt_and_resume(tmp_path: Path):
    skill_path = _write_skill(tmp_path)
    skill = SkillParser.parse(skill_path)
    worker_context = WorkerContext(worker_id="worker-1", tenant_id="tenant-1")
    llm = _MockLLM(
        [
            LLMResponse(content="approved", usage=UsageInfo(total_tokens=3)),
        ]
    )
    engine = LangGraphEngine(
        workspace_root=tmp_path,
        checkpointer=LangGraphCheckpointer(tmp_path),
        tool_executor=_MockTools(),
        llm_client=llm,
        inbox_store=SessionInboxStore(fallback_dir=tmp_path),
    )

    events = []
    async for event in engine.execute(
        skill,
        worker_context,
        "task-123",
        run_id="run-1",
    ):
        events.append(event)

    pending = [event for event in events if isinstance(event, ApprovalPendingEvent)]
    assert pending
    assert pending[0].thread_id == "run-1"
    assert "task-123" in pending[0].prompt
    assert isinstance(events[-1], RunFinishedEvent)
    assert events[-1].stop_reason == "approval_pending"

    bundle = build_graph_bundle(
        skill=skill,
        node_context=engine._make_context(
            skill=skill,
            worker_context=worker_context,
            thread_id="run-1",
            budget=BudgetTracker().snapshot(),
            available_tools=None,
        ),
        budget_tracker=BudgetTracker(),
    )
    snapshot = await bundle.compiled.aget_state(
        engine._config_for(
            thread_id="run-1",
            tenant_id="tenant-1",
            worker_id="worker-1",
            skill=skill,
            whitelist=bundle.state_whitelist,
        )
    )
    assert dict(getattr(snapshot, "values", {}) or {})["_approval_inbox_id"] == pending[0].inbox_id

    resume_events = []
    async for event in engine.resume(
        tenant_id="tenant-1",
        worker_id="worker-1",
        thread_id="run-1",
        skill_id="approval-flow",
        decision={"approved": True},
        expected_digest=(await engine._checkpointer.load_by_thread("run-1")).state_digest,
        inbox_id=pending[0].inbox_id,
    ):
        resume_events.append(event)

    text_events = [event for event in resume_events if isinstance(event, TextMessageEvent)]
    assert text_events
    assert text_events[0].content == "approved"
    assert isinstance(resume_events[-1], RunFinishedEvent)
    assert resume_events[-1].success is True


@pytest.mark.asyncio
async def test_langgraph_engine_rejects_resume_on_state_drift(tmp_path: Path):
    skill_path = _write_skill(tmp_path)
    skill = SkillParser.parse(skill_path)
    worker_context = WorkerContext(worker_id="worker-1", tenant_id="tenant-1")
    engine = LangGraphEngine(
        workspace_root=tmp_path,
        checkpointer=LangGraphCheckpointer(tmp_path),
        tool_executor=_MockTools(),
        llm_client=_MockLLM([]),
        inbox_store=SessionInboxStore(fallback_dir=tmp_path),
    )

    async for _ in engine.execute(
        skill,
        worker_context,
        "task-123",
        run_id="run-drift",
    ):
        pass

    events = []
    async for event in engine.resume(
        tenant_id="tenant-1",
        worker_id="worker-1",
        thread_id="run-drift",
        skill_id="approval-flow",
        decision={"approved": True},
        expected_digest="stale-digest",
        inbox_id="inbox-1",
    ):
        events.append(event)

    error_events = [event for event in events if isinstance(event, ErrorEvent)]
    assert error_events
    assert error_events[0].code == "LANGGRAPH_STATE_DRIFT"
    assert "expected=stale-digest" in error_events[0].message
    assert isinstance(events[-1], RunFinishedEvent)
    assert events[-1].success is False
    assert events[-1].stop_reason == "state_drift"


@pytest.mark.asyncio
async def test_langgraph_engine_resume_errors_for_unknown_thread(tmp_path: Path):
    engine = LangGraphEngine(
        workspace_root=tmp_path,
        checkpointer=LangGraphCheckpointer(tmp_path),
        tool_executor=_MockTools(),
        llm_client=_MockLLM([]),
        inbox_store=SessionInboxStore(fallback_dir=tmp_path),
    )

    events = []
    async for event in engine.resume(
        tenant_id="tenant-1",
        worker_id="worker-1",
        thread_id="missing-thread",
        skill_id="approval-flow",
        decision={"approved": True},
        expected_digest="",
        inbox_id="inbox-1",
    ):
        events.append(event)

    error_events = [event for event in events if isinstance(event, ErrorEvent)]
    assert error_events
    assert error_events[0].code == "LANGGRAPH_THREAD_NOT_FOUND"
    assert "missing-thread" in error_events[0].message
    assert isinstance(events[-1], RunFinishedEvent)
    assert events[-1].success is False
    assert events[-1].stop_reason == "thread_not_found"


@pytest.mark.asyncio
async def test_langgraph_engine_resume_exposes_approval_state_to_tool_nodes(tmp_path: Path):
    skill = SkillParser.parse(_write_resume_visibility_skill(tmp_path))
    tools = _RecordingTools()
    engine = LangGraphEngine(
        workspace_root=tmp_path,
        checkpointer=LangGraphCheckpointer(tmp_path),
        tool_executor=tools,
        llm_client=_MockLLM([]),
        inbox_store=SessionInboxStore(fallback_dir=tmp_path),
    )

    events = []
    async for event in engine.execute(
        skill,
        WorkerContext(worker_id="worker-1", tenant_id="tenant-1"),
        "task-123",
        run_id="run-approval-state",
    ):
        events.append(event)

    pending = next(event for event in events if isinstance(event, ApprovalPendingEvent))
    record = await engine._checkpointer.load_by_thread("run-approval-state")
    assert record is not None

    async for _ in engine.resume(
        tenant_id="tenant-1",
        worker_id="worker-1",
        thread_id="run-approval-state",
        skill_id="approval-visibility",
        decision={"approved": True, "note": "ship it"},
        expected_digest=record.state_digest,
        inbox_id=pending.inbox_id,
    ):
        pass

    assert [name for name, _ in tools.calls] == ["lookup", "apply"]
    _, apply_input = tools.calls[-1]
    assert apply_input["_approval_decision"] == {"approved": True, "note": "ship it"}
    assert apply_input["_approval_inbox_id"] == pending.inbox_id
