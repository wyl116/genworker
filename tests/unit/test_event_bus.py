# edition: baseline
"""
Tests for EventBus - publish/subscribe, tenant isolation, wildcards, filters.
"""
import asyncio

import pytest

from src.events.bus import (
    EventBus,
    Subscription,
    _event_type_matches,
    _filter_matches,
)
from src.events.models import Event


# --- _event_type_matches tests ---

class TestEventTypeMatches:
    def test_exact_match(self):
        assert _event_type_matches("data.file_uploaded", "data.file_uploaded")

    def test_exact_no_match(self):
        assert not _event_type_matches("data.file_uploaded", "data.file_deleted")

    def test_wildcard_star(self):
        assert _event_type_matches("*", "anything.here")

    def test_wildcard_prefix(self):
        assert _event_type_matches("data.*", "data.file_uploaded")

    def test_wildcard_prefix_no_match_different_prefix(self):
        assert not _event_type_matches("data.*", "user.created")

    def test_wildcard_single_level_only(self):
        # "data.*" should NOT match "data.file.uploaded" (two levels)
        assert not _event_type_matches("data.*", "data.file.uploaded")

    def test_wildcard_prefix_exact_prefix_no_extra(self):
        # "data.*" should not match "data." (empty remainder)
        assert not _event_type_matches("data.*", "data.")

    def test_empty_pattern(self):
        assert not _event_type_matches("", "data.file_uploaded")

    def test_same_prefix_different_suffix(self):
        assert _event_type_matches("channel.*", "channel.send_failed")


# --- _filter_matches tests ---

class TestFilterMatches:
    def test_empty_filter_always_matches(self):
        payload = (("key", "value"),)
        assert _filter_matches((), payload)

    def test_single_key_match(self):
        filter_spec = (("type", "email"),)
        payload = (("type", "email"), ("id", "123"))
        assert _filter_matches(filter_spec, payload)

    def test_single_key_no_match(self):
        filter_spec = (("type", "email"),)
        payload = (("type", "sms"),)
        assert not _filter_matches(filter_spec, payload)

    def test_missing_key(self):
        filter_spec = (("type", "email"),)
        payload = (("id", "123"),)
        assert not _filter_matches(filter_spec, payload)

    def test_multiple_filters_and_semantics(self):
        filter_spec = (("type", "email"), ("status", "failed"))
        payload = (("type", "email"), ("status", "failed"), ("id", "1"))
        assert _filter_matches(filter_spec, payload)

    def test_multiple_filters_partial_match(self):
        filter_spec = (("type", "email"), ("status", "failed"))
        payload = (("type", "email"), ("status", "ok"))
        assert not _filter_matches(filter_spec, payload)

    def test_list_value_match(self):
        filter_spec = (("tag", "urgent"),)
        payload = (("tag", ["urgent", "important"]),)
        assert _filter_matches(filter_spec, payload)

    def test_list_value_no_match(self):
        filter_spec = (("tag", "urgent"),)
        payload = (("tag", ["normal", "low"]),)
        assert not _filter_matches(filter_spec, payload)

    def test_regex_match(self):
        filter_spec = (("filename", r"regex:^report-\d+\.pdf$"),)
        payload = (("filename", "report-42.pdf"),)
        assert _filter_matches(filter_spec, payload)

    def test_regex_no_match(self):
        filter_spec = (("filename", r"regex:^report-\d+\.pdf$"),)
        payload = (("filename", "summary.txt"),)
        assert not _filter_matches(filter_spec, payload)

    def test_contains_match(self):
        filter_spec = (("subject", "contains:项目进度"),)
        payload = (("subject", "本周项目进度同步"),)
        assert _filter_matches(filter_spec, payload)

    def test_startswith_match(self):
        filter_spec = (("env", "startswith:prod-"),)
        payload = (("env", "prod-cn"),)
        assert _filter_matches(filter_spec, payload)

    def test_endswith_match(self):
        filter_spec = (("filename", "endswith:.pdf"),)
        payload = (("filename", "approval.pdf"),)
        assert _filter_matches(filter_spec, payload)

    def test_numeric_comparison_match(self):
        filter_spec = (("priority", ">= 3"),)
        payload = (("priority", 5),)
        assert _filter_matches(filter_spec, payload)

    def test_numeric_comparison_no_match(self):
        filter_spec = (("priority", ">= 3"),)
        payload = (("priority", 2),)
        assert not _filter_matches(filter_spec, payload)

    def test_between_expression_match(self):
        filter_spec = (("score", "between 0.7 and 0.9"),)
        payload = (("score", 0.8),)
        assert _filter_matches(filter_spec, payload)

    def test_list_value_regex_match(self):
        filter_spec = (("attachments", r"regex:.*\.pdf$"),)
        payload = (("attachments", ["readme.md", "contract.pdf"]),)
        assert _filter_matches(filter_spec, payload)


# --- EventBus tests ---

@pytest.fixture
def event_bus():
    return EventBus()


def _make_event(
    event_type: str = "test.event",
    tenant_id: str = "tenant_a",
    payload: tuple = (),
) -> Event:
    return Event(
        event_id="evt-001",
        type=event_type,
        source="test",
        tenant_id=tenant_id,
        payload=payload,
        timestamp="2026-01-01T00:00:00Z",
    )


