"""Contact registry data models."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class PersonIdentity:
    channel_type: str = ""
    handle: str = ""
    display_name: str = ""
    email: str = ""
    source: str = "configured"


@dataclass(frozen=True)
class PersonProfile:
    person_id: str
    primary_name: str
    role: str = ""
    organization: str = ""
    notes: str = ""
    confidence: float = 0.0
    identities: tuple[PersonIdentity, ...] = ()
    source: str = "configured"
    social_circles: tuple[str, ...] = ()
    is_same_org_as_owner: bool = False
    hierarchy_level: str = ""
    merge_history: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    service_count: int = 0
    common_topics: tuple[str, ...] = ()


@dataclass(frozen=True)
class ContactRegistryConfig:
    workspace_root: str = "workspace"
    discovered_dir: str = "discovered"
    configured_dir: str = "configured"
    index_file: str = "index.jsonl"
    context_limit: int = 8
