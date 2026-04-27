"""Registry for slash-style channel commands."""
from __future__ import annotations

from src.common.tenant import TrustLevel

from .models import CommandSpec


class CommandRegistry:
    """Store command specs and resolve aliases/visibility."""

    def __init__(self) -> None:
        self._commands: dict[str, CommandSpec] = {}
        self._aliases: dict[str, str] = {}

    def register(self, spec: CommandSpec) -> None:
        self._commands[spec.name] = spec
        for alias in spec.aliases:
            self._aliases[alias] = spec.name

    def resolve(self, name: str) -> CommandSpec | None:
        canonical = self._aliases.get(name, name)
        return self._commands.get(canonical)

    def list_visible(self, *, channel_type: str, trust_level: str) -> tuple[CommandSpec, ...]:
        current = _trust_value(trust_level)
        visible: list[CommandSpec] = []
        for spec in self._commands.values():
            if spec.hidden:
                continue
            if spec.visibility and channel_type not in spec.visibility:
                continue
            if current < _trust_value(spec.required_trust_level):
                continue
            visible.append(spec)
        return tuple(sorted(visible, key=lambda item: item.name))


def _trust_value(name: str) -> int:
    try:
        return int(getattr(TrustLevel, str(name).upper()))
    except Exception:
        return int(TrustLevel.BASIC)

