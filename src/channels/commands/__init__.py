"""Command registry components for channel message routing."""
import importlib

_EXPORTS = {
    "build_builtin_command_registry": ".builtin",
    "CommandDispatcher": ".dispatcher",
    "CommandContext": ".models",
    "CommandMatch": ".models",
    "CommandSpec": ".models",
    "CommandParser": ".parser",
    "CommandRegistry": ".registry",
}


def __getattr__(name: str):
    if name not in _EXPORTS:
        raise AttributeError(name)
    module = importlib.import_module(_EXPORTS[name], __name__)
    return getattr(module, name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_EXPORTS))

__all__ = [
    "build_builtin_command_registry",
    "CommandContext",
    "CommandDispatcher",
    "CommandMatch",
    "CommandParser",
    "CommandRegistry",
    "CommandSpec",
]
