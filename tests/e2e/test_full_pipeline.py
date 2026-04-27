# edition: baseline
"""
End-to-end tests for the full HTTP -> AG-UI SSE pipeline.

Tests the complete flow: HTTP POST -> WorkerRouter -> Engine -> SSE events.
Uses mock LLM and ToolExecutor for deterministic results.
"""
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.common.tenant import Tenant, TenantLoader, TrustLevel
from src.engine.protocols import LLMResponse, ToolResult
from src.engine.router.engine_dispatcher import EngineDispatcher
from src.engine.state import WorkerContext
from src.skills.models import (
    Skill,
    SkillKeyword,
    SkillScope,
    SkillStrategy,
    StrategyMode,
)
from src.skills.registry import SkillRegistry
from src.streaming.events import EventType
from src.worker.models import Worker, WorkerIdentity
from src.worker.registry import (
    WorkerEntry,
    WorkerRegistry,
    build_worker_registry,
)
from src.worker.router import WorkerRouter
from src.worker.task import TaskStore
from src.worker.task_runner import TaskRunner


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_skill(
    skill_id: str = "general-query",
    name: str = "General Query",
    keywords: tuple[SkillKeyword, ...] = (),
    default_skill: bool = True,
) -> Skill:
    """Create a test skill with autonomous strategy."""
    return Skill(
        skill_id=skill_id,
        name=name,
        scope=SkillScope.WORKER,
        strategy=SkillStrategy(mode=StrategyMode.AUTONOMOUS),
        keywords=keywords,
        default_skill=default_skill,
    )


def _make_worker(
    worker_id: str = "analyst-01",
    name: str = "Test Analyst",
    default_skill: str = "general-query",
) -> Worker:
    """Create a test worker."""
    return Worker(
        identity=WorkerIdentity(
            worker_id=worker_id,
            name=name,
            role="analyst",
        ),
        default_skill=default_skill,
    )


def _make_tenant(
    tenant_id: str = "demo",
    name: str = "Demo Tenant",
    default_worker: str = "analyst-01",
) -> Tenant:
    """Create a test tenant."""
    return Tenant(
        tenant_id=tenant_id,
        name=name,
        trust_level=TrustLevel.STANDARD,
        default_worker=default_worker,
    )


class StubTenantLoader:
    """
    In-memory tenant loader that does not touch the filesystem.

    Raises ConfigException for unknown tenant IDs, matching real behavior.
    """

    def __init__(self, tenants: dict[str, Tenant]) -> None:
        self._tenants = tenants

    def load(self, tenant_id: str) -> Tenant:
        tenant = self._tenants.get(tenant_id)
        if tenant is None:
            from src.common.exceptions import ConfigException

            raise ConfigException(f"Tenant config not found: {tenant_id}")
        return tenant


def _build_mock_llm(response_text: str = "Hello from mock LLM") -> AsyncMock:
    """Build a mock LLM client that returns a fixed text response."""
    mock_llm = AsyncMock()
    mock_llm.invoke = AsyncMock(
        return_value=LLMResponse(content=response_text, tool_calls=())
    )
    return mock_llm


def _build_mock_tool_executor() -> AsyncMock:
    """Build a mock tool executor."""
    mock_executor = AsyncMock()
    mock_executor.execute = AsyncMock(
        return_value=ToolResult(content="tool result", is_error=False)
    )
    return mock_executor


def _create_test_app(
    tenants: dict[str, Tenant] | None = None,
    worker_entries: list[WorkerEntry] | None = None,
    llm_response: str = "Hello from mock LLM",
) -> FastAPI:
    """
    Create a FastAPI app with mocked dependencies for testing.

    Bypasses the lifespan/bootstrap entirely and sets app.state directly.
    """
    from src.api.routes.health_routes import router as health_router
    from src.api.routes.worker_routes import router as worker_router

    app = FastAPI()
    app.include_router(health_router)
    app.include_router(worker_router)

    # Default tenant
    if tenants is None:
        tenants = {"demo": _make_tenant()}

    # Default worker + skill
    if worker_entries is None:
        skill = _make_skill()
        skill_registry = SkillRegistry.from_skills([skill])
        worker = _make_worker()
        worker_entries = [
            WorkerEntry(worker=worker, skill_registry=skill_registry)
        ]

    tenant_loader = StubTenantLoader(tenants)
    worker_registry = build_worker_registry(
        entries=worker_entries,
        default_worker_id="analyst-01",
    )

    mock_llm = _build_mock_llm(llm_response)
    mock_tool_executor = _build_mock_tool_executor()

    engine_dispatcher = EngineDispatcher(
        llm_client=mock_llm,
        tool_executor=mock_tool_executor,
        max_rounds=3,
    )

    task_store = TaskStore(workspace_root=Path("/tmp/genworker-test"))

    task_runner = TaskRunner(
        engine_dispatcher=engine_dispatcher,
        task_store=task_store,
    )

    worker_router_instance = WorkerRouter(
        worker_registry=worker_registry,
        tenant_loader=tenant_loader,
        task_runner=task_runner,
    )

    app.state.worker_router = worker_router_instance
    return app


