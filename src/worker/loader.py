"""Worker entry loading helpers used by bootstrap and runtime reload paths."""
from __future__ import annotations

from pathlib import Path

from src.skills.loader import SkillLoader
from src.skills.registry import SkillRegistry
from src.worker.parser import parse_persona_md
from src.worker.registry import WorkerEntry

_PERSONA_MD_FILENAME = "PERSONA.md"
_SKILLS_DIR_NAME = "skills"


def load_worker_entry(
    workspace_root: Path,
    tenant_id: str,
    worker_id: str,
    skill_loader: SkillLoader | None = None,
) -> WorkerEntry:
    """Load one worker entry from disk for bootstrap and runtime refresh."""
    skill_loader = skill_loader or SkillLoader()
    worker_dir = workspace_root / "tenants" / tenant_id / "workers" / worker_id
    persona_md = worker_dir / _PERSONA_MD_FILENAME
    if not persona_md.is_file():
        raise FileNotFoundError(f"PERSONA.md not found for worker '{worker_id}'")

    worker = parse_persona_md(persona_md)
    ensure_worker_runtime_dirs(worker_dir)

    system_skills = skill_loader.scan(workspace_root / "system" / _SKILLS_DIR_NAME)
    tenant_skills = skill_loader.scan(
        workspace_root / "tenants" / tenant_id / _SKILLS_DIR_NAME
    )
    worker_skills = skill_loader.scan(worker_dir / worker.skills_dir)
    skill_registry = SkillRegistry.merge(
        system_skills=system_skills,
        tenant_skills=tenant_skills,
        worker_skills=worker_skills,
    )
    return WorkerEntry(worker=worker, skill_registry=skill_registry)


def ensure_worker_runtime_dirs(worker_dir: Path) -> None:
    """Create the standard worker runtime directories when missing."""
    for relative in (
        "duties",
        "goals",
        "rules/directives",
        "rules/learned",
        "memory",
        "archive",
        "tasks/active",
    ):
        (worker_dir / relative).mkdir(parents=True, exist_ok=True)
