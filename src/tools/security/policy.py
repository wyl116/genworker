"""Request-scoped tool policy evaluation."""
from __future__ import annotations

from src.tools.security.models import PolicyDecision, PolicyResult


class PolicyEvaluator:
    """Evaluate whether a scoped tool call should be allowed."""

    def __init__(self, require_scope: bool = False) -> None:
        self._require_scope = require_scope

    def evaluate(self, ctx) -> PolicyResult:
        scope = getattr(ctx, "execution_scope", None)
        if scope is None:
            if str(ctx.tool_name).startswith("email_"):
                return PolicyResult(decision=PolicyDecision.ALLOW)
            if not self._require_scope:
                return PolicyResult(decision=PolicyDecision.ALLOW)
            return PolicyResult(
                decision=PolicyDecision.DENY,
                reason="Tool execution denied: missing execution scope",
            )
        if ctx.tool is None:
            return PolicyResult(
                decision=PolicyDecision.DENY,
                reason=f"Tool '{ctx.tool_name}' is not available in the current scope",
            )
        if ctx.tool_name not in scope.allowed_tool_names:
            return PolicyResult(
                decision=PolicyDecision.DENY,
                reason=f"Tool '{ctx.tool_name}' is not allowed for this run",
            )

        trust_gate = getattr(scope, "trust_gate", None)
        if ctx.tool_name == "bash_execute" and not getattr(trust_gate, "bash_enabled", False):
            return PolicyResult(
                decision=PolicyDecision.DENY,
                reason="Tool 'bash_execute' is disabled by trust gate",
            )
        if ctx.tool_name == "session_search" and not getattr(trust_gate, "semantic_search_enabled", False):
            return PolicyResult(
                decision=PolicyDecision.DENY,
                reason="Tool 'session_search' is disabled by trust gate",
            )

        if str(ctx.risk_level) == "critical":
            return PolicyResult(
                decision=PolicyDecision.NEEDS_APPROVAL,
                reason=(
                    f"Tool '{ctx.tool_name}' requires approval, "
                    "but online approval is not implemented"
                ),
            )

        return PolicyResult(decision=PolicyDecision.ALLOW)
