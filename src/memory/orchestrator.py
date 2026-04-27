"""Unified memory orchestration across multiple providers."""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, TypeVar
from uuid import uuid4

from src.common.content_scanner import scan
from src.common.logger import get_logger
from src.events.models import Event
from src.memory.episodic.models import EpisodeIndex
from src.memory.provider import MemoryProvider, MemoryRecallResult
from src.memory.write_models import MemoryWriteEvent

logger = get_logger()
_T = TypeVar("_T")


@dataclass(frozen=True)
class MemoryQueryResult:
    """Unified memory query payload returned to callers."""

    semantic_results: tuple[dict[str, Any], ...] = ()
    episodic_results: tuple[EpisodeIndex, ...] = ()
    preference_results: tuple[Any, ...] = ()
    merged_context: str = ""


@dataclass(frozen=True)
class MemoryBudgetConfig:
    """Static ratio allocation per provider."""

    ratios: tuple[tuple[str, float], ...] = (
        ("semantic", 0.4),
        ("episodic", 0.4),
        ("preference", 0.2),
    )
    default_ratio: float = 0.2

    def get_ratio(self, provider_name: str) -> float:
        for name, ratio in self.ratios:
            if name == provider_name:
                return ratio
        return self.default_ratio


class MemoryOrchestrator:
    """Coordinates retrieval and lifecycle calls across memory providers."""

    def __init__(
        self,
        providers: tuple[MemoryProvider, ...] = (),
        event_bus: Any | None = None,
        seen_write_limit: int = 10000,
    ) -> None:
        self._providers = providers
        self._event_bus = event_bus
        self._seen_write_limit = max(1, int(seen_write_limit))
        self._seen_writes: set[tuple[str, str, str, str, str, str]] = set()
        self._seen_write_order: deque[tuple[str, str, str, str, str, str]] = deque()
        self._inflight_writes: dict[
            tuple[str, str, str, str, str, str],
            asyncio.Event,
        ] = {}

    @property
    def providers(self) -> tuple[MemoryProvider, ...]:
        return self._providers

    def add_provider(self, provider: MemoryProvider) -> None:
        """Register a provider dynamically."""
        self._providers = (*self._providers, provider)

    async def query(
        self,
        text: str,
        worker_id: str,
        token_budget: int = 2000,
        trust_gate: Any | None = None,
        **kwargs: Any,
    ) -> MemoryQueryResult:
        active_providers = self._filter_by_trust(self._providers, trust_gate)
        results = await asyncio.gather(*(
            self._safe_call(
                provider.name,
                provider.query(text, worker_id, token_budget=token_budget, **kwargs),
                MemoryRecallResult(source=provider.name),
            )
            for provider in active_providers
        ))
        return self._merge(results, token_budget)

    async def on_pre_compress(
        self,
        messages: tuple[dict[str, Any], ...],
    ) -> None:
        await asyncio.gather(*(
            self._safe_call(provider.name, provider.on_pre_compress(messages), None)
            for provider in self._providers
        ))

    async def on_session_end(
        self,
        messages: tuple[dict[str, Any], ...],
    ) -> None:
        await asyncio.gather(*(
            self._safe_call(provider.name, provider.on_session_end(messages), None)
            for provider in self._providers
        ))

    async def on_memory_write(self, event: MemoryWriteEvent) -> None:
        dedupe_key = (
            event.tenant_id,
            event.worker_id,
            event.action,
            event.target,
            event.entity_id,
            event.occurred_at,
        )
        while True:
            if dedupe_key in self._seen_writes:
                return
            inflight_event = self._inflight_writes.get(dedupe_key)
            if inflight_event is None:
                inflight_event = asyncio.Event()
                self._inflight_writes[dedupe_key] = inflight_event
                break
            await inflight_event.wait()

        result = scan(event.scan_text())
        if not result.is_safe:
            logger.warning(
                "[MemoryOrchestrator] blocked unsafe memory mirror for %s: %s",
                event.entity_id,
                ", ".join(result.violations),
            )
            inflight_event.set()
            self._inflight_writes.pop(dedupe_key, None)
            return

        try:
            matched_providers = tuple(
                provider for provider in self._providers if provider.accepts(event)
            )
            provider_statuses = await asyncio.gather(*(
                self._safe_call_status(provider.name, provider.write(event))
                for provider in matched_providers
            ))
            if matched_providers:
                write_succeeded = all(provider_statuses)
            else:
                write_succeeded = not self._requires_provider_sink(event)
            if write_succeeded and self._event_bus is not None:
                await self._safe_call_status(
                    "event_bus",
                    self._event_bus.publish(Event(
                        event_id=uuid4().hex,
                        type="memory.written",
                        source="memory_orchestrator",
                        tenant_id=event.tenant_id,
                        payload=tuple(sorted({
                            "action": event.action,
                            "target": event.target,
                            "entity_id": event.entity_id,
                            "source_subsystem": event.source_subsystem,
                            "worker_id": event.worker_id,
                        }.items())),
                        timestamp=datetime.now(timezone.utc).isoformat(),
                    )),
                )

            if write_succeeded:
                self._remember_seen_write(dedupe_key)
        finally:
            inflight_event.set()
            self._inflight_writes.pop(dedupe_key, None)

    async def _safe_call(
        self,
        name: str,
        coro: Any,
        default: _T,
    ) -> _T:
        """Execute a provider call with timeout and fault isolation."""
        try:
            return await asyncio.wait_for(coro, timeout=5.0)
        except Exception as exc:
            logger.warning("[MemoryOrchestrator] provider %s failed: %s", name, exc)
            return default

    async def _safe_call_status(
        self,
        name: str,
        coro: Any,
    ) -> bool:
        try:
            await asyncio.wait_for(coro, timeout=5.0)
            return True
        except Exception as exc:
            logger.warning("[MemoryOrchestrator] provider %s failed: %s", name, exc)
            return False

    def _requires_provider_sink(self, event: MemoryWriteEvent) -> bool:
        return event.target == "semantic_fact"

    def _remember_seen_write(self, dedupe_key: tuple[str, str, str, str, str, str]) -> None:
        if dedupe_key in self._seen_writes:
            return
        self._seen_writes.add(dedupe_key)
        self._seen_write_order.append(dedupe_key)
        while len(self._seen_write_order) > self._seen_write_limit:
            expired_key = self._seen_write_order.popleft()
            self._seen_writes.discard(expired_key)

    def _filter_by_trust(
        self,
        providers: tuple[MemoryProvider, ...],
        gate: Any | None,
    ) -> tuple[MemoryProvider, ...]:
        if gate is None:
            return providers
        allowed: list[MemoryProvider] = []
        for provider in providers:
            if provider.name == "semantic" and not getattr(gate, "semantic_search_enabled", False):
                continue
            allowed.append(provider)
        return tuple(allowed)

    def _merge(
        self,
        results: tuple[MemoryRecallResult, ...],
        token_budget: int,
        budget_config: MemoryBudgetConfig = MemoryBudgetConfig(),
    ) -> MemoryQueryResult:
        merged_parts: list[str] = []
        semantic_results: tuple[dict[str, Any], ...] = ()
        episodic_results: tuple[EpisodeIndex, ...] = ()
        preference_results: tuple[Any, ...] = ()

        for result in results:
            provider_budget = int(token_budget * budget_config.get_ratio(result.source))
            truncated = self._truncate_to_budget(result.items, provider_budget)
            if truncated:
                header = "[Historical Context]" if result.source == "episodic" else f"[{result.source}]"
                merged_parts.append(f"{header}\n" + "\n".join(truncated))
            if result.source == "semantic":
                semantic_results = tuple(item for item in result.raw_items if isinstance(item, dict))
            elif result.source == "episodic":
                episodic_results = tuple(
                    item for item in result.raw_items if isinstance(item, EpisodeIndex)
                )
            elif result.source == "preference":
                preference_results = result.raw_items

        return MemoryQueryResult(
            semantic_results=semantic_results,
            episodic_results=episodic_results,
            preference_results=preference_results,
            merged_context="\n\n".join(part for part in merged_parts if part),
        )

    def _truncate_to_budget(
        self,
        items: tuple[str, ...],
        token_budget: int,
    ) -> tuple[str, ...]:
        if token_budget <= 0:
            return ()
        selected: list[str] = []
        used = 0
        for item in items:
            estimate = max(1, len(item) // 4)
            if selected and used + estimate > token_budget:
                break
            if not selected and estimate > token_budget:
                selected.append(item[: token_budget * 4])
                break
            selected.append(item)
            used += estimate
        return tuple(selected)
