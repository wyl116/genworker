"""
Skill system bootstrap initializer.

Loads skills in three levels (system -> tenant -> worker) and merges
them into a single SkillRegistry with proper override semantics.
"""
from pathlib import Path
from typing import List

from src.common.logger import get_logger
from src.common.paths import resolve_workspace_root
from src.skills.loader import SkillLoader
from src.skills.registry import SkillRegistry

from .base import Initializer
from .context import BootstrapContext

logger = get_logger()

# Default directory names
_SKILLS_DIR_NAME = "skills"


class SkillInitializer(Initializer):
    """
    Bootstrap initializer for the skill system.

    Scans system, tenant, and worker skill directories,
    then merges into a three-level override registry.
    """

    @property
    def name(self) -> str:
        return "skills"

    @property
    def depends_on(self) -> List[str]:
        return ["logging"]

    @property
    def priority(self) -> int:
        return 50

    async def initialize(self, context: BootstrapContext) -> bool:
        """Load and register all skills from workspace directories."""
        try:
            loader = SkillLoader()
            registry = _build_registry(loader, context)
            context.set_state("skill_registry", registry)
            context.set_state("skill_loader", loader)

            logger.info(
                f"[SkillInitializer] Loaded {len(registry)} skill(s) total"
            )
            return True
        except Exception as exc:
            logger.error(
                f"[SkillInitializer] Failed to initialize: {exc}",
                exc_info=True,
            )
            context.record_error(self.name, str(exc))
            return False

    async def cleanup(self) -> None:
        """No resources to clean up."""
        pass


def _build_registry(
    loader: SkillLoader,
    context: BootstrapContext,
) -> SkillRegistry:
    """Build a merged registry from three-level skill directories."""
    workspace_root = _get_workspace_root(context)

    system_dir = workspace_root / "system" / _SKILLS_DIR_NAME
    tenant_id = context.get_state("tenant_id", "")
    worker_id = context.get_state("worker_id", "")

    system_skills = loader.scan(system_dir)

    tenant_skills = ()
    if tenant_id:
        tenant_dir = workspace_root / "tenants" / tenant_id / _SKILLS_DIR_NAME
        tenant_skills = loader.scan(tenant_dir)

    worker_skills = ()
    if worker_id:
        worker_dir = (
            workspace_root / "tenants" / tenant_id / "workers"
            / worker_id / _SKILLS_DIR_NAME
        )
        worker_skills = loader.scan(worker_dir)

    return SkillRegistry.merge(
        system_skills=system_skills,
        tenant_skills=tenant_skills,
        worker_skills=worker_skills,
    )


def _get_workspace_root(context: BootstrapContext) -> Path:
    """Resolve the workspace root directory."""
    return resolve_workspace_root(context.get_state("workspace_root"))
