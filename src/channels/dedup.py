"""Router-level inbound message deduplication."""
from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from src.common.logger import get_logger
from src.common.runtime_status import ComponentRuntimeStatus, ComponentStatus

logger = get_logger()


@dataclass(frozen=True)
class DeduplicatorConfig:
    """Configuration for message deduplication."""

    ttl_seconds: int = 600
    redis_key_prefix: str = "lw:msg_dedup"
    memory_max_size: int = 5000


class MessageDeduplicator:
    """Redis-primary message deduplication with in-memory fallback."""

    def __init__(
        self,
        redis_client: Any | None = None,
        config: DeduplicatorConfig = DeduplicatorConfig(),
    ) -> None:
        self._redis = redis_client
        self._config = config
        self._memory: OrderedDict[str, float] = OrderedDict()
        self._status = ComponentStatus.READY
        self._last_error = ""
        self._selected_backend = "redis" if redis_client is not None else "memory"

    def runtime_status(self) -> ComponentRuntimeStatus:
        return ComponentRuntimeStatus(
            component="message_dedup",
            enabled=True,
            status=self._status,
            selected_backend=self._selected_backend,
            primary_backend="redis",
            fallback_backend="memory",
            ground_truth="memory",
            last_error=self._last_error,
        )

    async def is_duplicate(self, channel_type: str, message_id: str) -> bool:
        """Return True when the platform message was already processed."""
        if not channel_type or not message_id:
            return False

        dedup_key = self._dedup_key(channel_type, message_id)
        if self._redis is not None:
            redis_result = await self._check_redis(dedup_key)
            if redis_result is not None:
                return redis_result

        return self._check_memory(dedup_key)

    async def _check_redis(self, dedup_key: str) -> bool | None:
        try:
            existing = await self._redis.get(dedup_key)
            if existing is not None:
                return True
            created = await self._redis.set(
                dedup_key,
                "1",
                ttl=self._config.ttl_seconds,
                nx=True,
            )
            if created:
                return False
        except Exception as exc:
            self._status = ComponentStatus.DEGRADED
            self._selected_backend = "memory"
            self._last_error = str(exc).splitlines()[0][:200]
            logger.warning(
                "[MessageDeduplicator] Redis dedup failed, fallback to memory: %s",
                self._last_error,
            )
        return None

    def _check_memory(self, dedup_key: str) -> bool:
        now = time.time()
        self._evict_expired(now)
        expires_at = self._memory.get(dedup_key)
        if expires_at is not None and expires_at > now:
            self._memory.move_to_end(dedup_key)
            return True
        self._memory[dedup_key] = now + max(self._config.ttl_seconds, 1)
        self._memory.move_to_end(dedup_key)
        self._enforce_capacity()
        return False

    def _evict_expired(self, now: float) -> None:
        expired_keys = [
            key for key, expires_at in self._memory.items()
            if expires_at <= now
        ]
        for key in expired_keys:
            self._memory.pop(key, None)

    def _enforce_capacity(self) -> None:
        while len(self._memory) > max(self._config.memory_max_size, 1):
            self._memory.popitem(last=False)

    def _dedup_key(self, channel_type: str, message_id: str) -> str:
        return (
            f"{self._config.redis_key_prefix}:"
            f"{channel_type.strip().lower()}:{message_id.strip()}"
        )
