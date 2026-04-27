"""Autonomy runtime primitives: inbox, main-session and isolated runs."""
import importlib

_EXPORTS = {
    "InboxItem": ".inbox",
    "InboxStatus": ".inbox",
    "SessionInboxStore": ".inbox",
    "IsolatedRunManager": ".isolated_run",
    "MainSessionRuntime": ".main_session",
}


def __getattr__(name: str):
    if name not in _EXPORTS:
        raise AttributeError(name)
    module = importlib.import_module(_EXPORTS[name], __name__)
    return getattr(module, name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_EXPORTS))

__all__ = [
    "InboxItem",
    "InboxStatus",
    "IsolatedRunManager",
    "MainSessionRuntime",
    "SessionInboxStore",
]