class TestEventBusSubscribe:
    def test_subscribe_returns_handler_id(self, event_bus):
        async def handler(e): pass
        sub = Subscription(
            handler_id="h1",
            event_type="test.*",
            tenant_id="t1",
            handler=handler,
        )
        result = event_bus.subscribe(sub)
        assert result == "h1"
        assert event_bus.subscription_count == 1

    def test_unsubscribe_removes_handler(self, event_bus):
        async def handler(e): pass
        sub = Subscription(
            handler_id="h1",
            event_type="test.*",
            tenant_id="t1",
            handler=handler,
        )
        event_bus.subscribe(sub)
        assert event_bus.unsubscribe("t1", "h1") is True
        assert event_bus.subscription_count == 0

    def test_unsubscribe_nonexistent(self, event_bus):
        assert event_bus.unsubscribe("t1", "unknown") is False

    def test_clear_all(self, event_bus):
        async def handler(e): pass
        for i in range(3):
            sub = Subscription(
                handler_id=f"h{i}",
                event_type="test.*",
                tenant_id="t1",
                handler=handler,
            )
            event_bus.subscribe(sub)
        event_bus.clear_all()
        assert event_bus.subscription_count == 0


class TestEventBusPublish:
    @pytest.mark.asyncio
    async def test_publish_to_matching_handler(self, event_bus):
        received = []

        async def handler(e):
            received.append(e)

        sub = Subscription(
            handler_id="h1",
            event_type="test.event",
            tenant_id="tenant_a",
            handler=handler,
        )
        event_bus.subscribe(sub)

        event = _make_event()
        count = await event_bus.publish(event)
        assert count == 1
        assert len(received) == 1
        assert received[0].event_id == "evt-001"

    @pytest.mark.asyncio
    async def test_publish_no_match(self, event_bus):
        received = []

        async def handler(e):
            received.append(e)

        sub = Subscription(
            handler_id="h1",
            event_type="other.event",
            tenant_id="tenant_a",
            handler=handler,
        )
        event_bus.subscribe(sub)

        event = _make_event(event_type="test.event")
        count = await event_bus.publish(event)
        assert count == 0
        assert len(received) == 0

    @pytest.mark.asyncio
    async def test_tenant_isolation(self, event_bus):
        received_a = []
        received_b = []

        async def handler_a(e):
            received_a.append(e)

        async def handler_b(e):
            received_b.append(e)

        event_bus.subscribe(Subscription(
            handler_id="ha", event_type="test.*",
            tenant_id="tenant_a", handler=handler_a,
        ))
        event_bus.subscribe(Subscription(
            handler_id="hb", event_type="test.*",
            tenant_id="tenant_b", handler=handler_b,
        ))

        event = _make_event(event_type="test.event", tenant_id="tenant_a")
        count = await event_bus.publish(event)
        assert count == 1
        assert len(received_a) == 1
        assert len(received_b) == 0

    @pytest.mark.asyncio
    async def test_wildcard_subscription(self, event_bus):
        received = []

        async def handler(e):
            received.append(e)

        event_bus.subscribe(Subscription(
            handler_id="h1", event_type="data.*",
            tenant_id="t1", handler=handler,
        ))

        event = _make_event(event_type="data.file_uploaded", tenant_id="t1")
        count = await event_bus.publish(event)
        assert count == 1
        assert len(received) == 1

    @pytest.mark.asyncio
    async def test_filter_matching(self, event_bus):
        received = []

        async def handler(e):
            received.append(e)

        event_bus.subscribe(Subscription(
            handler_id="h1",
            event_type="channel.*",
            tenant_id="t1",
            handler=handler,
            filter=(("channel_type", "email"),),
        ))

        # Matching payload
        event_match = Event(
            event_id="e1", type="channel.send_failed",
            source="test", tenant_id="t1",
            payload=(("channel_type", "email"), ("error", "timeout")),
        )
        count = await event_bus.publish(event_match)
        assert count == 1

        # Non-matching payload
        event_no_match = Event(
            event_id="e2", type="channel.send_failed",
            source="test", tenant_id="t1",
            payload=(("channel_type", "sms"),),
        )
        count = await event_bus.publish(event_no_match)
        assert count == 0

    @pytest.mark.asyncio
    async def test_regex_filter_matching(self, event_bus):
        received = []

        async def handler(e):
            received.append(e)

        event_bus.subscribe(Subscription(
            handler_id="h1",
            event_type="data.file_uploaded",
            tenant_id="t1",
            handler=handler,
            filter=(("filename", r"regex:^report-\d+\.csv$"),),
        ))

        event = Event(
            event_id="e1",
            type="data.file_uploaded",
            source="test",
            tenant_id="t1",
            payload=(("filename", "report-202.csv"),),
        )
        count = await event_bus.publish(event)
        assert count == 1
        assert len(received) == 1

    @pytest.mark.asyncio
    async def test_handler_exception_does_not_block(self, event_bus):
        received = []

        async def bad_handler(e):
            raise RuntimeError("boom")

        async def good_handler(e):
            received.append(e)

        event_bus.subscribe(Subscription(
            handler_id="bad", event_type="test.*",
            tenant_id="t1", handler=bad_handler,
        ))
        event_bus.subscribe(Subscription(
            handler_id="good", event_type="test.*",
            tenant_id="t1", handler=good_handler,
        ))

        event = _make_event(event_type="test.event", tenant_id="t1")
        count = await event_bus.publish(event)
        assert count == 2
        assert len(received) == 1

    @pytest.mark.asyncio
    async def test_multiple_handlers_same_event(self, event_bus):
        received = []

        async def handler1(e):
            received.append("h1")

        async def handler2(e):
            received.append("h2")

        event_bus.subscribe(Subscription(
            handler_id="h1", event_type="test.event",
            tenant_id="t1", handler=handler1,
        ))
        event_bus.subscribe(Subscription(
            handler_id="h2", event_type="test.event",
            tenant_id="t1", handler=handler2,
        ))

        event = _make_event(event_type="test.event", tenant_id="t1")
        count = await event_bus.publish(event)
        assert count == 2
        assert "h1" in received
        assert "h2" in received
