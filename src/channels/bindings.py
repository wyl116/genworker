"""Worker-scoped channel binding helpers."""
from __future__ import annotations

from typing import Any

from src.channels.models import build_channel_binding
from src.common.logger import get_logger

logger = get_logger()


def build_worker_bindings(
    entry,
    *,
    tenant_id: str,
    platform_client_factory: Any | None = None,
) -> list:
    """Build validated channel bindings for one worker entry."""
    bindings = []
    for raw in entry.worker.channels:
        binding = build_channel_binding(
            raw,
            tenant_id=tenant_id,
            worker_id=entry.worker.worker_id,
        )
        if not binding.channel_type:
            continue
        if not binding.chat_ids and binding.channel_type != "slack":
            continue
        if platform_client_factory is not None:
            client = platform_client_factory.get_client(
                tenant_id,
                entry.worker.worker_id,
                binding.channel_type,
            )
            if client is None:
                logger.warning(
                    "[ChannelBindings] Skip binding worker=%s tenant=%s channel=%s: missing credentials",
                    entry.worker.worker_id,
                    tenant_id,
                    binding.channel_type,
                )
                continue
        bindings.append(binding)
    return bindings
