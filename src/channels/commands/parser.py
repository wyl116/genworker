"""Parser for slash-style commands."""
from __future__ import annotations

import shlex

from .models import CommandMatch
from .registry import _trust_value


class CommandParser:
    """Parse command text with configurable prefix."""

    def __init__(self, registry) -> None:
        self._registry = registry

    def try_parse(
        self,
        *,
        text: str,
        prefix: str = "/",
        channel_type: str = "",
        trust_level: str = "BASIC",
    ) -> CommandMatch | None:
        stripped = str(text or "").strip()
        if not prefix or not stripped.startswith(prefix):
            return None
        body = stripped[len(prefix):].strip()
        if not body:
            return None
        try:
            parts = shlex.split(body)
        except ValueError:
            parts = body.split()
        if not parts:
            return None
        spec = self._registry.resolve(parts[0].lower())
        if spec is None:
            return None
        if spec.visibility and channel_type not in spec.visibility:
            return None
        if _trust_value(trust_level) < _trust_value(spec.required_trust_level):
            return None
        args = {
            "argv": tuple(parts[1:]),
            "raw_args": " ".join(parts[1:]).strip(),
        }
        return CommandMatch(spec=spec, args=args)
