"""
Worker-level tool sandbox computation.

Combines Worker.tool_policy + Tenant.tool_policy, then delegates to
tools/sandbox.filter_tools() pure function for final filtering.

Also consumes TrustGate.bash_enabled to conditionally deny bash_execute.
"""
from src.common.tenant import Tenant
from src.tools.mcp.tool import Tool
from src.tools.sandbox import ToolPolicy, TenantPolicy, filter_tools

from .models import Worker, WorkerToolPolicy
from .trust_gate import WorkerTrustGate


def compute_available_tools(
    worker: Worker,
    tenant: Tenant,
    trust_gate: WorkerTrustGate,
    all_tools: tuple[Tool, ...],
) -> tuple[Tool, ...]:
    """
    Compute the final set of available tools for a worker.

    Combines:
    1. Worker.tool_policy (blacklist/whitelist)
    2. TrustGate.bash_enabled (deny bash_execute if False)
    3. Tenant.tool_policy (security overlay)

    Then delegates to tools/sandbox.filter_tools() pure function.

    Args:
        worker: The worker definition.
        tenant: The tenant configuration.
        trust_gate: Computed trust gate decisions.
        all_tools: All registered tools.

    Returns:
        Tuple of allowed Tool objects.
    """
    worker_policy = _build_tool_policy(worker.tool_policy, trust_gate)
    tenant_policy = _build_tenant_policy(tenant)

    return filter_tools(
        all_tools=all_tools,
        policy=worker_policy,
        tenant_policy=tenant_policy,
    )


def _build_tool_policy(
    worker_tool_policy: WorkerToolPolicy,
    trust_gate: WorkerTrustGate,
) -> ToolPolicy:
    """
    Build a ToolPolicy from worker policy + trust gate.

    If bash is not enabled by trust gate, adds bash_execute to denied set.
    """
    denied = worker_tool_policy.denied_tools
    if not trust_gate.bash_enabled:
        denied = denied | frozenset({"bash_tool", "bash_execute"})

    return ToolPolicy(
        mode=worker_tool_policy.mode,
        denied_tools=denied,
        allowed_tools=worker_tool_policy.allowed_tools,
    )


def _build_tenant_policy(tenant: Tenant) -> TenantPolicy:
    """Build a TenantPolicy from tenant configuration."""
    return TenantPolicy(denied_tools=tenant.tool_policy.denied_tools)
