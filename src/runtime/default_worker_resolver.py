from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.bootstrap.context import BootstrapContext

_DEMO_DEFAULT_WORKER_ID = "analyst-01"


@dataclass(frozen=True)
class DefaultWorkerSelection:
    worker_id: str
    worker_loaded: bool


def resolve_default_worker(context: "BootstrapContext" | Any) -> DefaultWorkerSelection:
    """Resolve the default chat worker using one backend-only policy."""
    worker_registry = context.get_state("worker_registry")
    if worker_registry is None:
        return DefaultWorkerSelection(worker_id="", worker_loaded=False)

    default_entry = getattr(worker_registry, "get_default", lambda: None)()
    if default_entry is not None:
        return DefaultWorkerSelection(
            worker_id=default_entry.worker.worker_id,
            worker_loaded=True,
        )

    tenant_id = str(context.get_state("tenant_id", "demo") or "demo")
    if tenant_id == "demo":
        demo_entry = getattr(worker_registry, "get", lambda _worker_id: None)(
            _DEMO_DEFAULT_WORKER_ID
        )
        if demo_entry is not None:
            return DefaultWorkerSelection(
                worker_id=demo_entry.worker.worker_id,
                worker_loaded=True,
            )

    for entry in getattr(worker_registry, "list_all", lambda: ())():
        worker = getattr(entry, "worker", None)
        if worker is not None and not getattr(worker, "is_service", False):
            return DefaultWorkerSelection(
                worker_id=worker.worker_id,
                worker_loaded=True,
            )

    return DefaultWorkerSelection(worker_id="", worker_loaded=False)
