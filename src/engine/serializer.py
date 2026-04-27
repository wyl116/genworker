"""Serialization helpers for checkpoint snapshots."""
from __future__ import annotations

from src.engine.state import StepResult, WorkerContext
from src.worker.trust_gate import WorkerTrustGate
from src.worker.scripts.models import deserialize_pre_script, serialize_pre_script


def serialize_step_result(result: StepResult) -> dict:
    return {
        "step_name": result.step_name,
        "step_type": result.step_type,
        "content": result.content,
        "structured_data": result.structured_data,
        "success": result.success,
        "error": result.error,
    }


def deserialize_step_result(data: dict) -> StepResult:
    return StepResult(
        step_name=str(data.get("step_name", "")),
        step_type=str(data.get("step_type", "")),
        content=str(data.get("content", "")),
        structured_data=data.get("structured_data"),
        success=bool(data.get("success", True)),
        error=data.get("error"),
    )


def serialize_worker_context(ctx: WorkerContext) -> dict:
    trust_gate = getattr(ctx, "trust_gate", None)
    return {
        "worker_id": ctx.worker_id,
        "tenant_id": ctx.tenant_id,
        "skill_id": ctx.skill_id,
        "identity": ctx.identity,
        "principles": ctx.principles,
        "constraints": ctx.constraints,
        "directives": ctx.directives,
        "learned_rules": ctx.learned_rules,
        "historical_context": ctx.historical_context,
        "task_context": ctx.task_context,
        "contact_context": ctx.contact_context,
        "tool_names": list(ctx.tool_names),
        "worker_dir": ctx.worker_dir,
        "goal_default_pre_script": serialize_pre_script(
            getattr(ctx, "goal_default_pre_script", None)
        ),
        "trust_gate": (
            {
                "trusted": bool(getattr(trust_gate, "trusted", False)),
                "bash_enabled": bool(getattr(trust_gate, "bash_enabled", False)),
                "mcp_remote_enabled": bool(getattr(trust_gate, "mcp_remote_enabled", False)),
                "learned_rules_enabled": bool(getattr(trust_gate, "learned_rules_enabled", False)),
                "episodic_write_enabled": bool(getattr(trust_gate, "episodic_write_enabled", False)),
                "cross_worker_sharing_enabled": bool(
                    getattr(trust_gate, "cross_worker_sharing_enabled", False),
                ),
                "semantic_search_enabled": bool(
                    getattr(trust_gate, "semantic_search_enabled", False),
                ),
            }
            if trust_gate is not None else None
        ),
    }


def deserialize_worker_context(data: dict) -> WorkerContext:
    trust_gate_data = data.get("trust_gate")
    return WorkerContext(
        worker_id=str(data.get("worker_id", "")),
        tenant_id=str(data.get("tenant_id", "")),
        skill_id=str(data.get("skill_id", "")),
        identity=str(data.get("identity", "")),
        principles=str(data.get("principles", "")),
        constraints=str(data.get("constraints", "")),
        directives=str(data.get("directives", "")),
        learned_rules=str(data.get("learned_rules", "")),
        historical_context=str(data.get("historical_context", "")),
        task_context=str(data.get("task_context", "")),
        contact_context=str(data.get("contact_context", "")),
        tool_names=tuple(data.get("tool_names", ()) or ()),
        worker_dir=str(data.get("worker_dir", "")),
        goal_default_pre_script=deserialize_pre_script(
            data.get("goal_default_pre_script")
        ),
        trust_gate=(
            WorkerTrustGate(
                trusted=bool(trust_gate_data.get("trusted", False)),
                bash_enabled=bool(trust_gate_data.get("bash_enabled", False)),
                mcp_remote_enabled=bool(trust_gate_data.get("mcp_remote_enabled", False)),
                learned_rules_enabled=bool(trust_gate_data.get("learned_rules_enabled", False)),
                episodic_write_enabled=bool(trust_gate_data.get("episodic_write_enabled", False)),
                cross_worker_sharing_enabled=bool(
                    trust_gate_data.get("cross_worker_sharing_enabled", False),
                ),
                semantic_search_enabled=bool(
                    trust_gate_data.get("semantic_search_enabled", False),
                ),
            )
            if isinstance(trust_gate_data, dict) else None
        ),
    )
