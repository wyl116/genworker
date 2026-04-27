"""Decorator-driven registration for pure builtin tool factories."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class ToolFactorySpec:
    factory: Callable[..., Any]
    name: str
    requires: tuple[str, ...] = ()
    multi: bool = False
    priority: int = 100
    enabled: bool = True


_FACTORY_REGISTRY: list[ToolFactorySpec] = []


def builtin_tool(
    *,
    requires: tuple[str, ...] = (),
    multi: bool = False,
    priority: int = 100,
    enabled: bool = True,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register a pure builtin tool factory."""

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        _FACTORY_REGISTRY.append(
            ToolFactorySpec(
                factory=fn,
                name=fn.__name__,
                requires=requires,
                multi=multi,
                priority=priority,
                enabled=enabled,
            )
        )
        return fn

    return decorator


def get_registered_factories() -> tuple[ToolFactorySpec, ...]:
    return tuple(
        sorted(
            (item for item in _FACTORY_REGISTRY if item.enabled),
            key=lambda item: item.priority,
        )
    )


def clear_registry() -> None:
    _FACTORY_REGISTRY.clear()

