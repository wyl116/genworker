# edition: baseline
"""
Integration tests for the worker task stream endpoint in service mode.
"""
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.routes.worker_routes import router as worker_router_route
from src.common.tenant import Tenant, TenantLoader, TrustLevel
from src.conversation.session_manager import SessionManager
from src.conversation.session_store import FileSessionStore
from src.engine.protocols import LLMResponse, ToolResult, UsageInfo
from src.engine.router.engine_dispatcher import EngineDispatcher
from src.skills.models import Skill, SkillKeyword, SkillScope, SkillStrategy, StrategyMode
from src.skills.registry import SkillRegistry
from src.worker.contacts.registry import ContactRegistry
from src.worker.models import ServiceConfig, Worker, WorkerIdentity, WorkerMode
from src.worker.registry import WorkerEntry, build_worker_registry
from src.worker.router import WorkerRouter
from src.worker.task import TaskStore
from src.worker.task_runner import TaskRunner


class MockLLMClient:
    async def invoke(self, messages, tools=None, tool_choice=None, system_blocks=None, intent=None):
        return LLMResponse(
            content="Task response.",
            tool_calls=(),
            usage=UsageInfo(prompt_tokens=10, completion_tokens=20, total_tokens=30),
        )


class MockToolExecutor:
    async def execute(self, tool_name, tool_input):
        return ToolResult(content=f"Executed {tool_name}", is_error=False)


def _make_skill() -> Skill:
    return Skill(
        skill_id="general-query",
        name="General Query",
        scope=SkillScope.SYSTEM,
        keywords=(SkillKeyword(keyword="refund", weight=1.0),),
        strategy=SkillStrategy(mode=StrategyMode.AUTONOMOUS),
        default_skill=True,
    )


@pytest.fixture
def tmp_workspace(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def service_task_app(tmp_workspace: Path) -> FastAPI:
    app = FastAPI()
    app.include_router(worker_router_route)

    worker = Worker(
        identity=WorkerIdentity(name="Service Worker", worker_id="svc-task"),
        mode=WorkerMode.SERVICE,
        service_config=ServiceConfig(session_ttl=900, max_concurrent_sessions=1),
        default_skill="general-query",
    )
    skill = _make_skill()
    registry = SkillRegistry.from_skills([skill])
    entry = WorkerEntry(worker=worker, skill_registry=registry)
    worker_registry = build_worker_registry(
        entries=[entry], default_worker_id="svc-task",
    )

    tenant = Tenant(
        tenant_id="demo",
        name="Demo",
        trust_level=TrustLevel.STANDARD,
        default_worker="svc-task",
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
    contact_registry = ContactRegistry(tmp_workspace / "service-task-contacts")
    worker_router = WorkerRouter(
        worker_registry=worker_registry,
        tenant_loader=tenant_loader,
        task_runner=runner,
        contact_registries={"svc-task": contact_registry},
    )
    session_manager = SessionManager(store=FileSessionStore(tmp_workspace))

    app.state.worker_router = worker_router
    app.state.session_manager = session_manager
    app.state.task_store = task_store
    return app


@pytest.fixture
def service_task_client(service_task_app: FastAPI) -> TestClient:
    return TestClient(service_task_app)


def _parse_sse_events(text: str) -> list[dict]:
    import json

    events = []
    for line in text.split("\n"):
        line = line.strip()
        if line.startswith("data: "):
            try:
                events.append(json.loads(line[6:]))
            except json.JSONDecodeError:
                pass
    return events


def _payload_types(events: list[dict]) -> list[str]:
    return [event.get("type") for event in events]


def _run_error_events(events: list[dict]) -> list[dict]:
    return [event for event in events if event.get("type") == "RUN_ERROR"]


def test_worker_task_stream_builds_service_profile_and_session(service_task_client: TestClient, service_task_app: FastAPI):
    response = service_task_client.post(
        "/api/v1/worker/task/stream",
        json={
            "task": "我是张三，工号 E001，退款还没到账",
            "tenant_id": "demo",
            "channel_type": "feishu",
            "channel_id": "ou_task_1",
            "topic": "refund",
        },
    )

    assert response.status_code == 200
    events = _parse_sse_events(response.text)
    assert "RUN_STARTED" in _payload_types(events)

    session_manager = service_task_app.state.session_manager
    session = session_manager._cache["service:feishu:ou_task_1"]
    assert session.ttl_seconds == 900
    assert dict(session.metadata)["service_profile_context"]

    contact_registry = service_task_app.state.worker_router.get_contact_registry("svc-task")
    contacts = contact_registry.search_contacts(query="E001")
    assert len(contacts) == 1
    assert contacts[0].primary_name == "张三"


def test_worker_task_stream_queues_when_service_busy(service_task_client: TestClient):
    first = service_task_client.post(
        "/api/v1/worker/task/stream",
        json={
            "task": "我是李四，工号 E002，退款问题",
            "tenant_id": "demo",
            "channel_type": "feishu",
            "channel_id": "ou_task_busy_1",
        },
    )
    assert first.status_code == 200

    second = service_task_client.post(
        "/api/v1/worker/task/stream",
        json={
            "task": "我是王五，工号 E003，退款问题",
            "tenant_id": "demo",
            "channel_type": "feishu",
            "channel_id": "ou_task_busy_2",
        },
    )
    assert second.status_code == 200
    events = _parse_sse_events(second.text)
    assert any(event.get("code") == "SERVICE_QUEUED" for event in _run_error_events(events))


def test_worker_task_stream_requires_auth_when_configured(
    service_task_client: TestClient,
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

    response = service_task_client.post(
        "/api/v1/worker/task/stream",
        json={
            "task": "我是张三，工号 E001，退款还没到账",
            "tenant_id": "demo",
            "worker_id": "svc-task",
        },
    )

    assert response.status_code == 401


def test_worker_task_stream_enforces_worker_scope(
    service_task_client: TestClient,
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

    response = service_task_client.post(
        "/api/v1/worker/task/stream",
        headers={"Authorization": "Bearer secret-token"},
        json={
            "task": "我是张三，工号 E001，退款还没到账",
            "tenant_id": "demo",
            "worker_id": "svc-task",
        },
    )

    assert response.status_code == 403
