"""External-only compatibility exports for integration models and channel types."""
from __future__ import annotations

from src.channels.outbound_types import (
    ChannelMessage,
    ChannelPriority,
    RetryConfig,
    SenderScope,
)

from .domain_models import MonitorConfig, ParsedGoalInfo, SyncRecord

__all__ = [
    "ChannelMessage",
    "ChannelPriority",
    "MonitorConfig",
    "ParsedGoalInfo",
    "RetryConfig",
    "SenderScope",
    "SyncRecord",
]
