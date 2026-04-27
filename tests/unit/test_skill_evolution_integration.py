# edition: baseline
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.channels.commands.builtin import build_builtin_command_registry
from src.channels.commands.models import CommandContext
from src.channels.models import ChannelInboundMessage, build_channel_binding
from src.common.tenant import Tenant, TrustLevel
from src.worker.duty.execution_log import write_execution_record
from src.worker.duty.models import DutyExecutionRecord
from src.worker.lifecycle.duty_builder import build_duty_from_payload, write_duty_md
from src.worker.lifecycle.duty_skill_detector import DutySkillDetector
from src.worker.lifecycle.feedback_store import FeedbackStore
from src.worker.lifecycle.suggestion_store import SuggestionStore
from src.worker.rules.crystallizer import run_crystallization_cycle
from src.worker.rules.models import Rule, RuleScope, RuleSource, rule_to_markdown
from src.worker.rules.rule_manager import load_rules


class _StubSessionManager:
    def __init__(self) -> None:
        self.session = SimpleNamespace(messages=(), session_id="session-1", thread_id="im:feishu:oc_123")

    async def find_by_thread(self, thread_id: str):
        return self.session

    async def reset_thread(self, thread_id: str):
        return None


def _ctx(tmp_path: Path, *, argv=(), suggestion_store=None, feedback_store=None) -> CommandContext:
    binding = build_channel_binding(
        {"type": "feishu", "connection_mode": "webhook", "chat_ids": ["oc_123"]},
        tenant_id="tenant-1",
        worker_id="worker-1",
    )
    return CommandContext(
        message=ChannelInboundMessage(
            message_id="msg-1",
            channel_type="feishu",
            chat_id="oc_123",
            sender_id="user-1",
            content="",
        ),
        binding=binding,
        tenant=Tenant(tenant_id="tenant-1", name="Tenant", trust_level=TrustLevel.STANDARD),
        args={"argv": tuple(argv), "raw_args": " ".join(argv)},
        session_manager=_StubSessionManager(),
        thread_id="im:feishu:oc_123",
        registry=build_builtin_command_registry(),
        suggestion_store=suggestion_store,
        feedback_store=feedback_store,
        workspace_root=tmp_path,
    )


@pytest.mark.asyncio
async def test_duty_to_skill_full_chain(tmp_path: Path):
    suggestion_store = SuggestionStore(tmp_path)
    feedback_store = FeedbackStore(tmp_path)
    duties_dir = tmp_path / "tenants" / "tenant-1" / "workers" / "worker-1" / "duties"
    duty = build_duty_from_payload(
        {
            "duty_id": "duty-1",
            "title": "每周周报汇总",
            "schedule": "0 9 * * 1",
            "action": "收集销售数据并输出周报摘要",
            "quality_criteria": ["完整", "准确"],
        }
    )
    write_duty_md(duty, duties_dir, filename="weekly-report.md")
    duty_log_dir = duties_dir / duty.duty_id
    for index in range(10):
        write_execution_record(
            duty_log_dir,
            DutyExecutionRecord(
                execution_id=f"exec-{index}",
                duty_id="duty-1",
                trigger_id="schedule-1",
                depth="standard",
                executed_at=f"2026-04-{index + 1:02d}T09:00:00+00:00",
                duration_seconds=2.0,
                conclusion="completed",
                escalated=False,
            ),
        )

    detector = DutySkillDetector(suggestion_store=suggestion_store)
    created = detector.detect(
        tenant_id="tenant-1",
        worker_id="worker-1",
        duties_dir=duties_dir,
    )

    assert len(created) == 1
    suggestion_id = created[0].suggestion_id
    reply = await build_builtin_command_registry().resolve("approve_suggestion").handler(
        _ctx(
            tmp_path,
            argv=(suggestion_id,),
            suggestion_store=suggestion_store,
            feedback_store=feedback_store,
        )
    )

    assert "已创建 Skill" in reply.text
    skill_id = created[0].payload_dict["skill_id"]
    skill_file = (
        tmp_path / "tenants" / "tenant-1" / "workers" / "worker-1"
        / "skills" / skill_id / "SKILL.md"
    )
    assert skill_file.exists()
    updated_duty = (duties_dir / "weekly-report.md").read_text(encoding="utf-8")
    assert f"skill_id: {skill_id}" in updated_duty
    resolved = suggestion_store.get("tenant-1", "worker-1", suggestion_id)
    assert resolved is not None
    assert resolved.status == "approved"


@pytest.mark.asyncio
async def test_rule_to_skill_full_chain(tmp_path: Path):
    suggestion_store = SuggestionStore(tmp_path)
    feedback_store = FeedbackStore(tmp_path)
    rules_dir = tmp_path / "tenants" / "tenant-1" / "workers" / "worker-1" / "rules"
    learned_dir = rules_dir / "learned"
    learned_dir.mkdir(parents=True, exist_ok=True)
    (learned_dir / "rule-1.md").write_text(
        rule_to_markdown(
            Rule(
                rule_id="rule-1",
                type="learned",
                category="strategy",
                status="active",
                rule="Step 1 collect data. Step 2 summarize findings.",
                reason="stable",
                scope=RuleScope(),
                source=RuleSource(
                    type="self_reflection",
                    evidence="summary",
                    created_at="2026-04-17T00:00:00+00:00",
                ),
                confidence=0.95,
                apply_count=30,
            )
        ),
        encoding="utf-8",
    )

    results = await run_crystallization_cycle(
        rules_dir=rules_dir,
        skills_dir=tmp_path / "tenants" / "tenant-1" / "workers" / "worker-1" / "skills",
        mcp_server=None,
        llm_client=None,
        suggestion_store=suggestion_store,
        tenant_id="tenant-1",
        worker_id="worker-1",
    )

    assert len(results) == 1
    suggestion_id = results[0].artifact_ref
    pending = suggestion_store.list_pending("tenant-1", "worker-1")
    assert len(pending) == 1
    skill_id = pending[0].payload_dict["skill_id"]
    reply = await build_builtin_command_registry().resolve("approve_suggestion").handler(
        _ctx(
            tmp_path,
            argv=(suggestion_id,),
            suggestion_store=suggestion_store,
            feedback_store=feedback_store,
        )
    )

    assert f"已创建 Skill '{skill_id}'" in reply.text
    skill_file = (
        tmp_path / "tenants" / "tenant-1" / "workers" / "worker-1"
        / "skills" / skill_id / "SKILL.md"
    )
    assert skill_file.exists()
    loaded_rule = next(rule for rule in load_rules(rules_dir) if rule.rule_id == "rule-1")
    assert loaded_rule.status == "crystallized"
