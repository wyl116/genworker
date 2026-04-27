# edition: baseline
"""
Integration tests for the chat stream endpoint.

Tests the /api/v1/chat/stream and /api/v1/chat/{thread_id}/tasks routes
with mock dependencies.
"""
from dataclasses import replace
import json
from pathlib import Path
import threading
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.routes.chat_routes import ChatRequest, router as chat_router
from src.common.tenant import Tenant, TenantLoader, TrustLevel
from src.conversation.models import ConversationSession
from src.conversation.session_manager import SessionManager
from src.conversation.session_store import FileSessionStore
from src.conversation.task_spawner import TaskSpawner
from src.engine.protocols import LLMResponse, ToolResult, UsageInfo
from src.engine.router.engine_dispatcher import EngineDispatcher
from src.skills.models import Skill, SkillKeyword, SkillScope, SkillStrategy, StrategyMode
from src.skills.registry import SkillRegistry
from src.streaming.events import EventType, RunFinishedEvent, RunStartedEvent, TaskSpawnedEvent, TextMessageEvent
from src.worker.contacts.registry import ContactRegistry
from src.worker.models import ServiceConfig, Worker, WorkerIdentity, WorkerMode
from src.worker.registry import WorkerEntry, build_worker_registry
from src.worker.router import WorkerRouter
from src.worker.task import TaskStore
from src.worker.task_runner import TaskRunner


# --- Mocks ---

class MockLLMClient:
    def __init__(self, response_text: str = "Chat response."):
        self._response_text = response_text

    async def invoke(self, messages, tools=None, tool_choice=None, system_blocks=None, intent=None):
        return LLMResponse(
            content=self._response_text,
            tool_calls=(),
            usage=UsageInfo(prompt_tokens=10, completion_tokens=20, total_tokens=30),
        )


class MockToolExecutor:
    async def execute(self, tool_name, tool_input):
        return ToolResult(content=f"Executed {tool_name}", is_error=False)


# --- Fixtures ---

def _make_skill(
    skill_id: str = "general-query",
    name: str = "General Query",
    keywords: tuple[SkillKeyword, ...] = (),
    default_skill: bool = True,
) -> Skill:
    return Skill(
        skill_id=skill_id,
        name=name,
        scope=SkillScope.SYSTEM,
        keywords=keywords or (
            SkillKeyword(keyword="analyze", weight=1.0),
            SkillKeyword(keyword="help", weight=0.8),
        ),
        strategy=SkillStrategy(mode=StrategyMode.AUTONOMOUS),
        default_skill=default_skill,
    )


