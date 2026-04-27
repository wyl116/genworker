"""Stable prefix cache for system prompt assembly."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass


@dataclass(frozen=True)
class StablePrefix:
    text: str
    token_count: int
    signature: str


class StablePrefixCache:
    """Cache stable prompt prefixes keyed by worker/skill/signature."""

    def __init__(self) -> None:
        self._cache: dict[tuple[str, str, str], StablePrefix] = {}

    def get_or_build(
        self,
        *,
        worker_id: str,
        skill_id: str,
        identity: str,
        principles: str,
        constraints: str,
        directives: str,
        token_counter,
    ) -> StablePrefix:
        signature = _build_signature(identity, principles, constraints, directives)
        key = (worker_id, skill_id, signature)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        text = "\n\n".join(
            part.strip()
            for part in (identity, principles, constraints, directives)
            if str(part).strip()
        )
        stable = StablePrefix(
            text=text,
            token_count=token_counter(text) if text else 0,
            signature=signature,
        )
        self._cache[key] = stable
        return stable


def _build_signature(identity: str, principles: str, constraints: str, directives: str) -> str:
    raw = "\n\x1e".join((identity, principles, constraints, directives)).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()

