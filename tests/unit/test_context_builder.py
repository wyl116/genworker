# edition: baseline
"""Tests for WorkerContextBuilder."""
from __future__ import annotations

from pathlib import Path

import pytest

from src.common.tenant import Tenant
from src.engine.state import WorkerContext
from src.worker.context_builder import (
    SYNTHESIS_INSTRUCTIONS,
    build_worker_context,
)
from src.worker.trust_gate import WorkerTrustGate


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_worker():
    """Create a minimal Worker-like object for testing."""
    from src.worker.models import Worker, WorkerIdentity, WorkerMode, WorkerPersonality

    return Worker(
        identity=WorkerIdentity(
            name="Test Worker",
            worker_id="w-test",
            role="tester",
            personality=WorkerPersonality(),
            principles=("Be helpful", "Be accurate"),
        ),
        mode=WorkerMode.PERSONAL,
        constraints=("No PII exposure",),
    )


def _make_tenant():
    """Create a minimal Tenant for testing."""
    return Tenant(tenant_id="t-test", name="Test Tenant")


def _make_trust_gate(
    learned_rules: bool = True,
    episodic_write: bool = True,
) -> WorkerTrustGate:
    return WorkerTrustGate(
        bash_enabled=True,
        mcp_remote_enabled=False,
        learned_rules_enabled=learned_rules,
        episodic_write_enabled=episodic_write,
    )


# ---------------------------------------------------------------------------
# Tests: build_worker_context
# ---------------------------------------------------------------------------

class TestBuildWorkerContext:
    def test_basic_context_build(self):
        worker = _make_worker()
        tenant = _make_tenant()
        gate = _make_trust_gate()

        ctx = build_worker_context(
            worker=worker,
            tenant=tenant,
            trust_gate=gate,
            available_tools=(),
        )
        assert isinstance(ctx, WorkerContext)
        assert ctx.worker_id == "w-test"
        assert ctx.tenant_id == "t-test"
        assert ctx.trust_gate == gate
        assert "Test Worker" in ctx.identity
        assert "Operating mode: personal" in ctx.identity

    def test_learned_rules_injected_when_trusted(self):
        worker = _make_worker()
        tenant = _make_tenant()
        gate = _make_trust_gate(learned_rules=True)

        ctx = build_worker_context(
            worker=worker,
            tenant=tenant,
            trust_gate=gate,
            available_tools=(),
            learned_rules="Always verify inputs",
        )
        assert ctx.learned_rules == "Always verify inputs"

    def test_learned_rules_blocked_when_untrusted(self):
        worker = _make_worker()
        tenant = _make_tenant()
        gate = _make_trust_gate(learned_rules=False)

        ctx = build_worker_context(
            worker=worker,
            tenant=tenant,
            trust_gate=gate,
            available_tools=(),
            learned_rules="Should be blocked",
        )
        assert ctx.learned_rules == ""

    def test_historical_context_passed_through(self):
        worker = _make_worker()
        tenant = _make_tenant()
        gate = _make_trust_gate()

        ctx = build_worker_context(
            worker=worker,
            tenant=tenant,
            trust_gate=gate,
            available_tools=(),
            historical_context="Previous episode data",
        )
        assert ctx.historical_context == "Previous episode data"

    def test_synthesis_instructions_injected_when_subagent_enabled(self):
        worker = _make_worker()
        tenant = _make_tenant()
        gate = _make_trust_gate()

        ctx = build_worker_context(
            worker=worker,
            tenant=tenant,
            trust_gate=gate,
            available_tools=(),
            subagent_enabled=True,
        )
        assert "spawn_subagents" in ctx.directives
        assert "Synthesize before acting" in ctx.directives
        assert "Never delegate understanding" in ctx.directives

    def test_synthesis_instructions_not_injected_by_default(self):
        worker = _make_worker()
        tenant = _make_tenant()
        gate = _make_trust_gate()

        ctx = build_worker_context(
            worker=worker,
            tenant=tenant,
            trust_gate=gate,
            available_tools=(),
        )
        assert "spawn_subagents" not in ctx.directives

    def test_synthesis_instructions_appended_to_existing_directives(self):
        worker = _make_worker()
        tenant = _make_tenant()
        gate = _make_trust_gate()

        ctx = build_worker_context(
            worker=worker,
            tenant=tenant,
            trust_gate=gate,
            available_tools=(),
            directives="Follow safety guidelines.",
            subagent_enabled=True,
        )
        assert "Follow safety guidelines." in ctx.directives
        assert "spawn_subagents" in ctx.directives

    def test_team_member_mode_injects_peer_guidance(self):
        from src.worker.models import WorkerMode

        worker = _make_worker()
        worker = worker.__class__(
            identity=worker.identity,
            mode=WorkerMode.TEAM_MEMBER,
            service_config=worker.service_config,
            tool_policy=worker.tool_policy,
            skills_dir=worker.skills_dir,
            default_skill=worker.default_skill,
            constraints=worker.constraints,
            triggers=worker.triggers,
            sensor_configs=worker.sensor_configs,
            configured_contacts=worker.configured_contacts,
            contacts_config=worker.contacts_config,
            body_instructions=worker.body_instructions,
            source_path=worker.source_path,
        )
        tenant = _make_tenant()
        gate = _make_trust_gate()

        ctx = build_worker_context(
            worker=worker,
            tenant=tenant,
            trust_gate=gate,
            available_tools=(),
        )
        assert "formal team member" in ctx.identity
        assert "private assistant" in ctx.identity

    def test_service_mode_injects_service_guidance(self):
        from src.worker.models import ServiceConfig, WorkerMode

        worker = _make_worker()
        worker = worker.__class__(
            identity=worker.identity,
            mode=WorkerMode.SERVICE,
            service_config=ServiceConfig(
                session_ttl=900,
                max_concurrent_sessions=80,
                anonymous_allowed=False,
                escalation_enabled=True,
                escalation_target="human_support",
                escalation_triggers=("连续3轮未解决",),
            ),
            tool_policy=worker.tool_policy,
            skills_dir=worker.skills_dir,
            default_skill=worker.default_skill,
            constraints=worker.constraints,
            triggers=worker.triggers,
            sensor_configs=worker.sensor_configs,
            configured_contacts=worker.configured_contacts,
            contacts_config=worker.contacts_config,
            body_instructions=worker.body_instructions,
            source_path=worker.source_path,
        )
        tenant = _make_tenant()
        gate = _make_trust_gate()

        ctx = build_worker_context(
            worker=worker,
            tenant=tenant,
            trust_gate=gate,
            available_tools=(),
        )
        assert "shared service agent" in ctx.identity
        assert "session_ttl=900s" in ctx.identity
        assert "human_support" in ctx.identity
