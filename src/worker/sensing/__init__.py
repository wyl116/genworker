"""Sensor framework for pluggable environment sensing."""

from .base import SensorBase
from .config import RoutingRule, SensorConfig, parse_sensor_config
from .protocol import FactCallback, Sensor, SensedFact
from .registry import SensorRegistry
from .snapshot_store import SnapshotStore

__all__ = [
    "FactCallback",
    "RoutingRule",
    "Sensor",
    "SensorBase",
    "SensorConfig",
    "SensorRegistry",
    "SensedFact",
    "SnapshotStore",
    "parse_sensor_config",
]
