# edition: baseline
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.routes.chat_routes import router as chat_router
from src.conversation.session_manager import SessionManager
from src.conversation.session_store import FileSessionStore
from src.engine.router.engine_dispatcher import EngineDispatcher
from src.runtime.api_wiring import build_llm_client
from src.skills.loader import SkillLoader
from src.skills.registry import SkillRegistry
from src.worker.loader import load_worker_entry
from src.worker.registry import build_worker_registry
from src.worker.router import WorkerRouter
from src.worker.task import TaskStore
from src.worker.task_runner import TaskRunner


def test_chat_stream_returns_smoke_stub_content(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    worker_dir = workspace / "tenants" / "demo" / "workers" / "analyst-01"
    worker_dir.mkdir(parents=True, exist_ok=True)
    (workspace / "tenants" / "demo" / "TENANT.json").write_text(
        (
            "{\n"
            '  "tenant_id": "demo",\n'
            '  "name": "Demo",\n'
            '  "trust_level": 1,\n'
            '  "mcp_remote_allowed": false,\n'
            '  "default_worker": "analyst-01"\n'
            "}\n"
        ),
        encoding="utf-8",
    )
    (workspace / "system" / "skills" / "general-query").mkdir(parents=True, exist_ok=True)
    (workspace / "system" / "skills" / "general-query" / "SKILL.md").write_text(
        (
            "---\n"
            "skill_id: general-query\n"
            "name: General Query\n"
            "default_skill: true\n"
            "strategy:\n"
            "  mode: autonomous\n"
            "---\n"
            "Default skill.\n"
        ),
        encoding="utf-8",
    )
    (worker_dir / "PERSONA.md").write_text(
        (
            "---\n"
            "identity:\n"
            "  worker_id: analyst-01\n"
            "  name: Analyst One\n"
            "default_skill: general-query\n"
            "---\n"
            "Body.\n"
        ),
        encoding="utf-8",
    )

    entry = load_worker_entry(workspace_root=workspace, tenant_id="demo", worker_id="analyst-01", skill_loader=SkillLoader())
    worker_registry = build_worker_registry(entries=[entry], default_worker_id="analyst-01")
    worker_router = WorkerRouter(
        worker_registry=worker_registry,
        tenant_loader=__import__("src.common.tenant", fromlist=["TenantLoader"]).TenantLoader(workspace),
        task_runner=TaskRunner(
            engine_dispatcher=EngineDispatcher(
                llm_client=build_llm_client(SimpleNamespace(
                    settings=SimpleNamespace(community_smoke_profile=True),
                    get_state=lambda _key: None,
                )),
                tool_executor=SimpleNamespace(execute=lambda **_kwargs: None),
            ),
            task_store=TaskStore(workspace),
        ),
    )

    app = FastAPI()
    app.include_router(chat_router)
    app.state.worker_router = worker_router
    app.state.session_manager = SessionManager(FileSessionStore(workspace))
    app.state.task_store = TaskStore(workspace)

    client = TestClient(app)
    response = client.post(
        "/api/v1/chat/stream?protocol=ag-ui",
        json={
            "message": "ping",
            "thread_id": "chat-001",
            "tenant_id": "demo",
            "worker_id": "analyst-01",
        },
    )

    assert response.status_code == 200
    assert "smoke-ok" in response.text
