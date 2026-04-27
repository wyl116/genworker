# edition: baseline
from pathlib import Path

import pytest

from src.autonomy.inbox import SessionInboxStore
from src.engine.langgraph.checkpointer import LangGraphCheckpointer
from src.engine.langgraph.engine import LangGraphEngine
from src.engine.protocols import LLMResponse, UsageInfo
from src.engine.state import WorkerContext
from src.skills.parser import SkillParser
from src.streaming.events import ApprovalPendingEvent, TextMessageEvent


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
        from src.engine.protocols import ToolResult

        return ToolResult(
            content="lookup-ok",
            metadata={"task": tool_input.get("task", ""), "status": "needs_approval"},
        )


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


@pytest.mark.asyncio
async def test_langgraph_interrupt_resume_flow(tmp_path: Path):
    skill = SkillParser.parse(_write_skill(tmp_path))
    llm = _MockLLM([LLMResponse(content="approved", usage=UsageInfo(total_tokens=3))])
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
        WorkerContext(worker_id="worker-1", tenant_id="tenant-1"),
        "task-123",
        run_id="run-1",
    ):
        events.append(event)

    pending = next(event for event in events if isinstance(event, ApprovalPendingEvent))
    record = await engine._checkpointer.load_by_thread("run-1")
    assert record is not None

    resumed = []
    async for event in engine.resume(
        tenant_id="tenant-1",
        worker_id="worker-1",
        thread_id="run-1",
        skill_id="approval-flow",
        decision={"approved": True},
        expected_digest=record.state_digest,
        inbox_id=pending.inbox_id,
    ):
        resumed.append(event)

    texts = [event.content for event in resumed if isinstance(event, TextMessageEvent)]
    assert texts == ["approved"]
