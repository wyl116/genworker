# edition: baseline
"""
Unit tests for WorkerTrustGate computation.

Tests trust levels:
- basic: all high-risk disabled
- standard: bash + learned rules enabled
- elevated: remote MCP enabled
- full: everything enabled
- bash denied by worker policy
"""
import dataclasses

import pytest

from src.common.tenant import Tenant, TenantToolPolicy, TrustLevel
from src.worker.models import Worker, WorkerIdentity, WorkerToolPolicy
from src.worker.trust_gate import WorkerTrustGate, compute_trust_gate


def _make_worker(
    worker_id: str = "test-worker",
    denied_tools: frozenset[str] = frozenset(),
) -> Worker:
    """Create a minimal Worker for testing."""
    return Worker(
        identity=WorkerIdentity(
            name="Test",
            worker_id=worker_id,
        ),
        tool_policy=WorkerToolPolicy(
            mode="blacklist",
            denied_tools=denied_tools,
        ),
    )


def _make_tenant(
    trust_level: TrustLevel = TrustLevel.BASIC,
    mcp_remote_allowed: bool = False,
) -> Tenant:
    """Create a minimal Tenant for testing."""
    return Tenant(
        tenant_id="test-tenant",
        name="Test Tenant",
        trust_level=trust_level,
        mcp_remote_allowed=mcp_remote_allowed,
    )


class TestTrustGateComputation:
    """Tests for compute_trust_gate function."""

    def test_basic_trust_disables_all_high_risk(self) -> None:
        """BASIC trust level disables all high-risk subsystems."""
        worker = _make_worker()
        tenant = _make_tenant(trust_level=TrustLevel.BASIC)

        gate = compute_trust_gate(worker, tenant)

        assert gate.trusted is False
        assert gate.bash_enabled is False
        assert gate.mcp_remote_enabled is False
        assert gate.learned_rules_enabled is False
        assert gate.episodic_write_enabled is False
        assert gate.cross_worker_sharing_enabled is False

    def test_standard_trust_enables_bash_and_rules(self) -> None:
        """STANDARD trust enables bash, learned rules, and episodic write."""
        worker = _make_worker()
        tenant = _make_tenant(trust_level=TrustLevel.STANDARD)

        gate = compute_trust_gate(worker, tenant)

        assert gate.trusted is True
        assert gate.bash_enabled is True
        assert gate.mcp_remote_enabled is False
        assert gate.learned_rules_enabled is True
        assert gate.episodic_write_enabled is True
        assert gate.cross_worker_sharing_enabled is True

    def test_elevated_trust_enables_remote_mcp(self) -> None:
        """ELEVATED trust with mcp_remote_allowed enables remote MCP."""
        worker = _make_worker()
        tenant = _make_tenant(
            trust_level=TrustLevel.ELEVATED,
            mcp_remote_allowed=True,
        )

        gate = compute_trust_gate(worker, tenant)

        assert gate.trusted is True
        assert gate.bash_enabled is True
        assert gate.mcp_remote_enabled is True
        assert gate.learned_rules_enabled is True
        assert gate.episodic_write_enabled is True

    def test_elevated_without_mcp_allowed_disables_remote_mcp(self) -> None:
        """ELEVATED trust without mcp_remote_allowed keeps MCP disabled."""
        worker = _make_worker()
        tenant = _make_tenant(
            trust_level=TrustLevel.ELEVATED,
            mcp_remote_allowed=False,
        )

        gate = compute_trust_gate(worker, tenant)

        assert gate.mcp_remote_enabled is False

    def test_full_trust_enables_everything(self) -> None:
        """FULL trust with mcp_remote_allowed enables everything."""
        worker = _make_worker()
        tenant = _make_tenant(
            trust_level=TrustLevel.FULL,
            mcp_remote_allowed=True,
        )

        gate = compute_trust_gate(worker, tenant)

        assert gate.trusted is True
        assert gate.bash_enabled is True
        assert gate.mcp_remote_enabled is True
        assert gate.learned_rules_enabled is True
        assert gate.episodic_write_enabled is True

    def test_bash_denied_by_worker_policy(self) -> None:
        """Worker denying bash_tool overrides trust level."""
        worker = _make_worker(denied_tools=frozenset({"bash_tool"}))
        tenant = _make_tenant(trust_level=TrustLevel.STANDARD)

        gate = compute_trust_gate(worker, tenant)

        assert gate.trusted is True
        assert gate.bash_enabled is False  # denied by worker policy
        assert gate.learned_rules_enabled is True

    def test_trust_gate_is_frozen(self) -> None:
        """WorkerTrustGate is immutable."""
        gate = WorkerTrustGate(trusted=True, bash_enabled=True)

        with pytest.raises(dataclasses.FrozenInstanceError):
            gate.bash_enabled = False  # type: ignore[misc]

    def test_trust_gate_replace(self) -> None:
        """Mutations happen via dataclasses.replace()."""
        gate = WorkerTrustGate(trusted=True, bash_enabled=True)
        new_gate = dataclasses.replace(gate, bash_enabled=False)

        assert gate.bash_enabled is True
        assert new_gate.bash_enabled is False
