"""
Event handler registration utilities.

Provides helper functions for creating Subscription objects
and registering common handler patterns with the EventBus.
"""
from uuid import uuid4

from .bus import EventBus
from .models import EventBusProtocol, EventHandler, Subscription


def register_handler(
    event_bus: EventBusProtocol,
    event_type: str,
    tenant_id: str,
    handler: EventHandler,
    handler_id: str | None = None,
    filter_spec: tuple[tuple[str, str], ...] = (),
) -> str:
    """
    Convenience function to register an event handler.

    Creates a Subscription and registers it with the EventBus.
    Returns the handler_id.
    """
    resolved_id = handler_id or uuid4().hex
    subscription = Subscription(
        handler_id=resolved_id,
        event_type=event_type,
        tenant_id=tenant_id,
        handler=handler,
        filter=filter_spec,
    )
    return event_bus.subscribe(subscription)


def unregister_handler(
    event_bus: EventBusProtocol,
    tenant_id: str,
    handler_id: str,
) -> bool:
    """Convenience function to unregister an event handler."""
    return event_bus.unsubscribe(tenant_id, handler_id)
