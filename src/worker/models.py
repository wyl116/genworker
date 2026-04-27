"""
Worker data models - all frozen dataclasses for immutability.

Defines:
- WorkerMode: the worker's operating mode
- WorkerIdentity: name, role, department, background, personality
- WorkerToolPolicy: blacklist/whitelist with frozenset
- Worker: complete worker definition parsed from PERSONA.md
"""
from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping

from src.worker.contacts.models import ContactRegistryConfig, PersonProfile


@dataclass(frozen=True)
class WorkerPersonality:
    """Worker personality traits and communication style."""
    traits: tuple[str, ...] = ()
    communication_style: str = ""
    decision_style: str = ""


@dataclass(frozen=True)
class WorkerIdentity:
    """Worker identity information from PERSONA.md."""
    name: str = ""
    worker_id: str = ""
    version: str = "1.0"
    role: str = ""
    department: str = ""
    reports_to: str = ""
    background: str = ""
    personality: WorkerPersonality = WorkerPersonality()
    principles: tuple[str, ...] = ()


@dataclass(frozen=True)
class WorkerToolPolicy:
    """
    Worker-level tool access policy using frozenset for immutability.

    mode: "blacklist" (deny listed) or "whitelist" (allow listed only).
    """
    mode: str = "blacklist"
    denied_tools: frozenset[str] = field(default_factory=frozenset)
    allowed_tools: frozenset[str] = field(default_factory=frozenset)


class WorkerMode(str, Enum):
    """Top-level worker operating mode from PERSONA.md."""

    PERSONAL = "personal"
    TEAM_MEMBER = "team_member"
    SERVICE = "service"


@dataclass(frozen=True)
class ServiceConfig:
    """Service-mode configuration from PERSONA.md."""

    knowledge_sources: tuple[Mapping, ...] = ()
    session_ttl: int = 1800
    max_concurrent_sessions: int = 50
    anonymous_allowed: bool = True
    escalation_enabled: bool = False
    escalation_target: str = ""
    escalation_triggers: tuple[str, ...] = ()


@dataclass(frozen=True)
class WorkerHeartbeatConfig:
    """Worker-scoped heartbeat strategy overrides."""

    goal_task_actions: tuple[str, ...] = ()
    goal_isolated_actions: tuple[str, ...] = ()
    goal_isolated_deviation_threshold: float | None = None


@dataclass(frozen=True)
class Worker:
    """
    Complete worker definition parsed from PERSONA.md.

    Immutable - use dataclasses.replace() for modifications.
    """
    identity: WorkerIdentity = WorkerIdentity()
    mode: WorkerMode = WorkerMode.PERSONAL
    service_config: ServiceConfig | None = None
    heartbeat_config: WorkerHeartbeatConfig = WorkerHeartbeatConfig()
    tool_policy: WorkerToolPolicy = WorkerToolPolicy()
    skills_dir: str = "skills/"
    default_skill: str = ""
    constraints: tuple[str, ...] = ()
    triggers: tuple[Mapping, ...] = ()
    sensor_configs: tuple[Mapping, ...] = ()
    channels: tuple[Mapping, ...] = ()
    configured_contacts: tuple[PersonProfile, ...] = ()
    contacts_config: ContactRegistryConfig = ContactRegistryConfig()
    body_instructions: str = ""
    source_path: str = ""

    @property
    def worker_id(self) -> str:
        """Shortcut to identity.worker_id."""
        return self.identity.worker_id

    @property
    def name(self) -> str:
        """Shortcut to identity.name."""
        return self.identity.name

    @property
    def principles(self) -> tuple[str, ...]:
        """Shortcut to identity.principles."""
        return self.identity.principles

    @property
    def is_personal(self) -> bool:
        """Whether the worker runs in personal assistant mode."""
        return self.mode == WorkerMode.PERSONAL

    @property
    def is_team_member(self) -> bool:
        """Whether the worker runs in team-member mode."""
        return self.mode == WorkerMode.TEAM_MEMBER

    @property
    def is_service(self) -> bool:
        """Whether the worker runs in service mode."""
        return self.mode == WorkerMode.SERVICE
