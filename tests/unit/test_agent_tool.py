# edition: baseline
"""
Unit tests for agent_tool - delegate_to_worker with depth limiting and tenant isolation.

Tests:
- DelegateRequest / DelegateResult immutability
- AGENT_TOOL_DEFINITION schema structure
- Depth limit enforcement (MAX_DELEGATION_DEPTH=2)
- Target worker validation (not found)
- Tenant isolation (cross-tenant rejected)
- Sync mode execution with result
- Async mode returns task_id immediately
- Sync mode timeout handling
- Notification via EventBus
"""
from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError, dataclass
from typing import Optional
from unittest.mock import AsyncMock

import pytest

from src.events.bus import EventBus
from src.streaming.events import ErrorEvent, TextMessageEvent
from src.tools.builtin.agent_tool import (
    AGENT_TOOL_DEFINITION,
    MAX_DELEGATION_DEPTH,
    DelegateRequest,
    DelegateResult,
    execute_delegation,
    send_notification,
)
from src.worker.models import Worker, WorkerIdentity


# ---------------------------------------------------------------------------
# Helpers: stub registry that simulates WorkerRegistry.get()
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _StubWorkerEntry:
    """Minimal stub matching WorkerEntry interface."""
    worker: Worker


class _StubRegistry:
    """Minimal stub matching WorkerRegistry.get() interface."""

    def __init__(self, entries: dict[str, _StubWorkerEntry]) -> None:
        self._entries = entries

    def get(self, worker_id: str) -> Optional[_StubWorkerEntry]:
        return self._entries.get(worker_id)


def _make_worker(worker_id: str, name: str = "", tenant_id: str = "") -> Worker:
    """Create a minimal Worker for testing."""
    return Worker(
        identity=WorkerIdentity(
            worker_id=worker_id,
            name=name or worker_id,
            role="test-role",
        ),
    )


def _make_registry(*workers: Worker) -> _StubRegistry:
    """Build a stub registry from workers."""
    entries = {
        w.worker_id: _StubWorkerEntry(worker=w)
        for w in workers
    }
    return _StubRegistry(entries)


# ---------------------------------------------------------------------------
# Data model immutability tests
# ---------------------------------------------------------------------------

class TestDelegateRequestImmutability:
    def test_frozen(self):
        req = DelegateRequest(target_worker="w1", task="do stuff")
        with pytest.raises(FrozenInstanceError):
            req.target_worker = "w2"  # type: ignore[misc]

    def test_defaults(self):
        req = DelegateRequest(target_worker="w1", task="t1")
        assert req.context == ()
        assert req.timeout == 300
        assert req.mode == "sync"
        assert req.delegation_depth == 0


class TestDelegateResultImmutability:
    def test_frozen(self):
        res = DelegateResult(status="completed", result="ok")
        with pytest.raises(FrozenInstanceError):
            res.status = "error"  # type: ignore[misc]

    def test_defaults(self):
        res = DelegateResult(status="completed")
        assert res.result == ""
        assert res.task_id == ""
        assert res.error == ""


# ---------------------------------------------------------------------------
# AGENT_TOOL_DEFINITION schema tests
# ---------------------------------------------------------------------------

class TestAgentToolDefinition:
    def test_name(self):
        assert AGENT_TOOL_DEFINITION["name"] == "delegate_to_worker"

    def test_has_description(self):
        assert len(AGENT_TOOL_DEFINITION["description"]) > 0

    def test_input_schema_required_fields(self):
        schema = AGENT_TOOL_DEFINITION["input_schema"]
        assert "target_worker" in schema["properties"]
        assert "task" in schema["properties"]
        assert "target_worker" in schema["required"]
        assert "task" in schema["required"]

    def test_risk_level(self):
        assert AGENT_TOOL_DEFINITION["risk_level"] == "normal"


# ---------------------------------------------------------------------------
# execute_delegation tests
# ---------------------------------------------------------------------------

class TestDelegationDepthLimit:
    @pytest.mark.asyncio
    async def test_depth_at_max_rejected(self):
        """delegation_depth == MAX_DELEGATION_DEPTH -> rejected."""
        registry = _make_registry(_make_worker("target-1"))
        req = DelegateRequest(
            target_worker="target-1",
            task="test",
            delegation_depth=MAX_DELEGATION_DEPTH,
        )
        result = await execute_delegation(
            request=req,
            worker_registry=registry,
            source_worker_id="source-1",
            tenant_id="t1",
        )
        assert result.status == "rejected"
        assert "depth limit exceeded" in result.error.lower()

    @pytest.mark.asyncio
    async def test_depth_above_max_rejected(self):
        """delegation_depth > MAX_DELEGATION_DEPTH -> rejected."""
        registry = _make_registry(_make_worker("target-1"))
        req = DelegateRequest(
            target_worker="target-1",
            task="test",
            delegation_depth=MAX_DELEGATION_DEPTH + 1,
        )
        result = await execute_delegation(
            request=req,
            worker_registry=registry,
            source_worker_id="source-1",
            tenant_id="t1",
        )
        assert result.status == "rejected"

    @pytest.mark.asyncio
    async def test_depth_below_max_allowed(self):
        """delegation_depth < MAX_DELEGATION_DEPTH -> allowed."""
        registry = _make_registry(_make_worker("target-1"))
        req = DelegateRequest(
            target_worker="target-1",
            task="test task",
            delegation_depth=0,
        )
        result = await execute_delegation(
            request=req,
            worker_registry=registry,
            source_worker_id="source-1",
            tenant_id="t1",
        )
        assert result.status == "completed"


