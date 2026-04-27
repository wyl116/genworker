"""Memory provider abstractions for orchestrated memory retrieval."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.common.logger import get_logger
from src.memory.episodic.models import EpisodeIndex
from src.memory.preferences.extractor import (
    format_decisions_for_prompt,
    format_preferences_for_prompt,
    load_active_decisions,
    load_preferences,
)
from src.memory.backends.openviking import (
    OpenVikingClient,
    OpenVikingHit,
    build_episodic_scope,
    build_semantic_scope,
)
from src.memory.write_models import (
    DecisionWritePayload,
    EpisodeWritePayload,
    MemoryWriteEvent,
    PreferenceWritePayload,
    SemanticFactWritePayload,
)

logger = get_logger()


@dataclass(frozen=True)
class MemoryRecallResult:
    """Normalized retrieval payload returned by a single provider."""

    source: str
    items: tuple[str, ...] = ()
    token_estimate: int = 0
    raw_items: tuple[Any, ...] = ()


class MemoryProvider(ABC):
    """Common interface implemented by all memory providers."""

    name: str = "memory"
    accepts_targets: tuple[str, ...] = ()

    @abstractmethod
    async def query(
        self,
        text: str,
        worker_id: str,
        **kwargs: Any,
    ) -> MemoryRecallResult:
        raise NotImplementedError

    async def on_pre_compress(
        self,
        messages: tuple[dict[str, Any], ...],
    ) -> None:
        return None

    async def on_session_end(
        self,
        messages: tuple[dict[str, Any], ...],
    ) -> None:
        return None

    def accepts(self, event: MemoryWriteEvent) -> bool:
        if event.target not in self.accepts_targets:
            return False
        if type(self).write is MemoryProvider.write:
            logger.warning(
                "[MemoryProvider] %s declares accepts_targets=%s but does not implement write()",
                type(self).__name__,
                self.accepts_targets,
            )
            return False
        return True

    async def write(self, event: MemoryWriteEvent) -> None:
        return None


class SemanticMemoryProvider(MemoryProvider):
    """Semantic search backed by OpenViking."""

    name = "semantic"
    accepts_targets = ("semantic_fact", "preference", "decision")

    def __init__(
        self,
        client: OpenVikingClient | None,
        *,
        scope_prefix: str = "viking://",
    ) -> None:
        self._client = client
        self._scope_prefix = scope_prefix

    def accepts(self, event: MemoryWriteEvent) -> bool:
        return self._client is not None and super().accepts(event)

    async def query(
        self,
        text: str,
        worker_id: str,
        **kwargs: Any,
    ) -> MemoryRecallResult:
        if self._client is None:
            return MemoryRecallResult(source=self.name)

        tenant_id = str(kwargs.get("tenant_id", "") or "default")
        scope = build_semantic_scope(
            self._scope_prefix,
            tenant_id=tenant_id,
            worker_id=worker_id,
        )
        hits = await self._client.search(
            scope=scope,
            query=text,
            top_k=int(kwargs.get("semantic_limit", 5)),
            filter={},
            level="L0",
        )
        threshold = float(kwargs.get("semantic_threshold", 0.0))
        filtered_hits = tuple(
            hit for hit in hits if hit.score >= threshold or hit.score == 0.0
        )
        provider_budget = int(int(kwargs.get("token_budget", 0) or 0) * 0.4)
        items, raw_items = await _resolve_viking_items(
            client=self._client,
            scope=scope,
            hits=filtered_hits,
            provider_budget=provider_budget,
            formatter=_format_semantic_item,
        )
        return MemoryRecallResult(
            source=self.name,
            items=items,
            token_estimate=sum(_estimate_tokens(item) for item in items),
            raw_items=tuple(
                {
                    "memory_id": hit.id,
                    "content": hit.display_text,
                    "score": hit.score,
                    "metadata": hit.metadata,
                }
                for hit in raw_items
            ),
        )

    async def write(self, event: MemoryWriteEvent) -> None:
        if self._client is None:
            return None

        payload = event.content
        if isinstance(payload, SemanticFactWritePayload):
            content = "\n".join(part for part in (
                f"Rule: {payload.rule}",
                f"Reason: {payload.reason}" if payload.reason else "",
            ) if part)
            metadata = {
                "target": event.target,
                "entity_id": event.entity_id,
                "rule": payload.rule,
                "reason": payload.reason,
                "source_subsystem": event.source_subsystem,
                "occurred_at": event.occurred_at,
            }
        elif isinstance(payload, PreferenceWritePayload):
            content = payload.content
            metadata = {
                "target": event.target,
                "entity_id": event.entity_id,
                "preference": payload.content,
                "source_subsystem": event.source_subsystem,
                "occurred_at": event.occurred_at,
            }
        elif isinstance(payload, DecisionWritePayload):
            content = payload.decision
            metadata = {
                "target": event.target,
                "entity_id": event.entity_id,
                "decision": payload.decision,
                "source_subsystem": event.source_subsystem,
                "occurred_at": event.occurred_at,
            }
        else:
            return None

        await self._client.index(
            scope=build_semantic_scope(
                self._scope_prefix,
                tenant_id=payload.tenant_id or "default",
                worker_id=payload.worker_id,
            ),
            item_id=event.entity_id,
            content=content,
            metadata=metadata,
            level="L1",
        )


class EpisodicMemoryProvider(MemoryProvider):
    """OpenViking-backed episodic retrieval with metadata filters."""

    name = "episodic"
    accepts_targets = ("episode",)

    def __init__(
        self,
        client: OpenVikingClient | None,
        *,
        base_dir: Path | None = None,
        scope_prefix: str = "viking://",
    ) -> None:
        self._client = client
        self._base_dir = base_dir
        self._scope_prefix = scope_prefix

    def accepts(self, event: MemoryWriteEvent) -> bool:
        return self._client is not None and super().accepts(event)

    async def query(
        self,
        text: str,
        worker_id: str,
        **kwargs: Any,
    ) -> MemoryRecallResult:
        if self._client is None:
            return MemoryRecallResult(source=self.name)

        tenant_id = str(kwargs.get("tenant_id", "") or "default")
        scope = build_episodic_scope(
            self._scope_prefix,
            tenant_id=tenant_id,
            worker_id=worker_id,
        )
        filter_payload = {
            key: value
            for key, value in {
                "skill_id": kwargs.get("skill_id"),
                "goal_id": kwargs.get("goal_id"),
                "duty_id": kwargs.get("duty_id"),
            }.items()
            if value
        }
        hits = await self._client.search(
            scope=scope,
            query=text,
            top_k=int(kwargs.get("episodic_top_k", 5)),
            filter=filter_payload,
            level="L0",
        )
        provider_budget = int(int(kwargs.get("token_budget", 0) or 0) * 0.4)
        items, raw_items = await _resolve_viking_items(
            client=self._client,
            scope=scope,
            hits=hits,
            provider_budget=provider_budget,
            formatter=_format_episodic_item,
        )
        return MemoryRecallResult(
            source=self.name,
            items=items,
            token_estimate=sum(_estimate_tokens(item) for item in items),
            raw_items=tuple(_episode_index_from_hit(hit) for hit in raw_items),
        )

    def _resolve_dir(self, kwargs: dict[str, Any]) -> Path | None:
        base_dir = kwargs.get("episodic_base_dir")
        if isinstance(base_dir, Path):
            return base_dir
        return self._base_dir

    async def write(self, event: MemoryWriteEvent) -> None:
        if self._client is None:
            return None

        payload = event.content
        if not isinstance(payload, EpisodeWritePayload):
            return None

        from src.memory.episodic.store import episode_to_markdown

        episode = payload.episode
        await self._client.index(
            scope=build_episodic_scope(
                self._scope_prefix,
                tenant_id=payload.tenant_id or "default",
                worker_id=payload.worker_id,
            ),
            item_id=event.entity_id,
            content=episode_to_markdown(episode),
            metadata={
                "episode_id": episode.episode_id,
                "summary": episode.summary,
                "ts": episode.created_at,
                "score": episode.relevance_score,
                "skill_id": episode.source.skill_used,
                "skills": [episode.source.skill_used] if episode.source.skill_used else [],
                "goal_id": episode.related_goals[0] if episode.related_goals else "",
                "goal_ids": list(episode.related_goals),
                "duty_id": episode.related_duties[0] if episode.related_duties else "",
                "duty_ids": list(episode.related_duties),
                "entities": [entity.value for entity in episode.related_entities],
                "entity_types": [entity.type for entity in episode.related_entities],
                "source_type": episode.source.type,
                "target": event.target,
                "source_subsystem": event.source_subsystem,
                "occurred_at": event.occurred_at,
            },
            level="L1",
        )


class PreferenceMemoryProvider(MemoryProvider):
    """Loads persisted user preferences and active decisions."""

    name = "preference"

    async def query(
        self,
        text: str,
        worker_id: str,
        **kwargs: Any,
    ) -> MemoryRecallResult:
        worker_dir = kwargs.get("worker_dir")
        if not isinstance(worker_dir, Path):
            return MemoryRecallResult(source=self.name)

        preferences_path = worker_dir / "preferences.jsonl"
        decisions_path = worker_dir / "decisions.jsonl"

        items: list[str] = []
        raw_items: list[Any] = []

        if preferences_path.exists():
            preferences = load_preferences(preferences_path)
            if preferences:
                items.append(format_preferences_for_prompt(preferences))
                raw_items.extend(preferences)

        if decisions_path.exists():
            decisions = load_active_decisions(decisions_path)
            if decisions:
                items.append(format_decisions_for_prompt(decisions))
                raw_items.extend(decisions)

        return MemoryRecallResult(
            source=self.name,
            items=tuple(item for item in items if item),
            token_estimate=sum(_estimate_tokens(item) for item in items),
            raw_items=tuple(raw_items),
        )


def _extract_keywords(task: str) -> tuple[str, ...]:
    """Simple keyword extraction shared by episodic retrieval."""
    stop_words = frozenset({
        "the", "a", "an", "is", "are", "was", "were", "be", "been",
        "to", "and", "or", "in", "of", "for", "with", "on", "at",
        "by", "not", "do", "does", "did", "this", "that", "it",
        "i", "you", "we", "they", "he", "she", "my", "your",
    })
    words = task.lower().split()
    return tuple(word for word in words if len(word) > 2 and word not in stop_words)


def _estimate_tokens(text: str) -> int:
    """Cheap token estimate used for budget slicing."""
    return max(1, len(text) // 4) if text else 0


async def _resolve_viking_items(
    *,
    client: OpenVikingClient,
    scope: str,
    hits: tuple[OpenVikingHit, ...],
    provider_budget: int,
    formatter,
) -> tuple[tuple[str, ...], tuple[OpenVikingHit, ...]]:
    if not hits:
        return (), ()
    l0_total = sum(_estimate_tokens(hit.display_text) for hit in hits)
    resolved_hits = hits
    if provider_budget > 0 and l0_total * 2 <= provider_budget:
        details = await client.read_many(
            scope=scope,
            ids=tuple(hit.id for hit in hits),
            level="L1",
        )
        if details:
            resolved_hits = details
    return tuple(formatter(hit) for hit in resolved_hits), tuple(resolved_hits)


def _format_semantic_item(hit: OpenVikingHit) -> str:
    text = hit.display_text.strip()
    return text


def _format_episodic_item(hit: OpenVikingHit) -> str:
    metadata = hit.metadata
    ts = str(metadata.get("ts") or metadata.get("created_at") or "")
    summary = str(metadata.get("summary") or hit.display_text or "").strip()
    score = float(metadata.get("score") or hit.score or 0.0)
    if ts:
        return f"- [{ts}] {summary} (relevance: {score:.2f})"
    return f"- {summary} (relevance: {score:.2f})"


def _episode_index_from_hit(hit: OpenVikingHit) -> EpisodeIndex:
    metadata = hit.metadata
    return EpisodeIndex(
        id=str(metadata.get("episode_id") or hit.id),
        ts=str(metadata.get("ts") or metadata.get("created_at") or ""),
        summary=str(metadata.get("summary") or hit.display_text or ""),
        entities=tuple(str(item) for item in metadata.get("entities", ()) or ()),
        skills=tuple(str(item) for item in metadata.get("skills", ()) or ()),
        duties=tuple(str(item) for item in metadata.get("duty_ids", ()) or ()),
        goals=tuple(str(item) for item in metadata.get("goal_ids", ()) or ()),
        score=float(metadata.get("score") or hit.score or 0.0),
    )
