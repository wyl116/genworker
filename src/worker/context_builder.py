"""WorkerContextBuilder - builds WorkerContext from Worker + Tenant + TrustGate."""
from __future__ import annotations

from pathlib import Path

from src.common.tenant import Tenant
from src.engine.state import WorkerContext
from src.tools.mcp.tool import Tool

from .models import Worker
from .trust_gate import WorkerTrustGate


# ---------------------------------------------------------------------------
# Coordinator synthesis instructions (injected when SubAgent tool is available)
# Aligns with Claude Code's Coordinator pattern: LLM must synthesize results
# before acting, never delegate understanding.
# ---------------------------------------------------------------------------

SYNTHESIS_INSTRUCTIONS = """\

## SubAgent Coordination Rules

You have access to the `spawn_subagents` tool for parallel sub-task execution.

### Core Principles
1. **Self-contained prompts**: SubAgents cannot see your conversation history. \
Every subtask description must include all necessary context.
2. **Synthesize before acting**: After receiving SubAgent results, you MUST \
understand them before taking further action. Read the findings, identify \
the key insights, and state what you learned.
3. **Never delegate understanding**: Do not write "based on the subtask results" \
or "as the SubAgent found". Instead, prove you understood by including specific \
details from the results.
4. **Decide next steps**: After synthesis, you may: spawn more SubAgents for \
follow-up work, respond directly with your synthesized findings, or ask for \
clarification if results are ambiguous.

### When to Use SubAgents
- Tasks that can be decomposed into independent parallel work
- Research tasks that benefit from multiple investigation angles
- Tasks that exceed a single execution round's scope

### When NOT to Use SubAgents
- Simple questions you can answer directly
- Tasks that require sequential reasoning with shared context
- Single-step operations"""


def build_worker_context(
    worker: Worker,
    tenant: Tenant,
    trust_gate: WorkerTrustGate,
    available_tools: tuple[Tool, ...],
    available_skill_ids: tuple[str, ...] = (),
    learned_rules: str = "",
    historical_context: str = "",
    task_context: str = "",
    contact_context: str = "",
    directives: str = "",
    subagent_enabled: bool = False,
    memory_orchestrator: object | None = None,
    worker_dir: Path | None = None,
    goal_default_pre_script: object | None = None,
) -> WorkerContext:
    """
    Build a WorkerContext from Worker, Tenant, and TrustGate.

    Args:
        worker: The worker definition.
        tenant: The tenant configuration.
        trust_gate: Computed trust gate decisions.
        available_tools: Tools available after sandbox filtering.
        learned_rules: Raw learned rules text (injected only if trust allows).
        historical_context: Historical context for the worker.
        task_context: Task-specific context.
        directives: Admin directives text.

    Returns:
        Frozen WorkerContext for engine consumption.
    """
    identity_text = _format_identity(worker)
    principles_text = _format_principles(worker)
    constraints_text = _format_constraints(worker)

    # TrustGate controls learned rules injection
    effective_rules = learned_rules if trust_gate.learned_rules_enabled else ""

    # Inject synthesis instructions when SubAgent tool is available
    effective_directives = directives
    if subagent_enabled:
        effective_directives = (
            f"{directives}\n{SYNTHESIS_INSTRUCTIONS}" if directives
            else SYNTHESIS_INSTRUCTIONS
        )

    tool_names = tuple(t.name for t in available_tools)

    return WorkerContext(
        worker_id=worker.worker_id,
        tenant_id=tenant.tenant_id,
        skill_id="",
        identity=identity_text,
        principles=principles_text,
        constraints=constraints_text,
        directives=effective_directives,
        learned_rules=effective_rules,
        historical_context=historical_context,
        task_context=task_context,
        contact_context=contact_context,
        tool_names=tool_names,
        available_skill_ids=available_skill_ids,
        memory_orchestrator=memory_orchestrator,
        worker_dir=str(worker_dir or ""),
        trust_gate=trust_gate,
        goal_default_pre_script=goal_default_pre_script,
    )


def _format_identity(worker: Worker) -> str:
    """Format worker identity for system prompt injection."""
    ident = worker.identity
    parts: list[str] = []

    if ident.name:
        parts.append(f"Name: {ident.name}")
    parts.append(f"Operating mode: {worker.mode.value}")
    if ident.role:
        parts.append(f"Role: {ident.role}")
    if ident.department:
        parts.append(f"Department: {ident.department}")
    if ident.background:
        parts.append(f"Background: {ident.background}")
    if ident.personality.communication_style:
        parts.append(
            f"Communication style: {ident.personality.communication_style}"
        )
    if ident.personality.decision_style:
        parts.append(
            f"Decision style: {ident.personality.decision_style}"
        )
    mode_guidance = _format_mode_guidance(worker)
    if mode_guidance:
        parts.append(mode_guidance)
    service_guidance = _format_service_guidance(worker)
    if service_guidance:
        parts.append(service_guidance)

    if worker.body_instructions:
        parts.append(f"\n{worker.body_instructions}")

    return "\n".join(parts)


def _format_mode_guidance(worker: Worker) -> str:
    """Inject mode-specific operating guidance into the system prompt."""
    if worker.is_personal:
        return (
            "Mode guidance: You are a dedicated personal assistant for a "
            "single owner. Optimize for continuity, personalization, and "
            "proactive support around the owner's goals."
        )
    if worker.is_team_member:
        return (
            "Mode guidance: You are a formal team member with your own "
            "responsibilities. Collaborate as a peer, surface risks and "
            "recommendations proactively, and do not behave like a private assistant."
        )
    if worker.is_service:
        return (
            "Mode guidance: You are a shared service agent. Optimize for "
            "standardized, policy-consistent responses, short interactions, "
            "and explicit escalation when an issue cannot be resolved directly."
        )
    return ""


def _format_service_guidance(worker: Worker) -> str:
    """Inject service-mode operational limits when configured."""
    if not worker.is_service or worker.service_config is None:
        return ""

    service = worker.service_config
    parts = [
        "Service constraints: "
        f"session_ttl={service.session_ttl}s, "
        f"max_concurrent_sessions={service.max_concurrent_sessions}, "
        f"anonymous_allowed={str(service.anonymous_allowed).lower()}."
    ]
    if service.escalation_enabled:
        clause = "Escalation is enabled"
        if service.escalation_target:
            clause += f" to '{service.escalation_target}'"
        if service.escalation_triggers:
            triggers = ", ".join(service.escalation_triggers)
            clause += f"; trigger on: {triggers}"
        parts.append(clause + ".")
    return " ".join(parts)


def _format_principles(worker: Worker) -> str:
    """Format principles as numbered list."""
    if not worker.principles:
        return ""
    lines = [
        f"{i + 1}. {p}" for i, p in enumerate(worker.principles)
    ]
    return "\n".join(lines)


def _format_constraints(worker: Worker) -> str:
    """Format constraints as bullet list."""
    if not worker.constraints:
        return ""
    lines = [f"- {c}" for c in worker.constraints]
    return "\n".join(lines)
