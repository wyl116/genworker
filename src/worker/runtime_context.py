"""Runtime context loading for worker execution."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.common.context_fence import fence_memory_context
from src.common.logger import get_logger
from src.memory.preferences.extractor import (
    format_decisions_for_prompt,
    format_preferences_for_prompt,
    load_active_decisions,
    load_preferences,
)
from src.worker.goal.parser import parse_goal
from src.worker.integrations.goal_generator import find_goal_file
from src.worker.scripts.models import PreScript
from src.worker.context_builder import build_worker_context
from src.worker.rules.rule_injector import format_for_prompt, select_rules
from src.worker.rules.rule_manager import load_rules

logger = get_logger()


@dataclass(frozen=True)
class RuntimeContextBundle:
    """Resolved context payload for one worker run."""

    worker_context: Any
    applied_rule_ids: tuple[str, ...]
    worker_dir: Path


class WorkerRuntimeContextBuilder:
    """Load rules, memory and profile context for a worker run."""

    def __init__(
        self,
        *,
        workspace_root: Path,
        memory_orchestrator: Any | None,
    ) -> None:
        self._workspace_root = workspace_root
        self._memory_orchestrator = memory_orchestrator

    async def build(
        self,
        *,
        worker,
        tenant,
        trust_gate,
        skill,
        available_tools: tuple[Any, ...],
        available_skill_ids: tuple[str, ...],
        task: str,
        task_context: str,
        contact_context: str,
        subagent_enabled: bool,
        provenance: Any | None = None,
    ) -> RuntimeContextBundle:
        worker_dir = (
            self._workspace_root / "tenants" / tenant.tenant_id / "workers" / worker.worker_id
        )
        rules_text, applied_rule_ids = _load_rules_text_with_ids(
            worker_dir,
            trust_gate,
            skill_id=skill.skill_id,
        )
        if self._memory_orchestrator is not None:
            query_result = await self._memory_orchestrator.query(
                text=task,
                worker_id=worker.worker_id,
                token_budget=2000,
                trust_gate=trust_gate,
                tenant_id=tenant.tenant_id,
                episodic_base_dir=worker_dir / "memory",
                worker_dir=worker_dir,
                skill_id=skill.skill_id,
                goal_id=getattr(provenance, "goal_id", None) or None,
                duty_id=getattr(provenance, "duty_id", None) or None,
            )
            episodic_text = (
                fence_memory_context(query_result.merged_context, source="orchestrator")
                if query_result.merged_context else ""
            )
            preferences_text = ""
            decisions_text = ""
        else:
            episodic_text = ""
            preferences_text = ""
            decisions_text = ""

        profile_text = _load_profile_text(worker_dir)
        combined_history = "\n\n".join(
            part
            for part in (
                episodic_text,
                profile_text,
                preferences_text,
                decisions_text,
            )
            if part
        )
        goal_default_pre_script = _load_goal_default_pre_script(
            worker_dir=worker_dir,
            goal_id=getattr(provenance, "goal_id", "") if provenance is not None else "",
        )

        worker_context = build_worker_context(
            worker=worker,
            tenant=tenant,
            trust_gate=trust_gate,
            available_tools=available_tools,
            available_skill_ids=available_skill_ids,
            learned_rules=rules_text,
            historical_context=combined_history,
            task_context=task_context,
            contact_context=contact_context,
            subagent_enabled=subagent_enabled,
            memory_orchestrator=self._memory_orchestrator,
            worker_dir=worker_dir,
            goal_default_pre_script=goal_default_pre_script,
        )
        return RuntimeContextBundle(
            worker_context=worker_context,
            applied_rule_ids=applied_rule_ids,
            worker_dir=worker_dir,
        )


def _load_goal_default_pre_script(
    *,
    worker_dir: Path,
    goal_id: str,
) -> PreScript | None:
    goal_id = str(goal_id or "").strip()
    if not goal_id:
        return None
    goal_file = find_goal_file(worker_dir / "goals", goal_id)
    if goal_file is None:
        return None
    try:
        goal = parse_goal(goal_file.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning(
            "[WorkerRuntimeContext] Failed to load goal default_pre_script for %s: %s",
            goal_id,
            exc,
        )
        return None
    return goal.default_pre_script


def _load_rules_text_with_ids(
    worker_dir: Path,
    trust_gate,
    skill_id: str | None,
) -> tuple[str, tuple[str, ...]]:
    if not trust_gate.learned_rules_enabled:
        return "", ()
    rules_dir = worker_dir / "rules"
    if not rules_dir.exists():
        return "", ()
    try:
        all_rules = load_rules(rules_dir)
        selected = select_rules(all_rules, skill_id=skill_id)
        return format_for_prompt(selected), tuple(
            rule.rule_id for rule in selected if rule.type == "learned"
        )
    except Exception as exc:
        logger.warning("[WorkerRuntimeContext] Failed to load rules: %s", exc)
        return "", ()
def _load_profile_text(worker_dir: Path) -> str:
    profile_path = worker_dir / "profile" / "PROFILE.md"
    if not profile_path.exists():
        return ""
    try:
        return fence_memory_context(
            profile_path.read_text(encoding="utf-8"),
            source="behavior_profile",
        )
    except Exception as exc:
        logger.warning("[WorkerRuntimeContext] Failed to load behavior profile: %s", exc)
        return ""


def _load_preferences_text(worker_dir: Path) -> str:
    preferences_path = worker_dir / "preferences.jsonl"
    if not preferences_path.exists():
        return ""
    try:
        preferences = load_preferences(preferences_path)
        if not preferences:
            return ""
        return fence_memory_context(
            format_preferences_for_prompt(preferences),
            source="user_preferences",
        )
    except Exception as exc:
        logger.warning("[WorkerRuntimeContext] Failed to load preferences: %s", exc)
        return ""


def _load_decisions_text(worker_dir: Path) -> str:
    decisions_path = worker_dir / "decisions.jsonl"
    if not decisions_path.exists():
        return ""
    try:
        decisions = load_active_decisions(decisions_path)
        if not decisions:
            return ""
        return fence_memory_context(
            format_decisions_for_prompt(decisions),
            source="user_decisions",
        )
    except Exception as exc:
        logger.warning("[WorkerRuntimeContext] Failed to load decisions: %s", exc)
        return ""
