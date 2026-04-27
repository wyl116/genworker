"""
SkillRegistry - three-level override registry for skills.

Override order: Worker > Tenant > System.
Same skill_id at a higher scope level replaces the lower one.
"""
from dataclasses import dataclass, field
from typing import Mapping, Optional, Sequence

from src.common.logger import get_logger

from .models import SCOPE_PRIORITY, Skill, SkillScope

logger = get_logger()


@dataclass(frozen=True)
class SkillRegistry:
    """
    Immutable registry of skills with three-level override semantics.

    Use dataclasses.replace() or the merge() class method to produce
    new registries with additional skills.
    """
    _skills: Mapping[str, Skill] = field(default_factory=dict)

    @property
    def skills(self) -> Mapping[str, Skill]:
        """All registered skills keyed by skill_id."""
        return self._skills

    def get(self, skill_id: str) -> Optional[Skill]:
        """Get a skill by ID, or None if not found."""
        return self._skills.get(skill_id)

    def list_all(self) -> tuple[Skill, ...]:
        """List all registered skills."""
        return tuple(self._skills.values())

    def get_default_skill(self) -> Optional[Skill]:
        """Get the default skill (if any is configured)."""
        for skill in self._skills.values():
            if skill.default_skill:
                return skill
        return None

    def __len__(self) -> int:
        return len(self._skills)

    def __contains__(self, skill_id: str) -> bool:
        return skill_id in self._skills

    @classmethod
    def from_skills(cls, skills: Sequence[Skill]) -> "SkillRegistry":
        """
        Build a registry from a sequence of skills.

        If duplicate skill_ids exist, the one with higher scope priority wins.
        Among equal scope, higher priority field value wins.
        """
        merged = _merge_skill_sequence(skills)
        return cls(_skills=merged)

    @classmethod
    def merge(
        cls,
        system_skills: Sequence[Skill] = (),
        tenant_skills: Sequence[Skill] = (),
        worker_skills: Sequence[Skill] = (),
    ) -> "SkillRegistry":
        """
        Merge three levels of skills with override semantics.

        Worker > Tenant > System: a higher-level skill with the same
        skill_id replaces the lower-level one entirely.
        """
        # Process in order: system first, then tenant overrides, then worker overrides
        combined = list(system_skills) + list(tenant_skills) + list(worker_skills)
        merged = _merge_skill_sequence(combined)

        logger.info(
            f"[SkillRegistry] Merged {len(merged)} skill(s) "
            f"(system={len(list(system_skills))}, "
            f"tenant={len(list(tenant_skills))}, "
            f"worker={len(list(worker_skills))})"
        )
        return cls(_skills=merged)


def _merge_skill_sequence(skills: Sequence[Skill]) -> Mapping[str, Skill]:
    """
    Merge a sequence of skills, resolving duplicates by scope priority.

    Higher scope priority (Worker > Tenant > System) wins.
    Among equal scope, higher priority field value wins.
    """
    result: dict[str, Skill] = {}
    for skill in skills:
        existing = result.get(skill.skill_id)
        if existing is None or _should_override(existing, skill):
            result[skill.skill_id] = skill
    return result


def _should_override(existing: Skill, candidate: Skill) -> bool:
    """Determine if candidate should override existing skill."""
    existing_scope_priority = SCOPE_PRIORITY.get(existing.scope, 0)
    candidate_scope_priority = SCOPE_PRIORITY.get(candidate.scope, 0)

    if candidate_scope_priority > existing_scope_priority:
        return True
    if candidate_scope_priority == existing_scope_priority:
        return candidate.priority > existing.priority
    return False
