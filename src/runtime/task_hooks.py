"""Task lifecycle hooks used by bootstrap wiring."""
from __future__ import annotations


async def _emit_memory_write_best_effort(memory_orchestrator, event) -> None:
    from src.common.logger import get_logger

    logger = get_logger()
    if memory_orchestrator is None:
        return
    try:
        await memory_orchestrator.on_memory_write(event)
    except Exception as exc:
        logger.warning("[TaskHooks] memory mirror failed for %s: %s", event.entity_id, exc)


def _episodic_write_allowed(trust_gate) -> bool:
    """Keep episodic persistence aligned with WorkerTrustGate semantics."""
    if trust_gate is None:
        return True
    return bool(getattr(trust_gate, "episodic_write_enabled", True))


def _orchestrator_can_mirror_episode(
    memory_orchestrator,
    *,
    tenant_id: str,
    worker_id: str,
    episode,
) -> bool:
    if memory_orchestrator is None:
        return False

    from src.memory.orchestrator import MemoryWriteEvent
    from src.memory.write_models import EpisodeWritePayload

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


async def _persist_episode(
    *,
    memory_dir,
    episode,
    episode_lock,
    tenant_id: str,
    worker_id: str,
    memory_orchestrator=None,
    openviking_client=None,
    openviking_scope_prefix: str = "viking://",
) -> None:
    """Persist local episodic memory and mirror remote indexing best-effort."""
    from src.memory.backends.openviking import build_episodic_indexer
    from src.memory.orchestrator import MemoryWriteEvent
    from src.memory.episodic.store import write_episode, write_episode_with_index
    from src.memory.write_models import EpisodeWritePayload

    route_remote_index_via_orchestrator = _orchestrator_can_mirror_episode(
        memory_orchestrator,
        tenant_id=tenant_id,
        worker_id=worker_id,
        episode=episode,
    )

    async with episode_lock:
        if route_remote_index_via_orchestrator:
            write_episode(memory_dir, episode)
        else:
            await write_episode_with_index(
                memory_dir,
                episode,
                viking_indexer=build_episodic_indexer(
                    openviking_client,
                    scope_prefix=openviking_scope_prefix,
                    tenant_id=tenant_id,
                    worker_id=worker_id,
                ),
            )

    if memory_orchestrator is not None:
        await _emit_memory_write_best_effort(memory_orchestrator, MemoryWriteEvent(
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
        ))


