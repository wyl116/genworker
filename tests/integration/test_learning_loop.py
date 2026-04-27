# edition: baseline
from __future__ import annotations

from pathlib import Path

import pytest

from src.common.tenant import Tenant, TenantLoader, TrustLevel
from src.engine.protocols import LLMResponse, ToolResult, UsageInfo
from src.engine.router.engine_dispatcher import EngineDispatcher
from src.memory.episodic.linkage import load_linkage
from src.memory.episodic.store import IndexFileLock, load_episode, load_index
from src.runtime.task_hooks import build_post_run_handler
from src.skills.models import Skill, SkillKeyword, SkillScope, SkillStrategy, StrategyMode
from src.skills.parser import SkillParser
from src.skills.registry import SkillRegistry
from src.tools.mcp.server import MCPServer
from src.worker.duty.duty_executor import DutyExecutor
from src.worker.duty.execution_log import load_recent_records, write_execution_record
from src.worker.duty.models import Duty, DutyExecutionRecord, DutyTrigger, ExecutionPolicy
from src.worker.duty.duty_learning import handle_duty_post_execution
from src.worker.models import Worker, WorkerIdentity
from src.worker.registry import WorkerEntry, build_worker_registry
from src.worker.router import WorkerRouter
from src.worker.rules.crystallizer import run_crystallization_cycle
from src.worker.rules.models import Rule, RuleScope, RuleSource, rule_to_markdown
from src.worker.rules.rule_manager import load_rules
from src.worker.task import TaskStore
from src.worker.task_runner import TaskRunner


class _SequencedLLM:
    def __init__(self):
        self.calls = 0

    async def invoke(self, messages, tools=None, tool_choice=None, system_blocks=None, intent=None):
        self.calls += 1
        prompt_text = "\n".join(str(item.get("content", "")) for item in messages)
        if "Execution summary:" in prompt_text or "把下面的规则扩展为简洁的操作说明" in prompt_text:
            if "Duty" in prompt_text or "error" in prompt_text.lower():
                return LLMResponse(
                    content='{"rule":"When duty execution reports errors, record anomalies before escalation","reason":"Duty failures should be triaged consistently","category":"strategy"}',
                    usage=UsageInfo(total_tokens=5),
                )
            return LLMResponse(
                content='{"rule":"Step 1 validate inputs. Step 2 compare outputs. Step 3 summarize findings.","reason":"Stable workflow extracted from successful execution","category":"strategy"}',
                usage=UsageInfo(total_tokens=5),
            )
        return LLMResponse(
            content="Task completed with result: validated output and generated summary.",
            tool_calls=(),
            usage=UsageInfo(prompt_tokens=10, completion_tokens=20, total_tokens=30),
        )


class _ToolExecutor:
    async def execute(self, tool_name, tool_input):
        return ToolResult(content=f"Executed {tool_name}", is_error=False)


def _make_skill() -> Skill:
    return Skill(
        skill_id="analysis-skill",
        name="Analysis Skill",
        scope=SkillScope.SYSTEM,
        keywords=(SkillKeyword(keyword="analyze", weight=1.0),),
        strategy=SkillStrategy(mode=StrategyMode.AUTONOMOUS),
        default_skill=True,
    )


def _make_worker() -> Worker:
    return Worker(
        identity=WorkerIdentity(name="Analyst", worker_id="analyst-01"),
        default_skill="analysis-skill",
    )


def _high_conf_rule() -> Rule:
    return Rule(
        rule_id="rule-seeded",
        type="learned",
        category="strategy",
        status="active",
        rule="Step 1 validate inputs. Step 2 compare outputs. Step 3 summarize findings.",
        reason="Seed rule for crystallization",
        scope=RuleScope(skills=("analysis-skill",)),
        source=RuleSource(
            type="self_reflection",
            evidence="seed",
            created_at="2026-04-10T00:00:00+00:00",
        ),
        confidence=0.9,
        apply_count=25,
    )


def _write_persona(worker_dir: Path) -> None:
    worker_dir.mkdir(parents=True, exist_ok=True)
    (worker_dir / "PERSONA.md").write_text(
        "---\nidentity:\n  worker_id: analyst-01\n  name: Analyst\nprinciples:\n  - Be accurate\n---\n",
        encoding="utf-8",
    )


