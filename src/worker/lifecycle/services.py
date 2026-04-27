"""Lifecycle service facade for command/runtime integrations."""
from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from .duty_builder import (
    apply_duty_redefine,
    build_duty_from_payload,
    find_duty_file,
    write_duty_md,
)
from .file_io import atomic_write_text
from .skill_builder import (
    _prune_empty_dirs,
    build_skill_from_payload,
    expand_instructions_with_llm,
    render_quality_criteria_block,
    write_skill_md,
)


@dataclass(frozen=True)
class LifecycleServices:
    """Centralize lifecycle path resolution and materialization helpers."""

    workspace_root: Path
    suggestion_store: object | None = None
    feedback_store: object | None = None
    goal_lock_registry: object | None = None

    def worker_dir(self, tenant_id: str, worker_id: str) -> Path:
        return self.workspace_root / "tenants" / tenant_id / "workers" / worker_id

    def duties_dir(self, tenant_id: str, worker_id: str) -> Path:
        return self.worker_dir(tenant_id, worker_id) / "duties"

    def skills_dir(self, tenant_id: str, worker_id: str) -> Path:
        return self.worker_dir(tenant_id, worker_id) / "skills"

    def materialize_duty_from_payload(
        self,
        *,
        tenant_id: str,
        worker_id: str,
        payload: dict,
        default_title: str = "",
    ):
        duty = build_duty_from_payload(payload, default_title=default_title)
        path = write_duty_md(duty, self.duties_dir(tenant_id, worker_id))
        return duty, path

    def apply_duty_redefine_payload(
        self,
        *,
        tenant_id: str,
        worker_id: str,
        duty_id: str,
        payload: dict,
    ):
        return apply_duty_redefine(
            self.duties_dir(tenant_id, worker_id),
            duty_id,
            payload,
        )

    async def materialize_skill_from_payload(
        self,
        *,
        tenant_id: str,
        worker_id: str,
        payload: dict,
        llm_client: object | None = None,
        source_record=None,
    ):
        """Build, validate, write, and back-link one evolved skill."""
        skill = build_skill_from_payload(payload)
        seed = str(payload.get("instructions_seed", "") or "").strip()
        reason = str(payload.get("instructions_reason", "") or "").strip()
        quality_criteria = tuple(
            str(item).strip()
            for item in payload.get("quality_criteria", ())
            if str(item).strip()
        )
        if llm_client is not None and seed:
            expanded = await expand_instructions_with_llm(seed, reason, llm_client)
            expanded += render_quality_criteria_block(quality_criteria)
            skill = replace(
                skill,
                instructions={**dict(skill.instructions), "general": expanded},
            )

        source_type, source_entity_id = self._resolve_skill_source(payload, source_record)
        snapshots = [self._snapshot_file(self.skills_dir(tenant_id, worker_id) / skill.skill_id / "SKILL.md")]
        if source_type == "duty" and source_entity_id:
            duty_file = find_duty_file(self.duties_dir(tenant_id, worker_id), source_entity_id)
            if duty_file is None:
                raise ValueError(f"Duty '{source_entity_id}' 未找到，无法绑定 Skill。")
            snapshots.append(self._snapshot_file(duty_file))
        elif source_type == "rule" and source_entity_id:
            rule_file = self._find_rule_file(tenant_id, worker_id, source_entity_id)
            if rule_file is None:
                raise ValueError(f"Rule '{source_entity_id}' 未找到，无法标记 crystallized。")
            snapshots.append(self._snapshot_file(rule_file))

        try:
            path = write_skill_md(skill, self.skills_dir(tenant_id, worker_id))
            if source_type == "duty" and source_entity_id:
                self._bind_skill_to_duty(tenant_id, worker_id, source_entity_id, skill.skill_id)
            elif source_type == "rule" and source_entity_id:
                self._mark_rule_crystallized(tenant_id, worker_id, source_entity_id)
        except Exception:
            for snapshot in reversed(snapshots):
                self._restore_snapshot(snapshot)
            raise
        return skill, path

    def _bind_skill_to_duty(
        self,
        tenant_id: str,
        worker_id: str,
        duty_id: str,
        skill_id: str,
    ) -> None:
        """Persist the generated skill binding back to the source duty."""
        from src.worker.duty.parser import parse_duty

        duties_dir = self.duties_dir(tenant_id, worker_id)
        duty_file = find_duty_file(duties_dir, duty_id)
        if duty_file is None:
            raise ValueError(f"Duty '{duty_id}' 未找到，无法绑定 Skill。")
        duty = parse_duty(duty_file.read_text(encoding="utf-8"))
        updated = replace(duty, skill_id=skill_id)
        write_duty_md(updated, duties_dir, filename=duty_file.name)

    def _mark_rule_crystallized(
        self,
        tenant_id: str,
        worker_id: str,
        rule_id: str,
    ) -> None:
        """Mark the source rule as crystallized after skill approval."""
        from src.worker.rules.crystallizer import _mark_rule_crystallized
        from src.worker.rules.rule_manager import load_rules

        rules_dir = self.worker_dir(tenant_id, worker_id) / "rules"
        for rule in load_rules(rules_dir):
            if rule.rule_id == rule_id:
                _mark_rule_crystallized(rules_dir, rule)
                return
        raise ValueError(f"Rule '{rule_id}' 未找到，无法标记 crystallized。")

    def _resolve_skill_source(self, payload: dict, source_record) -> tuple[str, str]:
        """Prefer immutable suggestion metadata over mutable payload fields."""
        if source_record is not None:
            record_type = str(getattr(source_record, "type", "") or "")
            source_entity_id = str(getattr(source_record, "source_entity_id", "") or "")
            if record_type == "duty_to_skill":
                return "duty", source_entity_id
            if record_type == "rule_to_skill":
                return "rule", source_entity_id
        source_type = str(payload.get("source_type", "") or "").strip()
        if source_type == "duty":
            return source_type, str(payload.get("source_duty_id", "") or "").strip()
        if source_type == "rule":
            return source_type, str(payload.get("source_rule_id", "") or "").strip()
        return "", ""

    def _find_rule_file(self, tenant_id: str, worker_id: str, rule_id: str) -> Path | None:
        rules_dir = self.worker_dir(tenant_id, worker_id) / "rules"
        for subdir in ("learned", "directives"):
            candidate = rules_dir / subdir / f"{rule_id}.md"
            if candidate.is_file():
                return candidate
        return None

    def _snapshot_file(self, path: Path) -> tuple[Path, str | None]:
        if path.is_file():
            return path, path.read_text(encoding="utf-8")
        return path, None

    def _restore_snapshot(self, snapshot: tuple[Path, str | None]) -> None:
        path, content = snapshot
        if content is None:
            path.unlink(missing_ok=True)
            if path.name == "SKILL.md":
                _prune_empty_dirs(path.parent, stop_dir=path.parent.parent)
            return
        atomic_write_text(path, content)
