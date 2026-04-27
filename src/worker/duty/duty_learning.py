"""Duty post-execution learning hook."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from src.common.content_scanner import scan
from src.memory.orchestrator import MemoryWriteEvent
from src.memory.episodic.models import Episode, EpisodeSource
from src.memory.episodic.store import write_episode, write_episode_with_index
from src.memory.write_models import EpisodeWritePayload
from src.worker.rules.models import RuleCandidate, RuleScope, RuleSource
from src.worker.rules.rule_generator import (
    extract_rule_from_reflection,
    validate_and_create_rule,
)
from src.worker.rules.rule_manager import load_rules

from .models import Duty, DutyExecutionRecord


async def _emit_memory_write_best_effort(memory_orchestrator, event) -> None:
    from src.common.logger import get_logger

    logger = get_logger()
    if memory_orchestrator is None:
        return
    try:
        await memory_orchestrator.on_memory_write(event)
    except Exception as exc:
        logger.warning("[DutyLearning] memory mirror failed for %s: %s", event.entity_id, exc)


def _orchestrator_can_mirror_episode(
    memory_orchestrator,
    *,
    tenant_id: str,
    worker_id: str,
    episode: Episode,
) -> bool:
    if memory_orchestrator is None:
        return False

    probe_event = MemoryWriteEvent(
        action="create",
        target="episode",
        entity_id=episode.episode_id,
        content=EpisodeWritePayload(
            tenant_id=tenant_id,
            worker_id=worker_id,
            episode=episode,
        ),
        source_subsystem="episodic",
        occurred_at=episode.created_at,
    )
    providers = tuple(getattr(memory_orchestrator, "providers", ()) or ())
    return any(provider.accepts(probe_event) for provider in providers)


async def handle_duty_post_execution(
    record: DutyExecutionRecord,
    duty: Duty,
    worker_dir: Path,
    llm_client: object | None,
    episode_lock: object,
    memory_orchestrator: object | None = None,
    openviking_client: object | None = None,
    openviking_scope_prefix: str = "viking://",
    trust_gate: object | None = None,
) -> None:
    """Create a duty episode and optionally extract a duty-specific rule."""
    memory_dir = worker_dir / "memory"
    rules_dir = worker_dir / "rules"
    preferred_skill_id = duty.preferred_skill_id or (
        duty.soft_preferred_skill_ids[0] if duty.soft_preferred_skill_ids else None
    )
    episode = Episode(
        episode_id=f"ep-{uuid4().hex[:8]}",
        created_at=datetime.now(timezone.utc).isoformat(),
        source=EpisodeSource(
            type="duty_execution",
            skill_used=preferred_skill_id or "duty",
            trigger=f"duty:{duty.duty_id}",
        ),
        summary=record.conclusion[:200] or duty.title,
        key_findings=record.anomalies_found,
        related_entities=(),
        related_duties=(duty.duty_id,),
    )
    if bool(getattr(trust_gate, "episodic_write_enabled", True)):
        route_remote_index_via_orchestrator = _orchestrator_can_mirror_episode(
            memory_orchestrator,
            tenant_id=worker_dir.parent.parent.name,
            worker_id=worker_dir.name,
            episode=episode,
        )

        async with episode_lock:
            if route_remote_index_via_orchestrator:
                write_episode(memory_dir, episode)
            else:
                from src.memory.backends.openviking import build_episodic_indexer

                await write_episode_with_index(
                    memory_dir,
                    episode,
                    viking_indexer=build_episodic_indexer(
                        openviking_client,
                        scope_prefix=openviking_scope_prefix,
                        tenant_id=worker_dir.parent.parent.name,
                        worker_id=worker_dir.name,
                    ),
                )

        if memory_orchestrator is not None and hasattr(memory_orchestrator, "on_memory_write"):
            await _emit_memory_write_best_effort(memory_orchestrator, MemoryWriteEvent(
                action="create",
                target="episode",
                entity_id=episode.episode_id,
                content=EpisodeWritePayload(
                    tenant_id=worker_dir.parent.parent.name,
                    worker_id=worker_dir.name,
                    episode=episode,
                ),
                source_subsystem="episodic",
                occurred_at=episode.created_at,
            ))

    if llm_client is None or not (record.anomalies_found or record.escalated):
        return

    candidate = await extract_rule_from_reflection(
        execution_summary=record.conclusion,
        outcome_quality="failure" if record.escalated else "success",
        llm_client=llm_client,
    )
    if candidate is None:
        candidate = RuleCandidate(
            rule=f"当执行 Duty {duty.title} 时，优先检查异常模式并记录结论",
            reason=record.conclusion[:300],
            category="strategy",
            scope=RuleScope(
                skills=duty.soft_preferred_skill_ids
                or ((preferred_skill_id,) if preferred_skill_id else ())
            ),
            source=RuleSource(
                type="self_reflection",
                evidence=record.conclusion,
                created_at=episode.created_at,
            ),
        )

    if not scan(f"{candidate.rule}\n{candidate.reason}").is_safe:
        return

    await validate_and_create_rule(
        candidate=candidate,
        rules_dir=rules_dir,
        principles=(),
        existing_rules=load_rules(rules_dir),
    )
