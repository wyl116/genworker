"""OpenViking HTTP client and scope helpers."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

from src.common.logger import get_logger

logger = get_logger()


@dataclass(frozen=True)
class OpenVikingHit:
    """Normalized OpenViking search/read payload."""

    id: str
    scope: str
    content: str = ""
    abstract: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    score: float = 0.0
    level: str = "L0"

    @property
    def display_text(self) -> str:
        return self.abstract or self.content


class OpenVikingClient:
    """Thin async HTTP client over the OpenViking API."""

    def __init__(
        self,
        *,
        endpoint: str,
        timeout_seconds: float = 5.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(
            base_url=self._endpoint,
            timeout=timeout_seconds,
        )

    async def health_check(self, *, raise_on_error: bool = False) -> bool:
        try:
            response = await self._client.get("/health")
        except Exception as exc:
            if raise_on_error:
                raise
            logger.warning("[OpenViking] health check failed: %s", exc)
            return False
        if raise_on_error:
            return response.is_success
        try:
            response.raise_for_status()
        except Exception as exc:
            logger.warning("[OpenViking] health check failed: %s", exc)
            return False
        return True

    async def index(
        self,
        *,
        scope: str,
        content: str,
        metadata: dict[str, Any] | None = None,
        item_id: str | None = None,
        level: str = "L1",
    ) -> str:
        payload = {
            "scope": scope,
            "id": item_id,
            "content": content,
            "metadata": metadata or {},
            "level": level,
        }
        data = await self._request("POST", "/v1/index", json=payload)
        resolved = str(data.get("id") or data.get("item_id") or item_id or "")
        if not resolved:
            raise ValueError("OpenViking index response missing document id")
        return resolved

    async def search(
        self,
        *,
        scope: str,
        query: str,
        top_k: int = 5,
        filter: dict[str, Any] | None = None,
        level: str = "L0",
    ) -> tuple[OpenVikingHit, ...]:
        data = await self._request(
            "POST",
            "/v1/search",
            json={
                "scope": scope,
                "query": query,
                "top_k": top_k,
                "filter": filter or {},
                "level": level,
            },
        )
        return tuple(self._coerce_hit(item, default_scope=scope, default_level=level) for item in _results_list(data))

    async def read_many(
        self,
        *,
        scope: str,
        ids: tuple[str, ...],
        level: str = "L1",
    ) -> tuple[OpenVikingHit, ...]:
        if not ids:
            return ()
        data = await self._request(
            "POST",
            "/v1/read_many",
            json={
                "scope": scope,
                "ids": list(ids),
                "level": level,
            },
        )
        return tuple(self._coerce_hit(item, default_scope=scope, default_level=level) for item in _results_list(data))

    async def delete(self, *, scope: str, item_id: str) -> bool:
        data = await self._request(
            "POST",
            "/v1/delete",
            json={"scope": scope, "id": item_id},
        )
        return bool(data.get("deleted", True))

    async def update_metadata(
        self,
        *,
        scope: str,
        item_id: str,
        metadata: dict[str, Any],
    ) -> bool:
        data = await self._request(
            "POST",
            "/v1/update_metadata",
            json={
                "scope": scope,
                "id": item_id,
                "metadata": metadata,
            },
        )
        return bool(data.get("updated", True))

    async def count(self, *, scope: str) -> int:
        data = await self._request(
            "POST",
            "/v1/count",
            json={"scope": scope},
        )
        return int(data.get("count", 0))

    async def list_scope(
        self,
        *,
        scope: str,
        limit: int = 1000,
        offset: int = 0,
    ) -> tuple[OpenVikingHit, ...]:
        data = await self._request(
            "POST",
            "/v1/list",
            json={
                "scope": scope,
                "limit": limit,
                "offset": offset,
            },
        )
        return tuple(self._coerce_hit(item, default_scope=scope, default_level="L1") for item in _results_list(data))

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        response = await self._client.request(method, path, json=json)
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict):
            return payload
        return {"results": payload}

    def _coerce_hit(
        self,
        item: Any,
        *,
        default_scope: str,
        default_level: str,
    ) -> OpenVikingHit:
        if isinstance(item, OpenVikingHit):
            return item
        if not isinstance(item, dict):
            return OpenVikingHit(id=str(item), scope=default_scope, level=default_level)
        return OpenVikingHit(
            id=str(item.get("id") or item.get("item_id") or ""),
            scope=str(item.get("scope") or default_scope),
            content=str(item.get("content") or item.get("text") or ""),
            abstract=str(item.get("abstract") or item.get("snippet") or ""),
            metadata=dict(item.get("metadata") or {}),
            score=float(item.get("score") or item.get("distance") or 0.0),
            level=str(item.get("level") or default_level),
        )


def build_memory_scope(
    scope_prefix: str,
    *,
    tenant_id: str,
    worker_id: str,
    category: str,
) -> str:
    """Build the canonical OpenViking scope for one worker memory partition."""
    prefix = (scope_prefix or "viking://").rstrip("/")
    if prefix.endswith(":"):
        prefix = f"{prefix}/"
    return (
        f"{prefix}/tenant/{tenant_id}/worker/{worker_id}/memories/{category}"
    )


def build_semantic_scope(scope_prefix: str, *, tenant_id: str, worker_id: str) -> str:
    return build_memory_scope(
        scope_prefix,
        tenant_id=tenant_id,
        worker_id=worker_id,
        category="semantic",
    )


def build_episodic_scope(scope_prefix: str, *, tenant_id: str, worker_id: str) -> str:
    return build_memory_scope(
        scope_prefix,
        tenant_id=tenant_id,
        worker_id=worker_id,
        category="episodic",
    )


class EpisodicVikingIndexer:
    """Worker-scoped helper for episodic indexing lifecycle."""

    def __init__(self, client: OpenVikingClient, scope: str) -> None:
        self._client = client
        self._scope = scope

    async def index_episode(self, episode: Any) -> str:
        from src.memory.episodic.store import episode_to_markdown

        return await self._client.index(
            scope=self._scope,
            item_id=str(episode.episode_id),
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
            },
            level="L1",
        )

    async def update_episode_score(self, episode_id: str, score: float) -> bool:
        return await self._client.update_metadata(
            scope=self._scope,
            item_id=episode_id,
            metadata={"score": score},
        )

    async def delete_episode(self, episode_id: str) -> bool:
        return await self._client.delete(scope=self._scope, item_id=episode_id)


def build_episodic_indexer(
    client: OpenVikingClient | None,
    *,
    scope_prefix: str,
    tenant_id: str,
    worker_id: str,
) -> EpisodicVikingIndexer | None:
    if client is None:
        return None
    return EpisodicVikingIndexer(
        client,
        build_episodic_scope(scope_prefix, tenant_id=tenant_id, worker_id=worker_id),
    )


def _results_list(payload: dict[str, Any]) -> list[Any]:
    for key in ("results", "hits", "items", "documents"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
    return []
