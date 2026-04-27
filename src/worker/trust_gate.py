"""
WorkerTrustGate - trust-gated initialization decisions.

Computed once during bootstrap from Tenant + Worker configuration.
Subsystems consume TrustGate boolean fields to decide feature enablement.

Trust levels:
  basic:    all high-risk subsystems disabled
  standard: bash + learned rules + episodic write enabled
  elevated: + remote MCP discovery enabled
  full:     everything enabled
"""
from dataclasses import dataclass

from src.common.tenant import Tenant, TrustLevel

from .models import Worker


@dataclass(frozen=True)
class WorkerTrustGate:
    """
    Immutable trust gate decisions.

    Each boolean field is consumed by a specific subsystem at a specific time.
    See design doc 5.12 for the consumption matrix.
    """
    trusted: bool = False
    bash_enabled: bool = False
    mcp_remote_enabled: bool = False
    learned_rules_enabled: bool = False
    episodic_write_enabled: bool = False
    cross_worker_sharing_enabled: bool = False
    semantic_search_enabled: bool = False


def compute_trust_gate(worker: Worker, tenant: Tenant) -> WorkerTrustGate:
    """
    Compute trust gate from Worker and Tenant configuration.

    Args:
        worker: The worker definition.
        tenant: The tenant configuration.

    Returns:
        Frozen WorkerTrustGate with computed boolean fields.
    """
    trust = tenant.trust_level

    # basic: everything off
    if trust < TrustLevel.STANDARD:
        return WorkerTrustGate(
            trusted=False,
            bash_enabled=False,
            mcp_remote_enabled=False,
            learned_rules_enabled=False,
            episodic_write_enabled=False,
            cross_worker_sharing_enabled=False,
            semantic_search_enabled=False,
        )

    # standard: bash + rules + episodic write (if not denied by worker policy)
    bash_ok = (
        "bash_tool" not in worker.tool_policy.denied_tools
        and "bash_execute" not in worker.tool_policy.denied_tools
    )

    # elevated+: remote MCP also enabled
    mcp_remote_ok = (
        trust >= TrustLevel.ELEVATED
        and tenant.mcp_remote_allowed
    )

    return WorkerTrustGate(
        trusted=True,
        bash_enabled=bash_ok,
        mcp_remote_enabled=mcp_remote_ok,
        learned_rules_enabled=True,
        episodic_write_enabled=True,
        cross_worker_sharing_enabled=True,
        semantic_search_enabled=True,
    )