class TestTargetWorkerValidation:
    @pytest.mark.asyncio
    async def test_nonexistent_worker_returns_error(self):
        """Target worker not in registry -> error result."""
        registry = _make_registry()  # empty
        req = DelegateRequest(target_worker="ghost-worker", task="test")
        result = await execute_delegation(
            request=req,
            worker_registry=registry,
            source_worker_id="source-1",
            tenant_id="t1",
        )
        assert result.status == "error"
        assert "not found" in result.error.lower()


class TestTenantIsolation:
    @pytest.mark.asyncio
    async def test_cross_tenant_rejected(self):
        """Workers from different tenants cannot delegate to each other."""
        # Create a worker with a tenant_id attribute to trigger isolation
        worker = _make_worker("target-1")
        # Attach a tenant_id to the worker for the isolation check
        # We use a wrapper since Worker is frozen
        entry = _StubWorkerEntry(worker=worker)

        class _TenantAwareRegistry:
            def get(self, worker_id: str):
                if worker_id == "target-1":
                    # Return entry with a worker that has tenant_id
                    return _TenantAwareEntry(worker=worker, tenant_id="tenant-B")
                return None

        @dataclass(frozen=True)
        class _TenantAwareEntry:
            worker: Worker
            tenant_id: str = ""

        # Monkey-patch _extract_tenant_id for this test by giving
        # the worker a tenant_id attribute
        class _WorkerWithTenant:
            def __init__(self, w: Worker, tid: str):
                self.worker_id = w.worker_id
                self.name = w.name
                self.identity = w.identity
                self.tenant_id = tid

        class _TenantRegistry:
            def get(self, worker_id: str):
                if worker_id == "target-1":
                    return _StubWorkerEntry(
                        worker=_WorkerWithTenant(worker, "tenant-B"),  # type: ignore
                    )
                return None

        registry = _TenantRegistry()
        req = DelegateRequest(target_worker="target-1", task="test")
        result = await execute_delegation(
            request=req,
            worker_registry=registry,
            source_worker_id="source-1",
            tenant_id="tenant-A",
        )
        assert result.status == "rejected"
        assert "cross-tenant" in result.error.lower()

    @pytest.mark.asyncio
    async def test_same_tenant_allowed(self):
        """Workers in the same tenant can delegate."""
        registry = _make_registry(_make_worker("target-1"))
        req = DelegateRequest(target_worker="target-1", task="test")
        result = await execute_delegation(
            request=req,
            worker_registry=registry,
            source_worker_id="source-1",
            tenant_id="t1",
        )
        assert result.status in ("completed", "submitted")


