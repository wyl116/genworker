"""
EventBus module - in-process publish/subscribe with tenant isolation.

Provides:
- Event: immutable event data model
- EventBus: publish/subscribe hub with wildcard matching
- Subscription: handler registration descriptor
"""

from .models import (
    EventBusProtocol,
    EventHandler,
    Event,
    GoalCreatedPayload,
    ChannelFailedPayload,
    MountAuthExpiredPayload,
    Subscription,
    SubAgentEventPayload,
    TaskFailedPayload,
)
from .bus import EventBus

__all__ = [
    "Event",
    "EventBus",
    "EventBusProtocol",
    "EventHandler",
    "Subscription",
    "GoalCreatedPayload",
    "ChannelFailedPayload",
    "MountAuthExpiredPayload",
    "SubAgentEventPayload",
    "TaskFailedPayload",
]
