"""Base helpers shared by concrete sensor implementations."""
from __future__ import annotations

import re
from typing import Any

from .config import RoutingRule
from .protocol import FactCallback, SensedFact


class SensorBase:
    """Common routing-rule evaluation and callback wiring."""

    def __init__(
        self,
        *,
        routing_rules: tuple[RoutingRule, ...] = (),
        fallback_route: str = "heartbeat",
    ) -> None:
        self._routing_rules = routing_rules
        self._fallback_route = fallback_route
        self._fact_callback: FactCallback | None = None

    def set_fact_callback(self, callback: FactCallback) -> None:
        self._fact_callback = callback

    async def poll(self) -> tuple[SensedFact, ...]:
        return ()

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    def get_snapshot(self) -> dict[str, Any]:
        return {}

    def restore_snapshot(self, snapshot: dict[str, Any]) -> None:
        return None

    def _classify_route(self, payload: tuple[tuple[str, Any], ...]) -> str:
        payload_map = dict(payload)
        for rule in self._routing_rules:
            value = str(payload_map.get(rule.field, ""))
            if not value:
                continue

            matched = False
            if rule.match_mode == "contains":
                matched = rule.pattern in value
            elif rule.match_mode == "regex":
                matched = bool(re.search(rule.pattern, value, re.IGNORECASE))
            elif rule.match_mode == "equals":
                matched = value == rule.pattern
            elif rule.match_mode == "startswith":
                matched = value.startswith(rule.pattern)
            else:
                raise ValueError(f"Unknown match_mode: '{rule.match_mode}'")

            if matched:
                return rule.route

        return self._fallback_route