@pytest.fixture
def tmp_workspace(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def app(tmp_workspace: Path) -> FastAPI:
    """Build a FastAPI app with all necessary state."""
    app = FastAPI()
    app.include_router(chat_router)

    # Worker setup
    worker = Worker(
        identity=WorkerIdentity(name="Test Worker", worker_id="w1"),
        default_skill="general-query",
    )
    skill = _make_skill()
    registry = SkillRegistry.from_skills([skill])
    entry = WorkerEntry(worker=worker, skill_registry=registry)
    worker_registry = build_worker_registry(
        entries=[entry], default_worker_id="w1",
    )

    # Tenant setup
    tenant = Tenant(
        tenant_id="demo",
        name="Demo",
        trust_level=TrustLevel.STANDARD,
        default_worker="w1",
    )
    tenant_loader = TenantLoader(tmp_workspace)
    tenant_loader._cache["demo"] = tenant

    # Engine setup
    dispatcher = EngineDispatcher(
        llm_client=MockLLMClient(),
        tool_executor=MockToolExecutor(),
    )
    task_store = TaskStore(workspace_root=tmp_workspace)
    runner = TaskRunner(
        engine_dispatcher=dispatcher,
        task_store=task_store,
    )
    worker_router = WorkerRouter(
        worker_registry=worker_registry,
        tenant_loader=tenant_loader,
        task_runner=runner,
    )

    # Conversation setup
    store = FileSessionStore(tmp_workspace)
    session_manager = SessionManager(store=store)

    # Attach to app.state
    app.state.worker_router = worker_router
    app.state.session_manager = session_manager
    app.state.task_store = task_store

    return app


@pytest.fixture
def service_app(tmp_workspace: Path) -> FastAPI:
    """Build a FastAPI app backed by a service-mode worker."""
    app = FastAPI()
    app.include_router(chat_router)

    worker = Worker(
        identity=WorkerIdentity(name="Service Worker", worker_id="svc-1"),
        mode=WorkerMode.SERVICE,
        service_config=ServiceConfig(session_ttl=900, max_concurrent_sessions=50),
        default_skill="general-query",
    )
    skill = _make_skill()
    registry = SkillRegistry.from_skills([skill])
    entry = WorkerEntry(worker=worker, skill_registry=registry)
    worker_registry = build_worker_registry(
        entries=[entry], default_worker_id="svc-1",
    )

    tenant = Tenant(
        tenant_id="demo",
        name="Demo",
        trust_level=TrustLevel.STANDARD,
        default_worker="svc-1",
    )
    tenant_loader = TenantLoader(tmp_workspace)
    tenant_loader._cache["demo"] = tenant

    dispatcher = EngineDispatcher(
        llm_client=MockLLMClient(),
        tool_executor=MockToolExecutor(),
    )
    task_store = TaskStore(workspace_root=tmp_workspace)
    runner = TaskRunner(
        engine_dispatcher=dispatcher,
        task_store=task_store,
    )
    contact_registry = ContactRegistry(tmp_workspace / "service-contacts")
    worker_router = WorkerRouter(
        worker_registry=worker_registry,
        tenant_loader=tenant_loader,
        task_runner=runner,
        contact_registries={"svc-1": contact_registry},
    )

    store = FileSessionStore(tmp_workspace)
    session_manager = SessionManager(store=store)

    app.state.worker_router = worker_router
    app.state.session_manager = session_manager
    app.state.task_store = task_store
    return app


@pytest.fixture
def service_client(service_app: FastAPI) -> TestClient:
    return TestClient(service_app)


@pytest.fixture
def limited_service_app(tmp_workspace: Path) -> FastAPI:
    """Service app with a strict max_concurrent_sessions limit."""
    app = FastAPI()
    app.include_router(chat_router)

    worker = Worker(
        identity=WorkerIdentity(name="Limited Service", worker_id="svc-limit"),
        mode=WorkerMode.SERVICE,
        service_config=ServiceConfig(session_ttl=900, max_concurrent_sessions=1),
        default_skill="general-query",
    )
    skill = _make_skill()
    registry = SkillRegistry.from_skills([skill])
    entry = WorkerEntry(worker=worker, skill_registry=registry)
    worker_registry = build_worker_registry(
        entries=[entry], default_worker_id="svc-limit",
    )
    tenant = Tenant(
        tenant_id="demo",
        name="Demo",
        trust_level=TrustLevel.STANDARD,
        default_worker="svc-limit",
    )
    tenant_loader = TenantLoader(tmp_workspace)
    tenant_loader._cache["demo"] = tenant

    dispatcher = EngineDispatcher(
        llm_client=MockLLMClient(),
        tool_executor=MockToolExecutor(),
    )
    task_store = TaskStore(workspace_root=tmp_workspace)
    runner = TaskRunner(
        engine_dispatcher=dispatcher,
        task_store=task_store,
    )
    contact_registry = ContactRegistry(tmp_workspace / "limited-service-contacts")
    worker_router = WorkerRouter(
        worker_registry=worker_registry,
        tenant_loader=tenant_loader,
        task_runner=runner,
        contact_registries={"svc-limit": contact_registry},
    )
    store = FileSessionStore(tmp_workspace)
    session_manager = SessionManager(store=store)

    app.state.worker_router = worker_router
    app.state.session_manager = session_manager
    app.state.task_store = task_store
    return app


@pytest.fixture
def limited_service_client(limited_service_app: FastAPI) -> TestClient:
    return TestClient(limited_service_app)


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


# --- Tests ---

class TestChatStreamEndpoint:
    """Integration tests for POST /api/v1/chat/stream."""

    def test_chat_stream_returns_sse(self, client: TestClient):
        """Chat stream endpoint returns SSE events."""
        response = client.post(
            "/api/v1/chat/stream",
            json={
                "message": "Analyze the data trends",
                "thread_id": "conv-001",
                "tenant_id": "demo",
            },
        )

        assert response.status_code == 200
        assert "text/event-stream" in response.headers["content-type"]

        # Parse SSE events
        events = _parse_sse_events(response.text)
        assert len(events) > 0

        # Should have at least RUN_STARTED
        event_types = _payload_types(events)
        assert "RUN_STARTED" in event_types

    def test_chat_stream_with_worker_id(self, client: TestClient):
        """Chat stream with explicit worker_id."""
        response = client.post(
            "/api/v1/chat/stream",
            json={
                "message": "Help me with analysis",
                "thread_id": "conv-002",
                "tenant_id": "demo",
                "worker_id": "w1",
            },
        )

        assert response.status_code == 200
        events = _parse_sse_events(response.text)
        assert len(events) > 0

    def test_chat_stream_persists_assistant_reply_and_spawned_tasks(
        self,
        app: FastAPI,
        client: TestClient,
    ):
        async def _route_stream(**kwargs):
            yield RunStartedEvent(run_id="run-persist")
            yield TextMessageEvent(run_id="run-persist", content="First chunk")
            yield TextMessageEvent(run_id="run-persist", content="Second chunk")
            yield TaskSpawnedEvent(
                run_id="run-persist",
                task_id="task-123",
                task_description="Background follow-up",
            )
            yield RunFinishedEvent(run_id="run-persist", success=True)

        app.state.worker_router.route_stream = _route_stream

        response = client.post(
            "/api/v1/chat/stream",
            json={
                "message": "Handle this and create a follow-up task",
                "thread_id": "conv-persist",
                "tenant_id": "demo",
            },
        )

        assert response.status_code == 200
        session = app.state.session_manager._cache["conv-persist"]
        assert tuple(msg.role for msg in session.messages) == ("user", "assistant")
        assert session.messages[0].content == "Handle this and create a follow-up task"
        assert session.messages[1].content == "First chunk\n\nSecond chunk"
        assert session.spawned_tasks == ("task-123",)

    def test_chat_stream_validation_error(self, client: TestClient):
        """Missing required fields return 422."""
        response = client.post(
            "/api/v1/chat/stream",
            json={"message": "hello"},  # missing thread_id, tenant_id
        )
        assert response.status_code == 422

    def test_chat_stream_requires_auth_when_configured(
        self,
        client: TestClient,
        monkeypatch,
    ):
        monkeypatch.setattr(
            "src.api.auth.get_settings",
            lambda: SimpleNamespace(
                api_bearer_token="secret-token",
                api_key="",
                api_worker_scope="*",
            ),
        )

        response = client.post(
            "/api/v1/chat/stream",
            json={
                "message": "Analyze the data trends",
                "thread_id": "conv-auth-001",
                "tenant_id": "demo",
            },
        )

        assert response.status_code == 401

    def test_chat_stream_accepts_api_key_when_configured(
        self,
        client: TestClient,
        monkeypatch,
    ):
        monkeypatch.setattr(
            "src.api.auth.get_settings",
            lambda: SimpleNamespace(
                api_bearer_token="",
                api_key="secret-key",
                api_worker_scope="*",
            ),
        )

        response = client.post(
            "/api/v1/chat/stream",
            headers={"X-API-Key": "secret-key"},
            json={
                "message": "Analyze the data trends",
                "thread_id": "conv-auth-002",
                "tenant_id": "demo",
            },
        )

        assert response.status_code == 200

    def test_chat_stream_enforces_worker_scope_when_worker_id_provided(
        self,
        client: TestClient,
        monkeypatch,
    ):
        monkeypatch.setattr(
            "src.api.auth.get_settings",
            lambda: SimpleNamespace(
                api_bearer_token="secret-token",
                api_key="",
                api_worker_scope="analyst-01",
            ),
        )

        response = client.post(
            "/api/v1/chat/stream",
            headers={"Authorization": "Bearer secret-token"},
            json={
                "message": "Analyze the data trends",
                "thread_id": "conv-auth-003",
                "tenant_id": "demo",
                "worker_id": "w1",
            },
        )

        assert response.status_code == 403

    def test_chat_and_worker_streams_independent(self, client: TestClient):
        """Chat stream and worker task stream do not interfere."""
        # Chat stream
        resp1 = client.post(
            "/api/v1/chat/stream",
            json={
                "message": "Chat message",
                "thread_id": "conv-003",
                "tenant_id": "demo",
            },
        )
        assert resp1.status_code == 200

        # The worker routes are not registered in this test app,
        # but the chat route works independently
        events = _parse_sse_events(resp1.text)
        assert len(events) > 0

    def test_service_chat_stream_builds_customer_profile_and_session_policy(
        self,
        service_client: TestClient,
        service_app: FastAPI,
    ):
        response = service_client.post(
            "/api/v1/chat/stream",
            json={
                "message": "我是张三，工号 E001，上次的退款还没到账",
                "thread_id": "svc-thread-001",
                "tenant_id": "demo",
                "channel_type": "feishu",
                "channel_id": "ou_xxx",
                "topic": "refund",
            },
        )

        assert response.status_code == 200
        events = _parse_sse_events(response.text)
        assert len(events) > 0

        session_manager = service_app.state.session_manager
        session = session_manager._cache["svc-thread-001"]
        assert session.ttl_seconds == 900
        metadata = dict(session.metadata)
        assert metadata["channel_type"] == "feishu"
        assert "service_profile_context" in metadata

        contact_registry = service_app.state.worker_router.get_contact_registry("svc-1")
        contacts = contact_registry.search_contacts(query="E001")
        assert len(contacts) == 1
        assert contacts[0].primary_name == "张三"

    def test_service_chat_stream_rejects_when_concurrency_limit_reached(
        self,
        limited_service_client: TestClient,
        limited_service_app: FastAPI,
    ):
        first = limited_service_client.post(
            "/api/v1/chat/stream",
            json={
                "message": "第一个客户请求",
                "thread_id": "svc-limit-1",
                "tenant_id": "demo",
                "channel_type": "feishu",
                "channel_id": "ou_first",
            },
        )
        assert first.status_code == 200

        second = limited_service_client.post(
            "/api/v1/chat/stream",
            json={
                "message": "第二个客户请求",
                "thread_id": "svc-limit-2",
                "tenant_id": "demo",
                "channel_type": "feishu",
                "channel_id": "ou_second",
            },
        )
        assert second.status_code == 200

        events = _parse_sse_events(second.text)
        event_types = _payload_types(events)
        assert "RUN_ERROR" in event_types
        error_messages = [event.get("message", "") for event in _run_error_events(events)]
        assert any("queued at position 1" in message for message in error_messages)
        error_codes = [event.get("code", "") for event in _run_error_events(events)]
        assert "SERVICE_QUEUED" in error_codes

        session_manager = limited_service_app.state.session_manager
        assert "svc-limit-2" not in session_manager._cache

        queue_status = limited_service_client.get(
            "/api/v1/chat/svc-limit-2/queue",
            params={"tenant_id": "demo", "worker_id": "svc-limit"},
        )
        assert queue_status.status_code == 200
        assert queue_status.json()["status"] == "queued"
        assert queue_status.json()["position"] == 1

    def test_service_chat_stream_allows_queued_thread_after_slot_frees(
        self,
        limited_service_client: TestClient,
        limited_service_app: FastAPI,
    ):
        first = limited_service_client.post(
            "/api/v1/chat/stream",
            json={
                "message": "第一个客户请求",
                "thread_id": "svc-queue-1",
                "tenant_id": "demo",
                "channel_type": "feishu",
                "channel_id": "ou_first",
            },
        )
        assert first.status_code == 200

        queued = limited_service_client.post(
            "/api/v1/chat/stream",
            json={
                "message": "第二个客户请求",
                "thread_id": "svc-queue-2",
                "tenant_id": "demo",
                "channel_type": "feishu",
                "channel_id": "ou_second",
            },
        )
        assert queued.status_code == 200

        session_manager = limited_service_app.state.session_manager
        first_session = session_manager._cache["svc-queue-1"]
        expired = replace(first_session, last_active_at="2000-01-01T00:00:00+00:00")
        session_manager._cache["svc-queue-1"] = expired
        import asyncio
        asyncio.run(session_manager.save(expired))
        asyncio.run(session_manager.cleanup_expired())

        retried = limited_service_client.post(
            "/api/v1/chat/stream",
            json={
                "message": "第二个客户再次重试",
                "thread_id": "svc-queue-2",
                "tenant_id": "demo",
                "channel_type": "feishu",
                "channel_id": "ou_second",
            },
        )
        assert retried.status_code == 200
        events = _parse_sse_events(retried.text)
        event_types = _payload_types(events)
        assert "RUN_STARTED" in event_types
        assert "svc-queue-2" in session_manager._cache

        queue_status = limited_service_client.get(
            "/api/v1/chat/svc-queue-2/queue",
            params={"tenant_id": "demo", "worker_id": "svc-limit"},
        )
        assert queue_status.status_code == 200
        assert queue_status.json()["status"] == "active"

    def test_service_queue_stream_emits_queued_then_active(
        self,
        limited_service_client: TestClient,
        limited_service_app: FastAPI,
    ):
        first = limited_service_client.post(
            "/api/v1/chat/stream",
            json={
                "message": "第一个客户请求",
                "thread_id": "svc-stream-1",
                "tenant_id": "demo",
                "channel_type": "feishu",
                "channel_id": "ou_first",
            },
        )
        assert first.status_code == 200

        queued = limited_service_client.post(
            "/api/v1/chat/stream",
            json={
                "message": "第二个客户请求",
                "thread_id": "svc-stream-2",
                "tenant_id": "demo",
                "channel_type": "feishu",
                "channel_id": "ou_second",
            },
        )
        assert queued.status_code == 200

        session_manager = limited_service_app.state.session_manager

        def _free_slot() -> None:
            first_session = session_manager._cache["svc-stream-1"]
            expired = replace(first_session, last_active_at="2000-01-01T00:00:00+00:00")
            session_manager._cache["svc-stream-1"] = expired
            import asyncio
            asyncio.run(session_manager.save(expired))
            asyncio.run(session_manager.cleanup_expired())

            limited_service_client.post(
                "/api/v1/chat/stream",
                json={
                    "message": "第二个客户再次重试",
                    "thread_id": "svc-stream-2",
                    "tenant_id": "demo",
                    "channel_type": "feishu",
                    "channel_id": "ou_second",
                },
            )

        timer = threading.Timer(0.2, _free_slot)
        timer.start()
        try:
            response = limited_service_client.get(
                "/api/v1/chat/svc-stream-2/queue/stream",
                params={
                    "tenant_id": "demo",
                    "worker_id": "svc-limit",
                    "timeout_seconds": 2,
                    "poll_interval": 0.1,
                },
            )
        finally:
            timer.join()

        assert response.status_code == 200
        events = _parse_sse_events(response.text)
        queue_statuses = [
            event["value"]["status"]
            for event in _custom_events(events, "queue_status")
        ]
        assert "queued" in queue_statuses
        assert "active" in queue_statuses


class TestListChatTasks:
    """Integration tests for GET /api/v1/chat/{thread_id}/tasks."""

    def test_list_tasks_empty(self, client: TestClient):
        """No spawned tasks returns empty list."""
        response = client.get("/api/v1/chat/conv-empty/tasks")
        assert response.status_code == 200
        assert response.json() == []

    @pytest.mark.asyncio
    async def test_list_tasks_with_spawned_task(
        self, app: FastAPI, client: TestClient, tmp_workspace: Path,
    ):
        """After spawning a task, it appears in the task list."""
        from src.worker.task import create_task_manifest

        session_manager = app.state.session_manager
        task_store = app.state.task_store

        # Create a session with a spawned task
        session = await session_manager.get_or_create(
            "conv-tasks", "demo", "w1",
        )
        manifest = create_task_manifest(
            worker_id="w1",
            tenant_id="demo",
            task_description="Test task",
        )
        manifest = manifest.mark_running()
        manifest = manifest.mark_completed(result_summary="Done")
        task_store.save(manifest)

        session = session.add_spawned_task(manifest.task_id)
        await session_manager.save(session)
        task_id = manifest.task_id

        response = client.get("/api/v1/chat/conv-tasks/tasks")
        assert response.status_code == 200

        tasks = response.json()
        assert len(tasks) == 1
        assert tasks[0]["task_id"] == task_id
        assert tasks[0]["status"] == "completed"

    def test_list_tasks_requires_auth_when_configured(
        self,
        client: TestClient,
        monkeypatch,
    ):
        monkeypatch.setattr(
            "src.api.auth.get_settings",
            lambda: SimpleNamespace(
                api_bearer_token="secret-token",
                api_key="",
                api_worker_scope="*",
            ),
        )

        response = client.get("/api/v1/chat/conv-empty/tasks")

        assert response.status_code == 401


class TestChatQueueEndpoints:
    def test_queue_status_enforces_worker_scope(
        self,
        limited_service_client: TestClient,
        monkeypatch,
    ):
        monkeypatch.setattr(
            "src.api.auth.get_settings",
            lambda: SimpleNamespace(
                api_bearer_token="secret-token",
                api_key="",
                api_worker_scope="analyst-01",
            ),
        )

        response = limited_service_client.get(
            "/api/v1/chat/svc-limit-2/queue",
            headers={"Authorization": "Bearer secret-token"},
            params={"tenant_id": "demo", "worker_id": "svc-limit"},
        )

        assert response.status_code == 403


# --- Helpers ---

def _parse_sse_events(text: str) -> list[dict]:
    """Parse SSE text into a list of JSON event dicts."""
    events = []
    for line in text.split("\n"):
        line = line.strip()
        if line.startswith("data: "):
            try:
                data = json.loads(line[6:])
                events.append(data)
            except json.JSONDecodeError:
                pass
    return events


def _payload_types(events: list[dict]) -> list[str]:
    return [event.get("type") for event in events]


def _run_error_events(events: list[dict]) -> list[dict]:
    return [event for event in events if event.get("type") == "RUN_ERROR"]


def _custom_events(events: list[dict], name: str) -> list[dict]:
    return [
        event for event in events
        if event.get("type") == "CUSTOM" and event.get("name") == name
    ]
