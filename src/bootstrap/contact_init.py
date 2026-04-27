"""
Contact registry bootstrap initializer.
"""
from __future__ import annotations

from pathlib import Path

from src.common.logger import get_logger
from src.worker.contacts import ContactRegistry

from .base import Initializer

logger = get_logger()


class ContactInitializer(Initializer):
    """Create per-worker contact registries and seed configured contacts."""

    @property
    def name(self) -> str:
        return "contacts"

    @property
    def depends_on(self) -> list[str]:
        return ["workers"]

    @property
    def priority(self) -> int:
        return 128

    async def initialize(self, context) -> bool:
        workspace_root = Path(context.get_state("workspace_root", "workspace"))
        tenant_id = context.get_state("tenant_id", "demo")
        worker_registry = context.get_state("worker_registry")
        event_bus = context.get_state("event_bus")
        registries: dict[str, ContactRegistry] = {}
        if worker_registry is None:
            context.set_state("contact_registries", registries)
            return True

        for entry in worker_registry.list_all():
            worker_dir = workspace_root / "tenants" / tenant_id / "workers" / entry.worker.worker_id / "contacts"
            registry = ContactRegistry(
                worker_dir,
                event_bus=event_bus,
                config=entry.worker.contacts_config,
            )
            if entry.worker.configured_contacts:
                registry.bootstrap_configured(entry.worker.configured_contacts)
            registries[entry.worker.worker_id] = registry

        context.set_state("contact_registries", registries)
        logger.info(
            "[ContactInit] Initialized %s contact registries",
            len(registries),
        )
        return True

    async def cleanup(self) -> None:
        return None