def build_post_run_handler(
    workspace_root,
    llm_client,
    episode_lock,
    memory_orchestrator=None,
    openviking_client=None,
    openviking_scope_prefix: str = "viking://",
    suggestion_store=None,
    goal_lock_registry=None,
):
    """Build the post-run persistence hook."""
    from datetime import datetime, timezone
    import json
    from pathlib import Path
    from uuid import uuid4

    from src.memory.orchestrator import MemoryWriteEvent
    from src.memory.episodic.linkage import (
        apply_outcome_feedback,
        create_links,
        write_linkage,
    )
    from src.memory.episodic.decay import (
        identify_archive_candidates,
    )
    from src.memory.episodic.models import Episode, EpisodeSource, RelatedEntity
    from src.memory.episodic.store import load_index
    from src.memory.preferences.extractor import (
        extract_decisions,
        extract_preferences,
        load_decisions,
        load_preferences,
        merge_preferences,
        save_decisions,
        save_preferences,
        supersede_decisions,
    )
    from src.memory.write_models import DecisionWritePayload, PreferenceWritePayload
    from src.worker.archive.archive_manager import ArchiveManager, ArchiveMetadata
    from src.worker.goal.planner import goal_to_duty
    from src.worker.lifecycle.goal_projector import project_task_outcome_to_goal
    from src.worker.lifecycle.models import SuggestionRecord, add_days_iso
    from src.worker.parser import parse_persona_md
    from src.worker.rules.models import RuleCandidate, RuleScope, RuleSource
    from src.worker.rules.rule_generator import (
        extract_rule_from_reflection,
        validate_and_create_rule,
    )
    from src.worker.rules.rule_manager import load_rules

    async def _handle_post_run(manifest, worker_context, extraction):
        worker_dir = (
            Path(workspace_root) / "tenants" / worker_context.tenant_id
            / "workers" / worker_context.worker_id
        )
        memory_dir = worker_dir / "memory"
        rules_dir = worker_dir / "rules"
        is_suggestion_preview = (
            str(getattr(manifest.provenance, "source_type", "") or "") == "suggestion_preview"
        )

        if extraction.episode_summary and _episodic_write_allowed(
            getattr(worker_context, "trust_gate", None),
        ):
            channel_entities: list[RelatedEntity] = []
            task_ctx = getattr(worker_context, "task_context", "") or ""
            if "channel_type:email" in task_ctx:
                channel_entities.append(RelatedEntity(type="channel", value="email"))
                for part in task_ctx.split(", "):
                    if part.startswith("subject:"):
                        subject = part[len("subject:"):].strip()
                        if subject:
                            channel_entities.append(
                                RelatedEntity(type="email_subject", value=subject)
                            )
            if getattr(manifest.provenance, "goal_task_id", ""):
                channel_entities.append(
                    RelatedEntity(
                        type="goal_task",
                        value=manifest.provenance.goal_task_id,
                    )
                )
            if getattr(manifest.provenance, "suggestion_id", ""):
                channel_entities.append(
                    RelatedEntity(
                        type="suggestion",
                        value=manifest.provenance.suggestion_id,
                    )
                )
            if getattr(manifest.provenance, "parent_task_id", ""):
                channel_entities.append(
                    RelatedEntity(
                        type="parent_task",
                        value=manifest.provenance.parent_task_id,
                    )
                )
            episode = Episode(
                episode_id=f"ep-{uuid4().hex[:8]}",
                created_at=datetime.now(timezone.utc).isoformat(),
                source=EpisodeSource(
                    type="task_completion",
                    skill_used=manifest.skill_id or "unknown",
                    trigger=f"task:{manifest.task_id}",
                ),
                summary=extraction.episode_summary,
                key_findings=extraction.key_findings,
                related_entities=tuple(channel_entities),
                related_goals=(
                    (manifest.provenance.goal_id,)
                    if getattr(manifest.provenance, "goal_id", "")
                    else ()
                ),
                related_duties=(
                    (manifest.provenance.duty_id,)
                    if getattr(manifest.provenance, "duty_id", "")
                    else ()
                ),
            )
            await _persist_episode(
                memory_dir=memory_dir,
                episode=episode,
                episode_lock=episode_lock,
                tenant_id=worker_context.tenant_id,
                worker_id=worker_context.worker_id,
                memory_orchestrator=memory_orchestrator,
                openviking_client=openviking_client,
                openviking_scope_prefix=openviking_scope_prefix,
            )
            async with episode_lock:
                await _archive_episode_candidates(
                    worker_dir,
                    memory_dir,
                    tenant_id=worker_context.tenant_id,
                    worker_id=worker_context.worker_id,
                )
            if extraction.applied_rule_ids and not is_suggestion_preview:
                write_linkage(
                    memory_dir,
                    create_links(
                        episode.episode_id,
                        extraction.applied_rule_ids,
                        episode.created_at,
                    ),
                )
                await apply_outcome_feedback(
                    episode_id=episode.episode_id,
                    outcome="success",
                    memory_dir=memory_dir,
                    rules_dir=rules_dir,
                )

        if is_suggestion_preview:
            return

        if goal_lock_registry is not None:
            project_result = await project_task_outcome_to_goal(
                manifest=manifest,
                worker_dir=worker_dir,
                goal_lock_registry=goal_lock_registry,
                llm_client=llm_client,
            )
            if (
                project_result.goal_completed
                and project_result.goal is not None
                and suggestion_store is not None
                and llm_client is not None
            ):
                if suggestion_store.creation_block_reason(
                    worker_context.tenant_id,
                    worker_context.worker_id,
                    suggestion_type="goal_to_duty",
                    source_entity_id=project_result.goal.goal_id,
                ) is None:
                    duty = await goal_to_duty(project_result.goal, llm_client)
                    if duty is not None:
                        suggestion_store.create(
                            worker_context.tenant_id,
                            worker_context.worker_id,
                            SuggestionRecord(
                                suggestion_id=f"sugg-{uuid4().hex[:8]}",
                                type="goal_to_duty",
                                source_entity_type="goal",
                                source_entity_id=project_result.goal.goal_id,
                                title=f"建议将 Goal 转为 Duty: {project_result.goal.title}",
                                reason=(
                                    f"Goal '{project_result.goal.title}' 已完成，"
                                    "建议转成维护型 Duty。"
                                ),
                                evidence=(manifest.task_id, project_result.goal.goal_id),
                                confidence=0.9,
                                candidate_payload=json.dumps(
                                    {
                                        "tenant_id": worker_context.tenant_id,
                                        "worker_id": worker_context.worker_id,
                                        "duty_id": duty.duty_id,
                                        "title": duty.title,
                                        "action": duty.action,
                                        "quality_criteria": list(duty.quality_criteria),
                                        "preferred_skill_ids": list(duty.preferred_skill_ids),
                                        "source_goal_id": project_result.goal.goal_id,
                                    },
                                    ensure_ascii=False,
                                ),
                                expires_at=add_days_iso(datetime.now(timezone.utc).isoformat(), 30),
                            ),
                        )

        preferences = extract_preferences(
            user_input=manifest.task_description,
            assistant_summary=extraction.episode_summary,
        )
        if preferences:
            existing_preferences = load_preferences(worker_dir / "preferences.jsonl")
            merged_preferences = merge_preferences(existing_preferences, preferences)
            if merged_preferences != existing_preferences:
                save_preferences(worker_dir / "preferences.jsonl", merged_preferences)
            for preference in merged_preferences:
                if any(item.preference_id == preference.preference_id for item in existing_preferences):
                    continue
                if memory_orchestrator is not None:
                    await _emit_memory_write_best_effort(memory_orchestrator, MemoryWriteEvent(
                        action="create",
                        target="preference",
                        entity_id=preference.preference_id,
                        content=PreferenceWritePayload(
                            tenant_id=worker_context.tenant_id,
                            worker_id=worker_context.worker_id,
                            content=preference.content,
                        ),
                        source_subsystem="preference",
                        occurred_at=preference.extracted_at,
                    ))

        decisions = extract_decisions(
            user_input=manifest.task_description,
            assistant_summary=extraction.episode_summary,
        )
        if decisions:
            existing_decisions = load_decisions(worker_dir / "decisions.jsonl")
            merged_decisions = supersede_decisions(existing_decisions, decisions)
            if merged_decisions != existing_decisions:
                save_decisions(worker_dir / "decisions.jsonl", merged_decisions)
            for decision in decisions:
                if not any(
                    item.decision_id == decision.decision_id for item in merged_decisions
                ):
                    continue
                if memory_orchestrator is not None:
                    await _emit_memory_write_best_effort(memory_orchestrator, MemoryWriteEvent(
                        action="create",
                        target="decision",
                        entity_id=decision.decision_id,
                        content=DecisionWritePayload(
                            tenant_id=worker_context.tenant_id,
                            worker_id=worker_context.worker_id,
                            decision=decision.decision,
                        ),
                        source_subsystem="decision",
                        occurred_at=decision.decided_at,
                    ))

        persona = parse_persona_md(worker_dir / "PERSONA.md")
        existing_rules = load_rules(rules_dir)
        candidate = None

        if llm_client is not None:
            candidate = await extract_rule_from_reflection(
                execution_summary=extraction.episode_summary or manifest.task_description,
                outcome_quality="success",
                llm_client=llm_client,
            )

        if candidate is None and extraction.rule_candidates:
            candidate = RuleCandidate(
                rule=extraction.rule_candidates[0],
                reason="Derived from successful task execution summary.",
                category="strategy",
                scope=RuleScope(
                    skills=(manifest.skill_id,) if manifest.skill_id else (),
                ),
                source=RuleSource(
                    type="self_reflection",
                    evidence=extraction.episode_summary,
                    created_at=datetime.now(timezone.utc).isoformat(),
                ),
            )

        if candidate is not None:
            await validate_and_create_rule(
                candidate=candidate,
                rules_dir=rules_dir,
                principles=persona.principles,
                existing_rules=existing_rules,
                memory_orchestrator=memory_orchestrator,
                tenant_id=worker_context.tenant_id,
                worker_id=worker_context.worker_id,
            )

    async def _archive_episode_candidates(worker_dir, memory_dir, *, tenant_id: str, worker_id: str):
        indices = load_index(memory_dir)
        if not indices:
            return

        now_iso = datetime.now(timezone.utc).isoformat()
        candidate_ids = identify_archive_candidates(indices, now_iso)
        if not candidate_ids:
            return

        archive_manager = ArchiveManager(worker_dir)
        episodic_indexer = build_episodic_indexer(
            openviking_client,
            scope_prefix=openviking_scope_prefix,
            tenant_id=tenant_id,
            worker_id=worker_id,
        )

        for episode_id in candidate_ids:
            episode_path = memory_dir / "episodes" / f"{episode_id}.md"
            if not episode_path.exists():
                continue
            await archive_manager.archive_episode(
                episode_path,
                ArchiveMetadata(
                    archived_at=now_iso,
                    archived_by="system",
                    reason="episodic_decay",
                ),
            )
            if episodic_indexer is not None:
                try:
                    await episodic_indexer.delete_episode(episode_id)
                except Exception:
                    pass

    return _handle_post_run