def _duty() -> Duty:
    return Duty(
        duty_id="duty-1",
        title="Daily Quality Check",
        status="active",
        triggers=(DutyTrigger(id="cron-1", type="schedule", cron="0 9 * * *"),),
        execution_policy=ExecutionPolicy(default="standard"),
        action="Analyze the latest quality signals.",
        quality_criteria=("No obvious failures",),
        skill_hint="analysis-skill",
    )


@pytest.mark.asyncio
async def test_learning_loop_end_to_end(tmp_path: Path):
    workspace_root = tmp_path
    tenant = Tenant(
        tenant_id="demo",
        name="Demo",
        trust_level=TrustLevel.STANDARD,
        default_worker="analyst-01",
    )
    tenant_loader = TenantLoader(workspace_root)
    tenant_loader._cache["demo"] = tenant

    llm = _SequencedLLM()
    post_run_handler = build_post_run_handler(
        workspace_root=workspace_root,
        llm_client=llm,
        episode_lock=IndexFileLock(),
    )
    dispatcher = EngineDispatcher(llm_client=llm, tool_executor=_ToolExecutor())
    task_store = TaskStore(workspace_root=workspace_root)
    task_runner = TaskRunner(
        engine_dispatcher=dispatcher,
        task_store=task_store,
        post_run_handler=post_run_handler,
    )
    worker_registry = build_worker_registry(
        [WorkerEntry(worker=_make_worker(), skill_registry=SkillRegistry.from_skills((_make_skill(),)))],
        default_worker_id="analyst-01",
    )
    mcp_server = MCPServer(name="learning-loop")
    router = WorkerRouter(
        worker_registry=worker_registry,
        tenant_loader=tenant_loader,
        task_runner=task_runner,
        mcp_server=mcp_server,
        workspace_root=workspace_root,
    )

    worker_dir = workspace_root / "tenants" / "demo" / "workers" / "analyst-01"
    _write_persona(worker_dir)
    learned_dir = worker_dir / "rules" / "learned"
    learned_dir.mkdir(parents=True, exist_ok=True)
    (learned_dir / "rule-seeded.md").write_text(
        rule_to_markdown(_high_conf_rule()),
        encoding="utf-8",
    )

    async for _ in router.route_stream(
        task="analyze the latest output quality",
        tenant_id="demo",
        worker_id="analyst-01",
    ):
        pass

    memory_dir = worker_dir / "memory"
    rules_dir = worker_dir / "rules"

    indices = load_index(memory_dir)
    assert indices
    assert any("Task completed" in item.summary for item in indices)

    links = load_linkage(memory_dir)
    assert links
    assert links[0].rule_id == "rule-seeded"

    updated_seeded = next(rule for rule in load_rules(rules_dir) if rule.rule_id == "rule-seeded")
    assert updated_seeded.confidence > 0.9

    results = await run_crystallization_cycle(
        rules_dir=rules_dir,
        skills_dir=worker_dir / "skills",
        mcp_server=mcp_server,
        llm_client=llm,
    )
    skill_result = next(result for result in results if result.success and result.target == "skill")
    skill_path = Path(skill_result.artifact_path)
    skill = SkillParser.parse(skill_path)
    assert skill.skill_id.startswith("crystallized-")
    assert skill.source_format == "genworker_v2"

    duty_dir = worker_dir / "duties" / "duty-1"
    duty_dir.mkdir(parents=True, exist_ok=True)
    write_execution_record(
        duty_dir,
        DutyExecutionRecord(
            execution_id="baseline-1",
            duty_id="duty-1",
            trigger_id="cron-1",
            depth="standard",
            executed_at="2026-04-09T00:00:00+00:00",
            duration_seconds=2.0,
            conclusion="completed",
            escalated=False,
        ),
    )

    duty_executor = DutyExecutor(
        worker_router=router,
        execution_log_dir=worker_dir / "duties",
        duty_learning_handler=lambda record, duty: handle_duty_post_execution(
            record=record,
            duty=duty,
            worker_dir=worker_dir,
            llm_client=llm,
            episode_lock=IndexFileLock(),
        ),
    )
    record = await duty_executor.execute(_duty(), _duty().triggers[0], "demo", "analyst-01")
    assert load_recent_records(duty_dir)

    updated_indices = load_index(memory_dir)
    assert any(item.summary.lower().startswith("error") or "validated output" in item.summary.lower() for item in updated_indices)
    episodes = tuple(load_episode(memory_dir, item.id) for item in updated_indices)
    assert any(episode.source.type == "duty_execution" for episode in episodes)
    assert record.duty_id == "duty-1"
