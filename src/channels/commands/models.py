"""Data models for channel command routing."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping


@dataclass(frozen=True)
class CommandSpec:
    name: str
    description: str
    handler: Callable[[Any], Awaitable[Any]]
    required_trust_level: str = "BASIC"
    visibility: frozenset[str] = field(default_factory=frozenset)
    require_mention: bool = False
    aliases: frozenset[str] = field(default_factory=frozenset)
    hidden: bool = False


@dataclass(frozen=True)
class CommandMatch:
    spec: CommandSpec
    args: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CommandContext:
    message: Any
    binding: Any
    tenant: Any
    args: Mapping[str, Any]
    session_manager: Any
    thread_id: str
    registry: Any = None
    event_bus: Any = None
    suggestion_store: Any = None
    feedback_store: Any = None
    inbox_store: Any = None
    trigger_managers: Any = None
    worker_schedulers: Any = None
    task_store: Any = None
    workspace_root: Any = None
    llm_client: Any = None
    lifecycle_services: Any = None
    worker_router: Any = None
    engine_dispatcher: Any = None