def build_error_feedback_handler(
    workspace_root,
    episode_lock,
    memory_orchestrator=None,
    openviking_client=None,
    openviking_scope_prefix: str = "viking://",
):
    """Build failure feedback hook for final task failures."""
    from datetime import datetime, timezone
    from pathlib import Path
    from uuid import uuid4

    from src.memory.episodic.linkage import (
        apply_outcome_feedback,
        create_links,
        write_linkage,
    )
    from src.memory.episodic.models import Episode, EpisodeSource

    async def _handle_error_feedback(manifest, worker_context, applied_rule_ids):
        worker_dir = (
            Path(workspace_root) / "tenants" / manifest.tenant_id
            / "workers" / manifest.worker_id
        )
        memory_dir = worker_dir / "memory"
        rules_dir = worker_dir / "rules"
        is_suggestion_preview = (
            str(getattr(getattr(manifest, "provenance", None), "source_type", "") or "")
            == "suggestion_preview"
        )
        if _episodic_write_allowed(getattr(worker_context, "trust_gate", None)):
            episode = Episode(
                episode_id=f"ep-{uuid4().hex[:8]}",
                created_at=datetime.now(timezone.utc).isoformat(),
                source=EpisodeSource(
                    type="suggestion_preview_failure" if is_suggestion_preview else "task_failure",
                    skill_used=manifest.skill_id or "unknown",
                    trigger=f"task:{manifest.task_id}",
                ),
                summary=f"Task failed: {(manifest.error_message or manifest.task_description)[:200]}",
                key_findings=(),
                related_entities=(),
            )
            await _persist_episode(
                memory_dir=memory_dir,
                episode=episode,
                episode_lock=episode_lock,
                tenant_id=manifest.tenant_id,
                worker_id=manifest.worker_id,
                memory_orchestrator=memory_orchestrator,
                openviking_client=openviking_client,
                openviking_scope_prefix=openviking_scope_prefix,
            )
            if applied_rule_ids and not is_suggestion_preview:
                write_linkage(
                    memory_dir,
                    create_links(episode.episode_id, applied_rule_ids, episode.created_at),
                )
            if not is_suggestion_preview:
                await apply_outcome_feedback(
                    episode_id=episode.episode_id,
                    outcome="failure",
                    memory_dir=memory_dir,
                    rules_dir=rules_dir,
                )

    return _handle_error_feedback


