"""Sensor configuration models."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class RoutingRule:
    """One per-fact route classification rule."""

    field: str
    pattern: str
    match_mode: str = "contains"
    route: str = "reactive"


@dataclass(frozen=True)
class SensorConfig:
    """Per-sensor configuration parsed from worker monitor settings."""

    source_type: str
    poll_interval: str = "5m"
    delivery_mode: str = ""
    filter: tuple[tuple[str, str], ...] = ()
    auto_create_goal: bool = False
    require_approval: bool = True
    cognition_route_override: str = ""
    routing_rules: tuple[RoutingRule, ...] = ()
    fallback_route: str = ""


def parse_sensor_config(raw: Mapping[str, Any] | dict[str, Any]) -> SensorConfig:
    """Convert a raw monitor config mapping into a normalized SensorConfig."""
    filter_raw = raw.get("filter", {})
    if isinstance(filter_raw, dict):
        filter_tuples = tuple((str(key), str(value)) for key, value in filter_raw.items())
    else:
        filter_tuples = tuple((str(key), str(value)) for key, value in filter_raw)

    routing_rules_raw = raw.get("routing_rules", ())
    routing_rules = tuple(
        RoutingRule(
            field=str(rule.get("field", "")),
            pattern=str(rule.get("pattern", "")),
            match_mode=str(rule.get("match_mode", "contains")),
            route=str(rule.get("route", "reactive")),
        )
        for rule in routing_rules_raw
        if isinstance(rule, Mapping)
    )

    return SensorConfig(
        source_type=str(raw.get("source_type", "")),
        poll_interval=str(raw.get("poll_interval", "5m")),
        delivery_mode=str(raw.get("delivery_mode", "")),
        filter=filter_tuples,
        auto_create_goal=bool(raw.get("auto_create_goal", False)),
        require_approval=bool(raw.get("require_approval", True)),
        cognition_route_override=str(raw.get("cognition_route_override", "")),
        routing_rules=routing_rules,
        fallback_route=str(raw.get("fallback_route", "")),
    )
