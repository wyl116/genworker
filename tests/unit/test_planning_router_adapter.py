# edition: baseline
import pytest

from src.runtime.bootstrap_builders import _RouterAdapter
from src.streaming.events import ErrorEvent, RunFinishedEvent, TextMessageEvent
from src.worker.planning.models import SubGoal
from src.worker.planning.subagent.models import SubAgentContext
from src.worker.scripts.models import InlineScript


class _FakeRouter:
    def __init__(self, events):
        self._events = events
        self.calls = []

    async def route_stream(self, **kwargs):
        self.calls.append(kwargs)
        for event in self._events:
            yield event


@pytest.mark.asyncio
async def test_router_adapter_forwards_skill_and_delegate():
    adapter = _RouterAdapter(tenant_id="demo")
    router = _FakeRouter(events=(TextMessageEvent(run_id="r1", content="alpha"),))
    adapter.set_router(router)
    context = SubAgentContext(
        agent_id="sa-1",
        parent_worker_id="worker-parent",
        parent_task_id="task-1",
        sub_goal=SubGoal(id="g1", description="do thing"),
        skill_id="analysis-skill",
        delegate_worker_id="worker-delegate",
        tool_sandbox=("tool_a",),
        max_rounds=3,
    )

    result = await adapter.execute_subagent(context)

    assert result == "alpha"
    assert router.calls == [{
        "task": "do thing",
        "tenant_id": "demo",
        "worker_id": "worker-delegate",
        "skill_id": "analysis-skill",
        "preferred_skill_ids": (),
        "tool_whitelist": ("tool_a",),
        "subagent_depth": 1,
        "max_rounds_override": 3,
    }]


@pytest.mark.asyncio
async def test_router_adapter_forwards_soft_preferred_skills():
    adapter = _RouterAdapter(tenant_id="demo")
    router = _FakeRouter(events=(TextMessageEvent(run_id="r1", content="alpha"),))
    adapter.set_router(router)
    context = SubAgentContext(
        agent_id="sa-1",
        parent_worker_id="worker-parent",
        parent_task_id="task-1",
        sub_goal=SubGoal(id="g1", description="do thing"),
        preferred_skill_ids=("analysis-skill", "report-skill"),
    )

    result = await adapter.execute_subagent(context)

    assert result == "alpha"
    assert router.calls[0]["preferred_skill_ids"] == ("analysis-skill", "report-skill")


@pytest.mark.asyncio
async def test_router_adapter_raises_on_failed_route():
    adapter = _RouterAdapter(tenant_id="demo")
    router = _FakeRouter(events=(
        ErrorEvent(run_id="r1", code="SKILL_NOT_FOUND", message="missing skill"),
        RunFinishedEvent(run_id="r1", success=False, stop_reason="missing skill"),
    ))
    adapter.set_router(router)
    context = SubAgentContext(
        agent_id="sa-1",
        parent_worker_id="worker-parent",
        parent_task_id="task-1",
        sub_goal=SubGoal(id="g1", description="do thing"),
    )

    with pytest.raises(RuntimeError, match="missing skill"):
        await adapter.execute_subagent(context)


@pytest.mark.asyncio
async def test_router_adapter_builds_manifest_when_subagent_has_pre_script():
    adapter = _RouterAdapter(tenant_id="demo")
    router = _FakeRouter(events=(TextMessageEvent(run_id="r1", content="alpha"),))
    adapter.set_router(router)
    context = SubAgentContext(
        agent_id="sa-1",
        parent_worker_id="worker-parent",
        parent_task_id="task-1",
        sub_goal=SubGoal(id="g1", description="do thing"),
        preferred_skill_ids=("analysis-skill",),
        pre_script=InlineScript(source="print('goal prefetch')"),
    )

    result = await adapter.execute_subagent(context)

    assert result == "alpha"
    assert "manifest" in router.calls[0]
    manifest = router.calls[0]["manifest"]
    assert manifest.task_description == "do thing"
    assert manifest.preferred_skill_ids == ("analysis-skill",)
    assert isinstance(manifest.pre_script, InlineScript)
    assert manifest.pre_script.source.strip() == "print('goal prefetch')"
