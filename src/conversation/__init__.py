"""Conversation module for thread sessions and task spawning.

Autonomy runtime objects now live under ``src.autonomy``. Compatibility leaf
modules remain available at historical import paths, but are no longer exposed
from the package root.
"""
import importlib

_EXPORTS = {
    "ChatMessage": ".models",
    "ConversationSession": ".models",
    "SessionManager": ".session_manager",
    "FileSessionStore": ".session_store",
    "SessionStore": ".session_store",
    "TaskSpawner": ".task_spawner",
    "SpawnTaskInput": ".task_spawner",
    "SpawnTaskResult": ".task_spawner",
    "SPAWN_TASK_TOOL_SCHEMA": ".task_spawner",
}


def __getattr__(name: str):
    if name not in _EXPORTS:
        raise AttributeError(name)
    module = importlib.import_module(_EXPORTS[name], __name__)
    return getattr(module, name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_EXPORTS))

__all__ = [
    "ChatMessage",
    "ConversationSession",
    "SessionManager",
    "SessionStore",
    "FileSessionStore",
    "TaskSpawner",
    "SpawnTaskInput",
    "SpawnTaskResult",
    "SPAWN_TASK_TOOL_SCHEMA",
]
