# edition: baseline
from src.channels.commands.approval_events import (
    APPROVAL_EVENT_TYPES,
    approval_event_types,
    register_approval_event_type,
)


def test_approval_event_types_constant_is_live_view():
    register_approval_event_type("dynamic_approval_event")

    assert "dynamic_approval_event" in approval_event_types()
    assert "dynamic_approval_event" in APPROVAL_EVENT_TYPES
    assert "dynamic_approval_event" in set(APPROVAL_EVENT_TYPES)
