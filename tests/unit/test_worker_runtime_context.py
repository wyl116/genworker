# edition: baseline
from __future__ import annotations

import pytest

from src.common.tenant import Tenant
from src.worker.models import Worker, WorkerHeartbeatConfig, WorkerIdentity, WorkerMode
from src.worker.runtime_context import WorkerRuntimeContextBuilder
from src.worker.task import TaskProvenance
from src.worker.goal.models import Goal, Milestone
from src.worker.integrations.goal_generator import write_goal_md
from src.worker.scripts.models import InlineScript
from src.worker.trust_gate import WorkerTrustGate


class _Skill:
    skill_id = "skill-1"


def _make_worker() -> Worker:
    return Worker(
        identity=WorkerIdentity(
            name="Planner Worker",
            worker_id="worker-1",
        ),
        mode=WorkerMode.PERSONAL,
        heartbeat_config=WorkerHeartbeatConfig(),
    )


@pytest.mark.asyncio
async def test_runtime_context_loads_goal_default_pre_script_into_worker_context(tmp_path):
    tenant = Tenant(tenant_id="tenant-1", name="Tenant")
    worker = _make_worker()
    trust_gate = WorkerTrustGate(
        trusted=True,
        bash_enabled=True,
        semantic_search_enabled=True,
        learned_rules_enabled=True,
        episodic_write_enabled=True,
    )
    worker_dir = tmp_path / "tenants" / tenant.tenant_id / "workers" / worker.worker_id
    write_goal_md(
        Goal(
            goal_id="goal-1",
            title="Goal With Pre Script",
            status="active",
            priority="high",
            milestones=(Milestone(id="ms-1", title="M1", status="pending"),),
            default_pre_script=InlineScript(source="print('goal context')"),
        ),
        worker_dir / "goals",
        filename="goal-1.md",
    )
    builder = WorkerRuntimeContextBuilder(
        workspace_root=tmp_path,
        memory_orchestrator=None,
    )

    bundle = await builder.build(
        worker=worker,
        tenant=tenant,
        trust_gate=trust_gate,
        skill=_Skill(),
        available_tools=(),
        available_skill_ids=(),
        task="Investigate the goal",
        task_context="",
        contact_context="",
        subagent_enabled=False,
        provenance=TaskProvenance(goal_id="goal-1"),
    )

    assert isinstance(bundle.worker_context.goal_default_pre_script, InlineScript)
    assert bundle.worker_context.goal_default_pre_script.source.strip() == "print('goal context')"


@pytest.mark.asyncio
async def test_runtime_context_skips_missing_goal_default_pre_script(tmp_path):
    tenant = Tenant(tenant_id="tenant-1", name="Tenant")
    worker = _make_worker()
    trust_gate = WorkerTrustGate(
        trusted=True,
        bash_enabled=True,
        semantic_search_enabled=True,
        learned_rules_enabled=True,
        episodic_write_enabled=True,
    )
    builder = WorkerRuntimeContextBuilder(
        workspace_root=tmp_path,
        memory_orchestrator=None,
    )

    bundle = await builder.build(
        worker=worker,
        tenant=tenant,
        trust_gate=trust_gate,
        skill=_Skill(),
        available_tools=(),
        available_skill_ids=(),
        task="Investigate the goal",
        task_context="",
        contact_context="",
        subagent_enabled=False,
        provenance=TaskProvenance(goal_id="missing-goal"),
    )

    assert bundle.worker_context.goal_default_pre_script is None