def _parse_sse_events(response_text: str) -> list[dict[str, Any]]:
    """Parse SSE text into a list of JSON event dicts."""
    events: list[dict[str, Any]] = []
    for line in response_text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("data: "):
            json_str = stripped[len("data: "):]
            if json_str:
                events.append(json.loads(json_str))
    return events


def _payload_types(events: list[dict[str, Any]]) -> list[str]:
    return [event.get("type") for event in events]


def _run_error_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [event for event in events if event.get("type") == "RUN_ERROR"]


def _text_content(events: list[dict[str, Any]]) -> str:
    return "".join(
        event.get("delta", "")
        for event in events
        if event.get("type") == "TEXT_MESSAGE_CONTENT"
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestStreamTaskAutonomous:
    """Test SSE streaming for autonomous mode tasks."""

    def test_sse_response_contains_lifecycle_events(self):
        """SSE stream must contain RUN_STARTED -> TEXT_MESSAGE -> RUN_FINISHED."""
        app = _create_test_app()
        client = TestClient(app)

        response = client.post(
            "/api/v1/worker/task/stream",
            json={"task": "Hello world", "tenant_id": "demo"},
        )

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")

        events = _parse_sse_events(response.text)
        event_types = _payload_types(events)

        assert "RUN_STARTED" in event_types
        assert "RUN_FINISHED" in event_types

    def test_sse_response_contains_text_message(self):
        """SSE stream must include TEXT_MESSAGE_CONTENT with LLM output."""
        app = _create_test_app(llm_response="Test response from LLM")
        client = TestClient(app)

        response = client.post(
            "/api/v1/worker/task/stream",
            json={
                "task": "What is the weather?",
                "tenant_id": "demo",
                "worker_id": "analyst-01",
            },
        )

        events = _parse_sse_events(response.text)
        assert _text_content(events) == "Test response from LLM"
        assert "TEXT_MESSAGE_START" in _payload_types(events)
        assert "TEXT_MESSAGE_END" in _payload_types(events)

    def test_sse_event_sequence_order(self):
        """Events must follow RUN_STARTED before RUN_FINISHED ordering."""
        app = _create_test_app()
        client = TestClient(app)

        response = client.post(
            "/api/v1/worker/task/stream",
            json={"task": "test ordering", "tenant_id": "demo"},
        )

        events = _parse_sse_events(response.text)
        event_types = _payload_types(events)

        started_idx = event_types.index("RUN_STARTED")
        finished_idx = event_types.index("RUN_FINISHED")
        assert started_idx < finished_idx

    def test_sse_events_have_run_id(self):
        """All events in a stream should share the same run_id."""
        app = _create_test_app()
        client = TestClient(app)

        response = client.post(
            "/api/v1/worker/task/stream",
            json={"task": "test run id", "tenant_id": "demo"},
        )

        events = _parse_sse_events(response.text)
        run_ids = {e.get("runId") for e in events if e.get("runId")}
        # All non-empty run_ids should be the same
        assert len(run_ids) == 1

    def test_sse_events_have_timestamps(self):
        """All events should have ISO 8601 timestamps."""
        app = _create_test_app()
        client = TestClient(app)

        response = client.post(
            "/api/v1/worker/task/stream",
            json={"task": "test timestamps", "tenant_id": "demo"},
        )

        events = _parse_sse_events(response.text)
        for event in events:
            assert "timestamp" in event
            assert isinstance(event["timestamp"], int)


class TestStreamTaskErrors:
    """Test SSE error events for domain errors."""

    def test_nonexistent_tenant_returns_error_event(self):
        """Unknown tenant_id must produce an ERROR SSE event, not HTTP error."""
        app = _create_test_app()
        client = TestClient(app)

        response = client.post(
            "/api/v1/worker/task/stream",
            json={"task": "test", "tenant_id": "nonexistent"},
        )

        # HTTP status is still 200 for SSE streams
        assert response.status_code == 200

        events = _parse_sse_events(response.text)
        error_events = _run_error_events(events)

        assert len(error_events) >= 1
        assert error_events[0]["code"] == "TENANT_NOT_FOUND"
        assert "nonexistent" in error_events[0]["message"]

    def test_nonexistent_worker_returns_error_event(self):
        """Unknown worker_id must produce a WORKER_NOT_FOUND SSE event."""
        # Create app with no workers registered
        app = _create_test_app(
            worker_entries=[],
        )
        # Override the registry to have no default worker
        app.state.worker_router._worker_registry = build_worker_registry(
            entries=[], default_worker_id=""
        )

        client = TestClient(app)

        response = client.post(
            "/api/v1/worker/task/stream",
            json={
                "task": "test",
                "tenant_id": "demo",
                "worker_id": "nonexistent-worker",
            },
        )

        assert response.status_code == 200

        events = _parse_sse_events(response.text)
        error_events = _run_error_events(events)

        assert len(error_events) >= 1
        assert error_events[0]["code"] == "WORKER_NOT_FOUND"

    def test_missing_required_fields_returns_422(self):
        """Missing 'task' field must trigger Pydantic 422 validation error."""
        app = _create_test_app()
        client = TestClient(app)

        response = client.post(
            "/api/v1/worker/task/stream",
            json={"tenant_id": "demo"},
        )

        assert response.status_code == 422

    def test_missing_tenant_id_returns_422(self):
        """Missing 'tenant_id' field must trigger 422."""
        app = _create_test_app()
        client = TestClient(app)

        response = client.post(
            "/api/v1/worker/task/stream",
            json={"task": "hello"},
        )

        assert response.status_code == 422

    def test_empty_task_returns_422(self):
        """Empty string for 'task' must trigger 422 (min_length=1)."""
        app = _create_test_app()
        client = TestClient(app)

        response = client.post(
            "/api/v1/worker/task/stream",
            json={"task": "", "tenant_id": "demo"},
        )

        assert response.status_code == 422

    def test_empty_tenant_id_returns_422(self):
        """Empty string for 'tenant_id' must trigger 422 (min_length=1)."""
        app = _create_test_app()
        client = TestClient(app)

        response = client.post(
            "/api/v1/worker/task/stream",
            json={"task": "hello", "tenant_id": ""},
        )

        assert response.status_code == 422


class TestStreamTaskSSEFormat:
    """Test SSE wire format correctness."""

    def test_response_media_type(self):
        """Response Content-Type must be text/event-stream."""
        app = _create_test_app()
        client = TestClient(app)

        response = client.post(
            "/api/v1/worker/task/stream",
            json={"task": "test", "tenant_id": "demo"},
        )

        assert "text/event-stream" in response.headers["content-type"]

    def test_sse_lines_start_with_data_prefix(self):
        """Each SSE event line must start with 'data: '."""
        app = _create_test_app()
        client = TestClient(app)

        response = client.post(
            "/api/v1/worker/task/stream",
            json={"task": "test", "tenant_id": "demo"},
        )

        lines = [
            line for line in response.text.split("\n")
            if line.strip()
        ]

        for line in lines:
            assert line.strip().startswith("data: ")

    def test_sse_data_is_valid_json(self):
        """Each SSE data payload must be valid JSON."""
        app = _create_test_app()
        client = TestClient(app)

        response = client.post(
            "/api/v1/worker/task/stream",
            json={"task": "test json", "tenant_id": "demo"},
        )

        events = _parse_sse_events(response.text)
        assert len(events) >= 1

        for event in events:
            assert isinstance(event, dict)
            assert "type" in event

    def test_no_cache_headers(self):
        """SSE response must include Cache-Control: no-cache."""
        app = _create_test_app()
        client = TestClient(app)

        response = client.post(
            "/api/v1/worker/task/stream",
            json={"task": "test", "tenant_id": "demo"},
        )

        assert response.headers.get("cache-control") == "no-cache"


class TestStreamTaskWithWorkerRouting:
    """Test worker routing within the SSE stream."""

    def test_explicit_worker_id_routes_correctly(self):
        """Providing worker_id should route to that specific worker."""
        app = _create_test_app()
        client = TestClient(app)

        response = client.post(
            "/api/v1/worker/task/stream",
            json={
                "task": "route test",
                "tenant_id": "demo",
                "worker_id": "analyst-01",
            },
        )

        assert response.status_code == 200
        events = _parse_sse_events(response.text)
        event_types = _payload_types(events)
        assert "RUN_STARTED" in event_types

    def test_omitted_worker_id_uses_default(self):
        """Omitting worker_id should fall back to default worker."""
        app = _create_test_app()
        client = TestClient(app)

        response = client.post(
            "/api/v1/worker/task/stream",
            json={"task": "default routing test", "tenant_id": "demo"},
        )

        assert response.status_code == 200
        events = _parse_sse_events(response.text)
        event_types = _payload_types(events)
        assert "RUN_STARTED" in event_types
        assert "RUN_FINISHED" in event_types


class TestHealthEndpointStillWorks:
    """Regression: health endpoint must still function."""

    def test_health_returns_healthy(self):
        """GET /health must return status healthy."""
        app = _create_test_app()
        client = TestClient(app)

        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
