"""Typed memory write events shared by orchestrator and providers."""
from __future__ import annotations

from dataclasses import dataclass

from src.memory.episodic.models import Episode


@dataclass(frozen=True)
class EpisodeWritePayload:
    """Payload for episodic memory mirroring."""

    tenant_id: str
    worker_id: str
    episode: Episode

    def scan_text(self) -> str:
        return "\n".join((self.episode.summary, *self.episode.key_findings))


@dataclass(frozen=True)
class SemanticFactWritePayload:
    """Payload for semantic rule/fact indexing."""

    tenant_id: str
    worker_id: str
    rule: str
    reason: str = ""

    def scan_text(self) -> str:
        return "\n".join(part for part in (self.rule, self.reason) if part)


@dataclass(frozen=True)
class PreferenceWritePayload:
    """Payload for preference memory mirroring."""

    tenant_id: str
    worker_id: str
    content: str

    def scan_text(self) -> str:
        return self.content


@dataclass(frozen=True)
class DecisionWritePayload:
    """Payload for decision memory mirroring."""

    tenant_id: str
    worker_id: str
    decision: str

    def scan_text(self) -> str:
        return self.decision


MemoryWritePayload = (
    EpisodeWritePayload
    | SemanticFactWritePayload
    | PreferenceWritePayload
    | DecisionWritePayload
)

_TARGET_TO_PAYLOAD = {
    "episode": EpisodeWritePayload,
    "semantic_fact": SemanticFactWritePayload,
    "preference": PreferenceWritePayload,
    "decision": DecisionWritePayload,
}


@dataclass(frozen=True)
class MemoryWriteEvent:
    """Cross-subsystem memory write notification."""

    action: str
    target: str
    entity_id: str
    content: MemoryWritePayload
    source_subsystem: str
    occurred_at: str

    def __post_init__(self) -> None:
        expected_type = _TARGET_TO_PAYLOAD.get(self.target)
        if expected_type is None:
            raise ValueError(f"unsupported memory write target: {self.target}")
        if not isinstance(self.content, expected_type):
            raise TypeError(
                f"memory write target {self.target} requires {expected_type.__name__}, "
                f"got {type(self.content).__name__}",
            )
        if not str(self.action or "").strip():
            raise ValueError("memory write event requires non-empty action")
        if not str(self.entity_id or "").strip():
            raise ValueError("memory write event requires non-empty entity_id")
        if not str(self.source_subsystem or "").strip():
            raise ValueError("memory write event requires non-empty source_subsystem")
        if not str(self.occurred_at or "").strip():
            raise ValueError("memory write event requires non-empty occurred_at")
        if not str(self.content.tenant_id or "").strip():
            raise ValueError("memory write event requires non-empty tenant_id")
        if not str(self.content.worker_id or "").strip():
            raise ValueError("memory write event requires non-empty worker_id")

    @property
    def tenant_id(self) -> str:
        return self.content.tenant_id

    @property
    def worker_id(self) -> str:
        return self.content.worker_id

    def scan_text(self) -> str:
        return self.content.scan_text()
