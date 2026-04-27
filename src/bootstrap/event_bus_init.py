"""
EventBus bootstrap initializer.

Creates and manages the lifecycle of the in-process EventBus.
Priority 25: after logging, before workers.
"""
import logging

from .base import Initializer

logger = logging.getLogger(__name__)


class EventBusInitializer(Initializer):
    """
    Initialize the EventBus subsystem.

    Creates an EventBus instance and stores it in the bootstrap context
    for consumption by Duty/Goal/Scheduler subsystems.
    """

    def __init__(self) -> None:
        self._event_bus = None
        self._subscription_id = ""

    @property
    def name(self) -> str:
        return "events"

    @property
    def depends_on(self) -> list[str]:
        return ["logging"]

    @property
    def priority(self) -> int:
        return 25

    @property
    def required(self) -> bool:
        return False

    async def initialize(self, context) -> bool:
        """Create EventBus and store in context."""
        try:
            from src.events.bus import EventBus, Subscription
            from src.events.recorder import RecentEventRecorder

            self._event_bus = EventBus()
            recorder = RecentEventRecorder(max_events=500)
            self._subscription_id = self._event_bus.subscribe(Subscription(
                handler_id="recent_event_recorder",
                event_type="*",
                tenant_id="*",
                handler=recorder.record,
            ))
            context.set_state("event_bus", self._event_bus)
            context.set_state("recent_event_recorder", recorder)
            logger.info("[EventBusInit] EventBus created and stored in context")
            return True
        except Exception as exc:
            logger.error(f"[EventBusInit] Failed: {exc}")
            return False

    async def cleanup(self) -> None:
        """Clear all subscriptions to prevent dangling handlers."""
        if self._event_bus is not None:
            self._event_bus.clear_all()
            self._subscription_id = ""
            logger.info("[EventBusInit] All subscriptions cleared")
