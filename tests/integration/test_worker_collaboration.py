# edition: baseline
"""
Integration tests for cross-Worker collaboration.

Tests end-to-end flows:
- Worker A delegates to Worker B (sync), result returned
- Worker A delegates to Worker B (async), task_id returned
- Delegation chain with depth tracking
- Notification mode through EventBus
- Tenant isolation across delegation
- Timeout handling in sync delegation
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Optional

import pytest

from src.events.bus import EventBus, Subscription
from src.events.models import Event
from src.tools.builtin.agent_tool import (
    MAX_DELEGATION_DEPTH,
    DelegateRequest,
    DelegateResult,
    execute_delegation,
    send_notification,
)
from src.worker.models import Worker, WorkerIdentity


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _WorkerEntry:
    """Stub matching WorkerRegistry.get() return type."""
    worker: Worker


class _InMemoryRegistry:
    """In-memory worker registry for integration tests."""

    def __init__(self) -> None:
        self._entries: dict[str, _WorkerEntry] = {}

    def register(self, worker: Worker) -> None:
        self._entries[worker.worker_id] = _WorkerEntry(worker=worker)

    def get(self, worker_id: str) -> Optional[_WorkerEntry]:
        return self._entries.get(worker_id)


def _create_worker(worker_id: str, role: str = "general") -> Worker:
    return Worker(
        identity=WorkerIdentity(
            worker_id=worker_id,
            name=worker_id.replace("-", " ").title(),
            role=role,
        ),
    )


@pytest.fixture
def registry() -> _InMemoryRegistry:
    reg = _InMemoryRegistry()
    reg.register(_create_worker("data-analyst", "Analyze data"))
    reg.register(_create_worker("crm-worker", "CRM operations"))
    reg.register(_create_worker("report-writer", "Write reports"))
    return reg


@pytest.fixture
def event_bus() -> EventBus:
    return EventBus()


# ---------------------------------------------------------------------------
# Delegation flow tests
# ---------------------------------------------------------------------------

class TestDelegationSync:
    @pytest.mark.asyncio
    async def test_worker_a_delegates_to_worker_b_sync(self, registry):
        """Full sync delegation: A -> B, result returned."""
        req = DelegateRequest(
            target_worker="crm-worker",
            task="Export Q1 sales data",
            mode="sync",
        )
        result = await execute_delegation(
            request=req,
            worker_registry=registry,
            source_worker_id="data-analyst",
            tenant_id="tenant-1",
        )
        assert result.status == "completed"
        assert "Export Q1 sales data" in result.result
        assert "crm-worker" in result.result
        assert result.task_id != ""

    @pytest.mark.asyncio
    async def test_sync_delegation_nonexistent_target(self, registry):
        """Delegation to nonexistent worker returns error."""
        req = DelegateRequest(
            target_worker="nonexistent-worker",
            task="Some task",
            mode="sync",
        )
        result = await execute_delegation(
            request=req,
            worker_registry=registry,
            source_worker_id="data-analyst",
            tenant_id="tenant-1",
        )
        assert result.status == "error"
        assert "not found" in result.error.lower()


class TestDelegationAsync:
    @pytest.mark.asyncio
    async def test_worker_a_delegates_to_worker_b_async(self, registry):
        """Async delegation returns task_id immediately."""
        req = DelegateRequest(
            target_worker="report-writer",
            task="Generate monthly report",
            mode="async",
        )
        result = await execute_delegation(
            request=req,
            worker_registry=registry,
            source_worker_id="data-analyst",
            tenant_id="tenant-1",
        )
        assert result.status == "submitted"
        assert result.task_id != ""
        assert result.error == ""


# ---------------------------------------------------------------------------
# Delegation chain and depth tests
# ---------------------------------------------------------------------------

class TestDelegationChain:
    @pytest.mark.asyncio
    async def test_depth_zero_delegation_succeeds(self, registry):
        """First-level delegation (depth=0) succeeds."""
        req = DelegateRequest(
            target_worker="crm-worker",
            task="task",
            delegation_depth=0,
        )
        result = await execute_delegation(
            request=req,
            worker_registry=registry,
            source_worker_id="data-analyst",
            tenant_id="t1",
        )
        assert result.status == "completed"

    @pytest.mark.asyncio
    async def test_depth_one_delegation_succeeds(self, registry):
        """Second-level delegation (depth=1) succeeds (max=2)."""
        req = DelegateRequest(
            target_worker="crm-worker",
            task="task",
            delegation_depth=1,
        )
        result = await execute_delegation(
            request=req,
            worker_registry=registry,
            source_worker_id="data-analyst",
            tenant_id="t1",
        )
        assert result.status == "completed"

    @pytest.mark.asyncio
    async def test_depth_at_max_rejected(self, registry):
        """Delegation at max depth is rejected."""
        req = DelegateRequest(
            target_worker="crm-worker",
            task="task",
            delegation_depth=MAX_DELEGATION_DEPTH,
        )
        result = await execute_delegation(
            request=req,
            worker_registry=registry,
            source_worker_id="data-analyst",
            tenant_id="t1",
        )
        assert result.status == "rejected"
        assert "depth limit" in result.error.lower()

    @pytest.mark.asyncio
    async def test_depth_propagates_incremented(self, registry):
        """Verify depth is incremented when passed to target worker."""
        import src.tools.builtin.agent_tool as mod

        captured_depths: list[int] = []
        original = mod._run_delegated_task

        async def _capture(**kwargs):
            captured_depths.append(kwargs["delegation_depth"])
            return DelegateResult(status="completed", task_id="x", result="ok")

        mod._run_delegated_task = _capture  # type: ignore
        try:
            req = DelegateRequest(
                target_worker="crm-worker",
                task="task",
                delegation_depth=0,
                mode="sync",
            )
            await execute_delegation(
                request=req,
                worker_registry=registry,
                source_worker_id="data-analyst",
                tenant_id="t1",
            )
            assert captured_depths == [1]

            # Second hop: depth=1 -> target gets depth=2
            req2 = DelegateRequest(
                target_worker="report-writer",
                task="sub-task",
                delegation_depth=1,
                mode="sync",
            )
            await execute_delegation(
                request=req2,
                worker_registry=registry,
                source_worker_id="crm-worker",
                tenant_id="t1",
            )
            assert captured_depths == [1, 2]
        finally:
            mod._run_delegated_task = original  # type: ignore


# ---------------------------------------------------------------------------
# Tenant isolation tests
# ---------------------------------------------------------------------------

class TestTenantIsolation:
    @pytest.mark.asyncio
    async def test_cross_tenant_delegation_rejected(self):
        """Workers from different tenants cannot delegate."""

        class _WorkerWithTenant:
            def __init__(self, wid: str, tid: str):
                self.worker_id = wid
                self.name = wid
                self.identity = WorkerIdentity(worker_id=wid, name=wid)
                self.tenant_id = tid

        class _TenantRegistry:
            def get(self, worker_id: str):
                if worker_id == "other-tenant-worker":
                    return _WorkerEntry(
                        worker=_WorkerWithTenant("other-tenant-worker", "tenant-B"),  # type: ignore
                    )
                return None

        req = DelegateRequest(
            target_worker="other-tenant-worker",
            task="cross-tenant task",
        )
        result = await execute_delegation(
            request=req,
            worker_registry=_TenantRegistry(),
            source_worker_id="my-worker",
            tenant_id="tenant-A",
        )
        assert result.status == "rejected"
        assert "cross-tenant" in result.error.lower()

    @pytest.mark.asyncio
    async def test_same_tenant_delegation_allowed(self, registry):
        """Workers in same tenant can delegate freely."""
        req = DelegateRequest(
            target_worker="crm-worker",
            task="same-tenant task",
        )
        result = await execute_delegation(
            request=req,
            worker_registry=registry,
            source_worker_id="data-analyst",
            tenant_id="tenant-1",
        )
        assert result.status != "rejected"


# ---------------------------------------------------------------------------
# Timeout tests
# ---------------------------------------------------------------------------

class TestSyncTimeout:
    @pytest.mark.asyncio
    async def test_sync_timeout_returns_timeout_status(self, registry):
        """Sync delegation that exceeds timeout returns timeout."""
        import src.tools.builtin.agent_tool as mod

        original = mod._run_delegated_task

        async def _slow(**kwargs):
            await asyncio.sleep(10)
            return DelegateResult(status="completed", result="late")

        mod._run_delegated_task = _slow  # type: ignore
        try:
            req = DelegateRequest(
                target_worker="crm-worker",
                task="slow task",
                mode="sync",
                timeout=1,
            )
            result = await execute_delegation(
                request=req,
                worker_registry=registry,
                source_worker_id="data-analyst",
                tenant_id="t1",
            )
            assert result.status == "timeout"
            assert result.task_id != ""
            assert "timed out" in result.error.lower()
        finally:
            mod._run_delegated_task = original  # type: ignore


# ---------------------------------------------------------------------------
# Notification mode tests
# ---------------------------------------------------------------------------

class TestNotificationMode:
    @pytest.mark.asyncio
    async def test_notification_dispatches_via_eventbus(self, event_bus):
        """Notification mode correctly publishes to EventBus."""
        received_events: list[Event] = []

        async def handler(event: Event):
            received_events.append(event)

        event_bus.subscribe(Subscription(
            handler_id="collab-handler",
            event_type="worker.*",
            tenant_id="t1",
            handler=handler,
        ))

        event_id = await send_notification(
            event_bus=event_bus,
            source_worker_id="data-analyst",
            tenant_id="t1",
            event_type="worker.task_completed",
            payload=(("task_id", "task-123"), ("summary", "All done")),
        )

        assert event_id != ""
        assert len(received_events) == 1

        event = received_events[0]
        assert event.type == "worker.task_completed"
        assert event.source == "data-analyst"
        assert event.tenant_id == "t1"
        payload_dict = dict(event.payload)
        assert payload_dict["task_id"] == "task-123"

    @pytest.mark.asyncio
    async def test_notification_tenant_isolated(self, event_bus):
        """Notification only reaches handlers in same tenant."""
        tenant_a_events: list[Event] = []
        tenant_b_events: list[Event] = []

        async def handler_a(event: Event):
            tenant_a_events.append(event)

        async def handler_b(event: Event):
            tenant_b_events.append(event)

        event_bus.subscribe(Subscription(
            handler_id="handler-a",
            event_type="worker.*",
            tenant_id="tenant-A",
            handler=handler_a,
        ))
        event_bus.subscribe(Subscription(
            handler_id="handler-b",
            event_type="worker.*",
            tenant_id="tenant-B",
            handler=handler_b,
        ))

        await send_notification(
            event_bus=event_bus,
            source_worker_id="worker-1",
            tenant_id="tenant-A",
            event_type="worker.done",
        )

        assert len(tenant_a_events) == 1
        assert len(tenant_b_events) == 0
