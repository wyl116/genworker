# edition: baseline
"""
Tests for ChannelAdapter - email, feishu, reliable retry, multi-channel fallback.
"""
from __future__ import annotations

import asyncio

import pytest

from src.channels.outbound import (
    ChannelSendError,
    EmailChannelAdapter,
    FeishuChannelAdapter,
    MultiChannelFallback,
    ReliableChannelAdapter,
    DingTalkChannelAdapter,
    DirectEmailAdapter,
    WeComChannelAdapter,
    _compute_backoff,
    _heading_level,
    _replace_section,
)
from src.channels.outbound_types import (
    ChannelMessage,
    ChannelPriority,
    RetryConfig,
)


# ---------------------------------------------------------------------------
# Mocks
# ---------------------------------------------------------------------------

class MockToolExecutor:
    """Mock tool executor that records calls."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def execute(self, tool_name: str, tool_input: dict):
        self.calls.append((tool_name, tool_input))
        return {"status": "sent", "id": "email-123"}


class MockMountManager:
    """Mock mount manager for feishu document operations."""

    def __init__(self, file_content: str = ""):
        self.files: dict[str, str] = {}
        self._default_content = file_content

    async def read_file(self, path: str) -> str:
        return self.files.get(path, self._default_content)

    async def write_file(self, path: str, content: str) -> None:
        self.files[path] = content


class FailingAdapter:
    """Adapter that always fails."""

    def __init__(self, error_msg: str = "Send failed"):
        self._error_msg = error_msg
        self.call_count = 0

    async def send(self, message: ChannelMessage) -> str:
        self.call_count += 1
        raise RuntimeError(self._error_msg)

    async def update_document(
        self, path: str, content: str, section: str | None = None,
    ) -> bool:
        self.call_count += 1
        raise RuntimeError(self._error_msg)


class CountingAdapter:
    """Adapter that fails N times then succeeds."""

    def __init__(self, fail_times: int = 0):
        self._fail_times = fail_times
        self.call_count = 0

    async def send(self, message: ChannelMessage) -> str:
        self.call_count += 1
        if self.call_count <= self._fail_times:
            raise RuntimeError(f"Fail #{self.call_count}")
        return f"msg-{self.call_count}"

    async def update_document(
        self, path: str, content: str, section: str | None = None,
    ) -> bool:
        self.call_count += 1
        if self.call_count <= self._fail_times:
            raise RuntimeError(f"Fail #{self.call_count}")
        return True


class SuccessAdapter:
    """Adapter that always succeeds."""

    def __init__(self, msg_id: str = "ok-1"):
        self._msg_id = msg_id
        self.call_count = 0

    async def send(self, message: ChannelMessage) -> str:
        self.call_count += 1
        return self._msg_id

    async def update_document(
        self, path: str, content: str, section: str | None = None,
    ) -> bool:
        self.call_count += 1
        return True


class MockEventBus:
    """Mock event bus that records published events."""

    def __init__(self):
        self.events: list = []

    async def publish(self, event) -> int:
        self.events.append(event)
        return 1


class MockIMAdapter:
    def __init__(self, msg_id: str = "im-1") -> None:
        self.msg_id = msg_id
        self.calls: list[tuple[str, object]] = []

    async def send_message(self, chat_id: str, content) -> str:
        self.calls.append((chat_id, content))
        return self.msg_id


class MockWeComClient:
    async def send_message(self, recipients, content):
        return {"msgid": "wecom-1"}


class MockDingTalkClient:
    async def send_message(self, recipients, content):
        return {"processQueryKey": "dt-1"}


class MockEmailClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def send(self, **kwargs) -> str:
        self.calls.append(kwargs)
        return "email-direct"


def _msg(**overrides) -> ChannelMessage:
    """Create a ChannelMessage with defaults."""
    defaults = dict(
        channel="email",
        recipients=("test@example.com",),
        subject="Test Subject",
        content="Test content",
    )
    defaults.update(overrides)
    return ChannelMessage(**defaults)


# ---------------------------------------------------------------------------
# _compute_backoff
# ---------------------------------------------------------------------------

class TestComputeBackoff:
    def test_attempt_0(self):
        assert _compute_backoff(0, 2.0, 60.0) == 2.0

    def test_attempt_1(self):
        assert _compute_backoff(1, 2.0, 60.0) == 4.0

    def test_attempt_2(self):
        assert _compute_backoff(2, 2.0, 60.0) == 8.0

    def test_attempt_3(self):
        assert _compute_backoff(3, 2.0, 60.0) == 16.0

    def test_capped_at_max(self):
        assert _compute_backoff(10, 2.0, 60.0) == 60.0

    def test_custom_base(self):
        assert _compute_backoff(0, 3.0, 60.0) == 3.0
        assert _compute_backoff(1, 3.0, 60.0) == 6.0


# ---------------------------------------------------------------------------
# EmailChannelAdapter
# ---------------------------------------------------------------------------

class TestEmailChannelAdapter:
    @pytest.mark.asyncio
    async def test_send_calls_tool_executor(self):
        executor = MockToolExecutor()
        adapter = EmailChannelAdapter(executor)

        msg_id = await adapter.send(_msg(
            recipients=("a@b.com", "c@d.com"),
            subject="Hello",
            content="Body text",
        ))

        assert msg_id.startswith("email-")
        assert len(executor.calls) == 1
        tool_name, tool_input = executor.calls[0]
        assert tool_name == "email_send"
        assert "a@b.com" in tool_input["to"]
        assert tool_input["subject"] == "Hello"

    @pytest.mark.asyncio
    async def test_send_with_reply_to(self):
        executor = MockToolExecutor()
        adapter = EmailChannelAdapter(executor)

        await adapter.send(_msg(reply_to="original-msg-id"))

        _, tool_input = executor.calls[0]
        assert tool_input["reply_to"] == "original-msg-id"

    @pytest.mark.asyncio
    async def test_update_document_returns_false(self):
        executor = MockToolExecutor()
        adapter = EmailChannelAdapter(executor)

        result = await adapter.update_document("/path", "content")
        assert result is False

    @pytest.mark.asyncio
    async def test_send_delegates_to_im_adapter_when_chat_id_present(self):
        executor = MockToolExecutor()
        im_adapter = MockIMAdapter("im-email")
        adapter = EmailChannelAdapter(executor, im_adapter=im_adapter)

        msg_id = await adapter.send(_msg(im_chat_id="support@corp.com"))

        assert msg_id == "im-email"
        assert im_adapter.calls[0][0] == "support@corp.com"
        assert executor.calls == []


class TestDirectEmailAdapter:
    @pytest.mark.asyncio
    async def test_send_delegates_to_im_adapter_when_chat_id_present(self):
        email_client = MockEmailClient()
        im_adapter = MockIMAdapter("im-email")
        adapter = DirectEmailAdapter(email_client, im_adapter=im_adapter)

        msg_id = await adapter.send(_msg(im_chat_id="support@corp.com"))

        assert msg_id == "im-email"
        assert im_adapter.calls[0][0] == "support@corp.com"
        assert email_client.calls == []


# ---------------------------------------------------------------------------
# FeishuChannelAdapter
# ---------------------------------------------------------------------------

class TestFeishuChannelAdapter:
    @pytest.mark.asyncio
    async def test_update_document_full_replace(self):
        mount = MockMountManager()
        adapter = FeishuChannelAdapter(mount)

        result = await adapter.update_document(
            "/doc.md", "new content", section=None,
        )

        assert result is True
        assert mount.files["/doc.md"] == "new content"

    @pytest.mark.asyncio
    async def test_update_document_section_replace(self):
        mount = MockMountManager(
            "# Title\n\n## Progress Update\nOld progress\n\n## Other\nKept"
        )
        adapter = FeishuChannelAdapter(mount)

        result = await adapter.update_document(
            "/doc.md", "New progress text", section="## Progress Update",
        )

        assert result is True
        updated = mount.files["/doc.md"]
        assert "New progress text" in updated
        assert "Old progress" not in updated
        assert "## Other" in updated
        assert "Kept" in updated

    @pytest.mark.asyncio
    async def test_send_returns_msg_id(self):
        mount = MockMountManager()
        adapter = FeishuChannelAdapter(mount)

        msg_id = await adapter.send(_msg(channel="feishu"))
        assert msg_id.startswith("feishu-")

    @pytest.mark.asyncio
    async def test_send_delegates_to_im_adapter_when_chat_id_present(self):
        mount = MockMountManager()
        im_adapter = MockIMAdapter("im-feishu")
        adapter = FeishuChannelAdapter(mount, im_adapter=im_adapter)

        msg_id = await adapter.send(_msg(channel="feishu", im_chat_id="oc_123"))

        assert msg_id == "im-feishu"
        assert im_adapter.calls[0][0] == "oc_123"


class TestWeComChannelAdapter:
    @pytest.mark.asyncio
    async def test_send_delegates_to_im_adapter_when_chat_id_present(self):
        im_adapter = MockIMAdapter("im-wecom")
        adapter = WeComChannelAdapter(MockWeComClient(), im_adapter=im_adapter)

        msg_id = await adapter.send(_msg(channel="wecom", im_chat_id="chat_123"))

        assert msg_id == "im-wecom"
        assert im_adapter.calls[0][0] == "chat_123"


class TestDingTalkChannelAdapter:
    @pytest.mark.asyncio
    async def test_send_delegates_to_im_adapter_when_chat_id_present(self):
        im_adapter = MockIMAdapter("im-dingtalk")
        adapter = DingTalkChannelAdapter(MockDingTalkClient(), im_adapter=im_adapter)

        msg_id = await adapter.send(_msg(channel="dingtalk", im_chat_id="cid_123"))

        assert msg_id == "im-dingtalk"
        assert im_adapter.calls[0][0] == "cid_123"


# ---------------------------------------------------------------------------
# ReliableChannelAdapter
# ---------------------------------------------------------------------------

class TestReliableChannelAdapter:
    @pytest.mark.asyncio
    async def test_success_on_first_try(self):
        inner = SuccessAdapter("msg-ok")
        adapter = ReliableChannelAdapter(
            inner=inner,
            retry_config=RetryConfig(max_retries=3, backoff_base=0.01),
        )

        result = await adapter.send(_msg())
        assert result == "msg-ok"
        assert inner.call_count == 1

    @pytest.mark.asyncio
    async def test_success_after_retries(self):
        inner = CountingAdapter(fail_times=2)
        adapter = ReliableChannelAdapter(
            inner=inner,
            retry_config=RetryConfig(max_retries=3, backoff_base=0.01),
        )

        result = await adapter.send(_msg())
        assert result == "msg-3"
        assert inner.call_count == 3

    @pytest.mark.asyncio
    async def test_all_retries_exhausted_raises(self):
        inner = FailingAdapter("persistent error")
        event_bus = MockEventBus()
        adapter = ReliableChannelAdapter(
            inner=inner,
            retry_config=RetryConfig(max_retries=3, backoff_base=0.01),
            event_bus=event_bus,
            tenant_id="test-tenant",
        )

        with pytest.raises(ChannelSendError) as exc_info:
            await adapter.send(_msg())

        assert exc_info.value.attempts == 4  # 1 initial + 3 retries
        assert inner.call_count == 4

    @pytest.mark.asyncio
    async def test_publishes_send_failed_event(self):
        inner = FailingAdapter("error")
        event_bus = MockEventBus()
        adapter = ReliableChannelAdapter(
            inner=inner,
            retry_config=RetryConfig(max_retries=2, backoff_base=0.01),
            event_bus=event_bus,
            tenant_id="t1",
        )

        with pytest.raises(ChannelSendError):
            await adapter.send(_msg(channel="email"))

        assert len(event_bus.events) == 1
        event = event_bus.events[0]
        assert event.type == "channel.send_failed"
        payload = dict(event.payload)
        assert payload["channel_type"] == "email"
        assert payload["attempts"] == 3

    @pytest.mark.asyncio
    async def test_backoff_delays_correct(self):
        """Verify exponential backoff: base=2 -> 2s, 4s, 8s."""
        inner = FailingAdapter("fail")
        adapter = ReliableChannelAdapter(
            inner=inner,
            retry_config=RetryConfig(
                max_retries=3, backoff_base=2.0, backoff_max=60.0,
            ),
        )

        # We can't actually wait, so we check the computed delays
        # by using a very small base and verifying the math
        inner2 = FailingAdapter("fail")
        adapter2 = ReliableChannelAdapter(
            inner=inner2,
            retry_config=RetryConfig(
                max_retries=3, backoff_base=0.001, backoff_max=60.0,
            ),
        )

        with pytest.raises(ChannelSendError):
            await adapter2.send(_msg())

        # Verify delay calculation: 0.001*1, 0.001*2, 0.001*4
        assert len(adapter2._last_delays) == 3
        assert adapter2._last_delays[0] == pytest.approx(0.001, rel=0.1)
        assert adapter2._last_delays[1] == pytest.approx(0.002, rel=0.1)
        assert adapter2._last_delays[2] == pytest.approx(0.004, rel=0.1)

    @pytest.mark.asyncio
    async def test_backoff_formula_base2(self):
        """backoff_base=2 -> attempt 0: 2s, attempt 1: 4s, attempt 2: 8s."""
        assert _compute_backoff(0, 2.0, 60.0) == 2.0
        assert _compute_backoff(1, 2.0, 60.0) == 4.0
        assert _compute_backoff(2, 2.0, 60.0) == 8.0

    @pytest.mark.asyncio
    async def test_update_document_with_retry(self):
        inner = CountingAdapter(fail_times=1)
        adapter = ReliableChannelAdapter(
            inner=inner,
            retry_config=RetryConfig(max_retries=2, backoff_base=0.01),
        )

        result = await adapter.update_document("/path", "content")
        assert result is True
        assert inner.call_count == 2


# ---------------------------------------------------------------------------
# MultiChannelFallback
# ---------------------------------------------------------------------------

class TestMultiChannelFallback:
    @pytest.mark.asyncio
    async def test_primary_success(self):
        primary = SuccessAdapter("primary-1")
        fallback = SuccessAdapter("fallback-1")
        channels = (
            ChannelPriority(channel_type="email", adapter=primary),
            ChannelPriority(channel_type="feishu", adapter=fallback),
        )
        multi = MultiChannelFallback(channels)

        result = await multi.send(_msg())
        assert result == "primary-1"
        assert primary.call_count == 1
        assert fallback.call_count == 0

    @pytest.mark.asyncio
    async def test_fallback_on_primary_failure(self):
        primary = FailingAdapter("primary down")
        fallback = SuccessAdapter("fallback-1")
        channels = (
            ChannelPriority(channel_type="email", adapter=primary),
            ChannelPriority(channel_type="feishu", adapter=fallback),
        )
        multi = MultiChannelFallback(channels)

        result = await multi.send(_msg())
        assert result == "fallback-1"
        assert primary.call_count == 1
        assert fallback.call_count == 1

    @pytest.mark.asyncio
    async def test_all_channels_fail_raises(self):
        primary = FailingAdapter("fail-1")
        fallback = FailingAdapter("fail-2")
        event_bus = MockEventBus()
        channels = (
            ChannelPriority(channel_type="email", adapter=primary),
            ChannelPriority(channel_type="feishu", adapter=fallback),
        )
        multi = MultiChannelFallback(channels, event_bus=event_bus)

        with pytest.raises(ChannelSendError):
            await multi.send(_msg())

        assert len(event_bus.events) == 1
        assert event_bus.events[0].type == "channel.all_channels_failed"

    @pytest.mark.asyncio
    async def test_high_priority_broadcasts_to_all(self):
        adapter1 = SuccessAdapter("ch1")
        adapter2 = SuccessAdapter("ch2")
        channels = (
            ChannelPriority(channel_type="email", adapter=adapter1),
            ChannelPriority(channel_type="feishu", adapter=adapter2),
        )
        multi = MultiChannelFallback(channels)

        result = await multi.send(_msg(priority="high"))
        # Both should be called
        assert adapter1.call_count == 1
        assert adapter2.call_count == 1

    @pytest.mark.asyncio
    async def test_high_priority_partial_success(self):
        adapter1 = FailingAdapter("fail")
        adapter2 = SuccessAdapter("ch2-ok")
        channels = (
            ChannelPriority(channel_type="email", adapter=adapter1),
            ChannelPriority(channel_type="feishu", adapter=adapter2),
        )
        multi = MultiChannelFallback(channels)

        result = await multi.send(_msg(priority="high"))
        assert result == "ch2-ok"

    @pytest.mark.asyncio
    async def test_high_priority_all_fail_raises(self):
        adapter1 = FailingAdapter("fail-1")
        adapter2 = FailingAdapter("fail-2")
        event_bus = MockEventBus()
        channels = (
            ChannelPriority(channel_type="email", adapter=adapter1),
            ChannelPriority(channel_type="feishu", adapter=adapter2),
        )
        multi = MultiChannelFallback(channels, event_bus=event_bus)

        with pytest.raises(ChannelSendError):
            await multi.send(_msg(priority="high"))

        assert len(event_bus.events) == 1

    @pytest.mark.asyncio
    async def test_update_document_uses_first_supporting_channel(self):
        adapter1 = SuccessAdapter()
        adapter2 = SuccessAdapter()
        channels = (
            ChannelPriority(channel_type="email", adapter=adapter1),
            ChannelPriority(channel_type="feishu", adapter=adapter2),
        )
        multi = MultiChannelFallback(channels)

        result = await multi.update_document("/doc", "content")
        assert result is True


# ---------------------------------------------------------------------------
# _replace_section helper
# ---------------------------------------------------------------------------

class TestReplaceSection:
    def test_replaces_section_content(self):
        doc = "# Title\n\n## Updates\nOld content\n\n## Other\nKept"
        result = _replace_section(doc, "## Updates", "New content")
        assert "New content" in result
        assert "Old content" not in result
        assert "## Other" in result
        assert "Kept" in result

    def test_section_not_found_appends(self):
        doc = "# Title\n\nSome text"
        result = _replace_section(doc, "## Missing", "Added content")
        assert "## Missing" in result
        assert "Added content" in result

    def test_heading_level_detection(self):
        assert _heading_level("## Heading") == 2
        assert _heading_level("### Sub") == 3
        assert _heading_level("# Top") == 1
        assert _heading_level("Not a heading") == 0
        assert _heading_level("") == 0
