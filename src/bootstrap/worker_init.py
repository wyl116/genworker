"""
Worker bootstrap initializer.

Scans workspace/tenants/{tid}/workers/{wid}/PERSONA.md files,
loads Workers with trust gate computation, builds WorkerRegistry.
"""
from pathlib import Path
from typing import List

from src.common.logger import get_logger
from src.common.paths import resolve_workspace_root
from src.common.tenant import TenantLoader
from src.skills.loader import SkillLoader
from src.worker.loader import load_worker_entry as _load_worker_entry
from src.worker.registry import WorkerEntry, WorkerRegistry, build_worker_registry
from src.worker.trust_gate import compute_trust_gate

from .base import Initializer
from .context import BootstrapContext

logger = get_logger()

_PERSONA_MD_FILENAME = "PERSONA.md"

# Historical import path retained for callers outside the repo.
load_worker_entry = _load_worker_entry


class WorkerInitializer(Initializer):
    """
    Bootstrap initializer for the worker system.

    Scans tenant worker directories, parses PERSONA.md files,
    computes trust gates, and builds the WorkerRegistry.
    """

    @property
    def name(self) -> str:
        return "workers"

    @property
    def depends_on(self) -> List[str]:
        return ["skills"]

    @property
    def priority(self) -> int:
        return 60

    async def initialize(self, context: BootstrapContext) -> bool:
        """Load all workers from workspace."""
        try:
            workspace_root = _get_workspace_root(context)
            tenant_id = context.get_state("tenant_id", "")

            if not tenant_id:
                logger.warning("[WorkerInitializer] No tenant_id configured")
                context.set_state("worker_registry", WorkerRegistry())
                return True

            tenant_loader = TenantLoader(workspace_root)
            tenant = tenant_loader.load(tenant_id)
            skill_loader = SkillLoader()

            entries = _scan_workers(
                workspace_root=workspace_root,
                tenant_id=tenant_id,
                skill_loader=skill_loader,
            )

            registry = build_worker_registry(
                entries=entries,
                default_worker_id=tenant.default_worker or "",
            )

            context.set_state("worker_registry", registry)
            context.set_state("tenant_loader", tenant_loader)

            # Compute trust gates for each worker
            trust_gates = {}
            for entry in entries:
                gate = compute_trust_gate(entry.worker, tenant)
                trust_gates[entry.worker.worker_id] = gate

            context.set_state("trust_gates", trust_gates)

            # Phase 7a: scan memory/, rules/, archive/ for each worker
            worker_subsystems = _scan_worker_subsystems(
                workspace_root, tenant_id, entries,
            )
            context.set_state("worker_subsystems", worker_subsystems)

            logger.info(
                f"[WorkerInitializer] Loaded {len(registry)} worker(s) "
                f"for tenant '{tenant_id}'"
            )
            return True

        except Exception as exc:
            logger.error(
                f"[WorkerInitializer] Failed: {exc}",
                exc_info=True,
            )
            context.record_error(self.name, str(exc))
            return False

    async def cleanup(self) -> None:
        """No resources to clean up."""
        pass


def _scan_workers(
    workspace_root: Path,
    tenant_id: str,
    skill_loader: SkillLoader,
) -> list[WorkerEntry]:
    """Scan all worker directories under a tenant and build WorkerEntry list."""
    workers_dir = workspace_root / "tenants" / tenant_id / "workers"
    if not workers_dir.is_dir():
        logger.warning(f"[WorkerInitializer] Workers dir not found: {workers_dir}")
        return []

    entries: list[WorkerEntry] = []
    for worker_dir in sorted(workers_dir.iterdir()):
        if not worker_dir.is_dir():
            continue

        persona_md = worker_dir / _PERSONA_MD_FILENAME
        if not persona_md.is_file():
            logger.debug(f"[WorkerInitializer] No PERSONA.md in {worker_dir}")
            continue

        try:
            entry = _load_worker_entry(
                workspace_root=workspace_root,
                tenant_id=tenant_id,
                worker_id=worker_dir.name,
                skill_loader=skill_loader,
            )
            entries.append(entry)

            logger.info(
                f"[WorkerInitializer] Loaded worker '{entry.worker.worker_id}' "
                f"with {len(entry.skill_registry)} skill(s)"
            )

        except Exception as exc:
            logger.error(
                f"[WorkerInitializer] Failed to load worker from {worker_dir}: {exc}",
                exc_info=True,
            )

    return entries

def _scan_worker_subsystems(
    workspace_root: Path,
    tenant_id: str,
    entries: list[WorkerEntry],
) -> dict[str, dict[str, bool]]:
    """
    Scan Phase 7a subsystem directories for each worker.

    Returns a dict: worker_id -> {"memory": bool, "rules": bool, "archive": bool}
    indicating which subsystem directories exist.
    """
    results: dict[str, dict[str, bool]] = {}
    for entry in entries:
        worker_id = entry.worker.worker_id
        worker_dir = (
            workspace_root / "tenants" / tenant_id / "workers" / worker_id
        )
        subsystems = {
            "memory": (worker_dir / "memory").is_dir(),
            "rules": (worker_dir / "rules").is_dir(),
            "archive": (worker_dir / "archive").is_dir(),
        }
        results[worker_id] = subsystems
        if any(subsystems.values()):
            active = [k for k, v in subsystems.items() if v]
            logger.info(
                f"[WorkerInitializer] Worker '{worker_id}' "
                f"Phase 7a subsystems: {', '.join(active)}"
            )
    return results


def _get_workspace_root(context: BootstrapContext) -> Path:
    """Resolve the workspace root directory."""
    return resolve_workspace_root(context.get_state("workspace_root"))