class TestSyncMode:
    @pytest.mark.asyncio
    async def test_sync_returns_completed(self):
        """Sync mode waits and returns completed result."""
        registry = _make_registry(_make_worker("target-1"))
        req = DelegateRequest(
            target_worker="target-1",
            task="analyze data",
            mode="sync",
        )
        result = await execute_delegation(
            request=req,
            worker_registry=registry,
            source_worker_id="source-1",
            tenant_id="t1",
        )
        assert result.status == "completed"
        assert "analyze data" in result.result
        assert result.task_id != ""

    @pytest.mark.asyncio
    async def test_sync_timeout(self):
        """Sync mode returns timeout when execution exceeds timeout."""
        import src.tools.builtin.agent_tool as agent_tool_mod

        original_fn = agent_tool_mod._run_delegated_task

        async def _slow_task(**kwargs):
            await asyncio.sleep(10)  # Will exceed timeout
            return DelegateResult(status="completed", result="done")

        agent_tool_mod._run_delegated_task = _slow_task  # type: ignore

        try:
            registry = _make_registry(_make_worker("target-1"))
            req = DelegateRequest(
                target_worker="target-1",
                task="slow task",
                mode="sync",
                timeout=1,  # 1 second timeout
            )
            result = await execute_delegation(
                request=req,
                worker_registry=registry,
                source_worker_id="source-1",
                tenant_id="t1",
            )
            assert result.status == "timeout"
            assert "timed out" in result.error.lower()
        finally:
            agent_tool_mod._run_delegated_task = original_fn  # type: ignore

    @pytest.mark.asyncio
    async def test_sync_uses_worker_router_when_available(self):
        """Compatibility helper should use WorkerRouter when provided."""
        registry = _make_registry(_make_worker("target-1"))

        class _Router:
            def __init__(self) -> None:
                self.calls = []

            async def route_stream(self, **kwargs):
                self.calls.append(kwargs)
                yield TextMessageEvent(run_id="run-1", content="delegated result")

        router = _Router()
        req = DelegateRequest(
            target_worker="target-1",
            task="analyze data",
            context=(("thread_id", "t-1"), ("priority", "high")),
            mode="sync",
        )
        result = await execute_delegation(
            request=req,
            worker_registry=registry,
            source_worker_id="source-1",
            tenant_id="t1",
            worker_router=router,
        )

        assert result.status == "completed"
        assert result.result == "delegated result"
        assert len(router.calls) == 1
        assert router.calls[0]["task_context"] == "thread_id: t-1\npriority: high"
        assert router.calls[0]["subagent_depth"] == 1

    @pytest.mark.asyncio
    async def test_sync_surfaces_worker_router_errors(self):
        """WorkerRouter errors should be reflected as delegation errors."""
        registry = _make_registry(_make_worker("target-1"))

        class _Router:
            async def route_stream(self, **kwargs):
                yield ErrorEvent(run_id="run-1", code="FAILED", message="router failed")

        req = DelegateRequest(
            target_worker="target-1",
            task="analyze data",
            mode="sync",
        )
        result = await execute_delegation(
            request=req,
            worker_registry=registry,
            source_worker_id="source-1",
            tenant_id="t1",
            worker_router=_Router(),
        )

        assert result.status == "error"
        assert result.error == "router failed"


class TestAsyncMode:
    @pytest.mark.asyncio
    async def test_async_returns_submitted(self):
        """Async mode returns submitted with task_id immediately."""
        registry = _make_registry(_make_worker("target-1"))
        req = DelegateRequest(
            target_worker="target-1",
            task="background task",
            mode="async",
        )
        result = await execute_delegation(
            request=req,
            worker_registry=registry,
            source_worker_id="source-1",
            tenant_id="t1",
        )
        assert result.status == "submitted"
        assert result.task_id != ""
        assert result.error == ""


class TestDelegationDepthIncrement:
    @pytest.mark.asyncio
    async def test_depth_increments_on_delegation(self):
        """Target worker receives delegation_depth + 1."""
        import src.tools.builtin.agent_tool as agent_tool_mod

        captured_depth: list[int] = []
        original_fn = agent_tool_mod._run_delegated_task

        async def _capture_depth(**kwargs):
            captured_depth.append(kwargs.get("delegation_depth", -1))
            return DelegateResult(
                status="completed",
                task_id="test-id",
                result="done",
            )

        agent_tool_mod._run_delegated_task = _capture_depth  # type: ignore

        try:
            registry = _make_registry(_make_worker("target-1"))
            req = DelegateRequest(
                target_worker="target-1",
                task="test",
                mode="sync",
                delegation_depth=0,
            )
            await execute_delegation(
                request=req,
                worker_registry=registry,
                source_worker_id="source-1",
                tenant_id="t1",
            )
            assert captured_depth == [1], (
                f"Expected depth=1, got {captured_depth}"
            )
        finally:
            agent_tool_mod._run_delegated_task = original_fn  # type: ignore


# ---------------------------------------------------------------------------
# Notification mode tests
# ---------------------------------------------------------------------------

class TestNotificationMode:
    @pytest.mark.asyncio
    async def test_notification_publishes_event(self):
        """send_notification publishes event via EventBus."""
        bus = EventBus()
        received: list = []

        async def handler(event):
            received.append(event)

        from src.events.bus import Subscription
        bus.subscribe(Subscription(
            handler_id="test-handler",
            event_type="worker.task_completed",
            tenant_id="t1",
            handler=handler,
        ))

        event_id = await send_notification(
            event_bus=bus,
            source_worker_id="worker-a",
            tenant_id="t1",
            event_type="worker.task_completed",
            payload=(("task_id", "123"), ("result", "ok")),
        )

        assert event_id != ""
        assert len(received) == 1
        assert received[0].type == "worker.task_completed"
        assert received[0].source == "worker-a"
        assert received[0].tenant_id == "t1"

    @pytest.mark.asyncio
    async def test_notification_returns_event_id(self):
        """send_notification returns a non-empty event_id."""
        bus = EventBus()
        event_id = await send_notification(
            event_bus=bus,
            source_worker_id="worker-a",
            tenant_id="t1",
            event_type="worker.status_update",
        )
        assert isinstance(event_id, str)
        assert len(event_id) > 0