def build_memory_flush_callback(
    workspace_root,
    memory_orchestrator,
    episode_lock,
    openviking_client=None,
    openviking_scope_prefix: str = "viking://",
):
    """Build callback that persists memory_flush artifacts."""
    from datetime import datetime, timezone
    from pathlib import Path
    from uuid import uuid4

    from src.memory.episodic.models import Episode, EpisodeSource
    from src.worker.rules.models import RuleCandidate, RuleScope, RuleSource
    from src.worker.rules.rule_generator import validate_and_create_rule

    async def _on_flush(flush_result):
        worker_dir_str = str(flush_result.get("worker_dir", ""))
        if not worker_dir_str:
            return
        worker_dir = Path(worker_dir_str)
        try:
            worker_id = worker_dir.name
            tenant_id = worker_dir.parent.parent.name
        except Exception:
            return
        memory_dir = worker_dir / "memory"
        rules_dir = worker_dir / "rules"
        for summary in flush_result.get("episodes", ()):
            episode = Episode(
                episode_id=f"ep-{uuid4().hex[:8]}",
                created_at=datetime.now(timezone.utc).isoformat(),
                source=EpisodeSource(
                    type="task_completion",
                    skill_used="memory_flush",
                    trigger="memory_flush",
                ),
                summary=str(summary)[:200],
                key_findings=(),
                related_entities=(),
            )
            await _persist_episode(
                memory_dir=memory_dir,
                episode=episode,
                episode_lock=episode_lock,
                tenant_id=tenant_id,
                worker_id=worker_id,
                memory_orchestrator=memory_orchestrator,
                openviking_client=openviking_client,
                openviking_scope_prefix=openviking_scope_prefix,
            )
        for rule_text in flush_result.get("rule_candidates", ()):
            candidate = RuleCandidate(
                rule=str(rule_text),
                reason="Recovered from pre-compaction memory flush.",
                category="strategy",
                scope=RuleScope(),
                source=RuleSource(
                    type="self_reflection",
                    evidence=str(rule_text),
                    created_at=datetime.now(timezone.utc).isoformat(),
                ),
            )
            await validate_and_create_rule(
                candidate=candidate,
                rules_dir=rules_dir,
                principles=(),
                existing_rules=(),
                memory_orchestrator=memory_orchestrator,
                tenant_id=tenant_id,
                worker_id=worker_id,
            )

    return _on_flush
