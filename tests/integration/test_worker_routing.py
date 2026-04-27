# edition: baseline
"""
Integration test for the full worker routing chain.

Tests Worker -> Skill match -> Engine dispatch with mock LLM/ToolExecutor.
Uses real Skill loading from workspace demo data.
"""
import textwrap
from pathlib import Path
from typing import Any

import pytest

from src.common.tenant import Tenant, TenantToolPolicy, TrustLevel, TenantLoader
from src.engine.protocols import LLMClient, LLMResponse, ToolExecutor, ToolResult, UsageInfo
from src.engine.router.engine_dispatcher import EngineDispatcher
from src.engine.state import UsageBudget
from src.skills.loader import SkillLoader
from src.skills.registry import SkillRegistry
from src.streaming.events import (
    ErrorEvent,
    RunFinishedEvent,
    RunStartedEvent,
    TextMessageEvent,
)
from src.worker.context_builder import build_worker_context
from src.worker.models import Worker, WorkerIdentity, WorkerToolPolicy
from src.worker.parser import parse_persona_md
from src.worker.registry import WorkerEntry, WorkerRegistry, build_worker_registry
from src.worker.router import WorkerRouter
from src.worker.task import TaskManifest, TaskStatus, TaskStore, create_task_manifest
from src.worker.task_runner import TaskRunner
from src.worker.tool_sandbox import compute_available_tools
from src.worker.trust_gate import WorkerTrustGate, compute_trust_gate


# --- Mock LLM and ToolExecutor ---

class MockLLMClient:
    """Mock LLM that returns a fixed response."""

    def __init__(self, response_text: str = "Analysis complete.") -> None:
        self._response_text = response_text
        self.call_count = 0

    async def invoke(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        system_blocks: list[dict[str, Any]] | None = None,
        intent=None,
    ) -> LLMResponse:
        self.call_count += 1
        return LLMResponse(
            content=self._response_text,
            tool_calls=(),
            usage=UsageInfo(prompt_tokens=10, completion_tokens=20, total_tokens=30),
        )


class MockToolExecutor:
    """Mock tool executor that always succeeds."""

    async def execute(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
    ) -> ToolResult:
        return ToolResult(content=f"Executed {tool_name}", is_error=False)


# --- Fixtures ---

@pytest.fixture
def workspace_root() -> Path:
    """Return the project workspace root."""
    return Path(__file__).parent.parent.parent / "workspace"


@pytest.fixture
def demo_tenant() -> Tenant:
    """Create a demo tenant matching TENANT.json."""
    return Tenant(
        tenant_id="demo",
        name="Demo Tenant",
        trust_level=TrustLevel.STANDARD,
        tool_policy=TenantToolPolicy(denied_tools=frozenset({"admin_console"})),
        mcp_remote_allowed=False,
        default_worker="analyst-01",
    )


@pytest.fixture
def mock_llm() -> MockLLMClient:
    return MockLLMClient()


@pytest.fixture
def mock_tool_executor() -> MockToolExecutor:
    return MockToolExecutor()


@pytest.fixture
def task_store(tmp_path: Path) -> TaskStore:
    return TaskStore(workspace_root=tmp_path)


@pytest.fixture
def demo_worker(workspace_root: Path) -> Worker:
    """Load the demo worker from PERSONA.md."""
    persona_md = workspace_root / "tenants" / "demo" / "workers" / "analyst-01" / "PERSONA.md"
    return parse_persona_md(persona_md)


@pytest.fixture
def demo_skill_registry(workspace_root: Path) -> SkillRegistry:
    """Build skill registry from demo workspace."""
    loader = SkillLoader()
    system_skills = loader.scan(workspace_root / "system" / "skills")
    worker_skills = loader.scan(
        workspace_root / "tenants" / "demo" / "workers" / "analyst-01" / "skills"
    )
    return SkillRegistry.merge(
        system_skills=system_skills,
        worker_skills=worker_skills,
    )


@pytest.fixture
def demo_worker_entry(demo_worker: Worker, demo_skill_registry: SkillRegistry) -> WorkerEntry:
    return WorkerEntry(worker=demo_worker, skill_registry=demo_skill_registry)


@pytest.fixture
def worker_registry(demo_worker_entry: WorkerEntry) -> WorkerRegistry:
    return build_worker_registry(
        entries=[demo_worker_entry],
        default_worker_id="analyst-01",
    )


# --- Tests ---

class TestFullRoutingChain:
    """Integration tests for the Worker -> Skill -> Engine chain."""

    @pytest.mark.asyncio
    async def test_full_routing_chain_worker_to_engine(
        self,
        workspace_root: Path,
        demo_tenant: Tenant,
        demo_worker: Worker,
        demo_skill_registry: SkillRegistry,
        worker_registry: WorkerRegistry,
        mock_llm: MockLLMClient,
        mock_tool_executor: MockToolExecutor,
        task_store: TaskStore,
    ) -> None:
        """Full chain: route task -> match worker -> match skill -> engine executes."""
        dispatcher = EngineDispatcher(
            llm_client=mock_llm,
            tool_executor=mock_tool_executor,
        )
        runner = TaskRunner(
            engine_dispatcher=dispatcher,
            task_store=task_store,
        )

        # Create a TenantLoader with a pre-populated cache
        tenant_loader = TenantLoader(workspace_root)
        tenant_loader._cache["demo"] = demo_tenant

        router = WorkerRouter(
            worker_registry=worker_registry,
            tenant_loader=tenant_loader,
            task_runner=runner,
            all_tools=(),
        )

        events = []
        async for event in router.route_stream(
            task="Analyze the data trends for Q1",
            tenant_id="demo",
        ):
            events.append(event)

        # Verify we got lifecycle events
        event_types = [type(e).__name__ for e in events]
        assert "RunStartedEvent" in event_types
        assert "RunFinishedEvent" in event_types

        # Verify LLM was called
        assert mock_llm.call_count >= 1

        # Verify text was produced
        text_events = [e for e in events if isinstance(e, TextMessageEvent)]
        assert len(text_events) > 0
        assert "Analysis complete" in text_events[0].content

    @pytest.mark.asyncio
    async def test_routing_with_specific_worker_id(
        self,
        workspace_root: Path,
        demo_tenant: Tenant,
        worker_registry: WorkerRegistry,
        mock_llm: MockLLMClient,
        mock_tool_executor: MockToolExecutor,
        task_store: TaskStore,
    ) -> None:
        """Routing with explicit worker_id skips matching."""
        dispatcher = EngineDispatcher(
            llm_client=mock_llm,
            tool_executor=mock_tool_executor,
        )
        runner = TaskRunner(
            engine_dispatcher=dispatcher,
            task_store=task_store,
        )

        tenant_loader = TenantLoader(workspace_root)
        tenant_loader._cache["demo"] = demo_tenant

        router = WorkerRouter(
            worker_registry=worker_registry,
            tenant_loader=tenant_loader,
            task_runner=runner,
        )

        events = []
        async for event in router.route_stream(
            task="Help me with a question",
            tenant_id="demo",
            worker_id="analyst-01",
        ):
            events.append(event)

        assert any(isinstance(e, RunFinishedEvent) for e in events)

    @pytest.mark.asyncio
    async def test_routing_unknown_tenant_yields_error(
        self,
        tmp_path: Path,
        worker_registry: WorkerRegistry,
        mock_llm: MockLLMClient,
        mock_tool_executor: MockToolExecutor,
        task_store: TaskStore,
    ) -> None:
        """Unknown tenant yields TENANT_NOT_FOUND error event."""
        dispatcher = EngineDispatcher(
            llm_client=mock_llm,
            tool_executor=mock_tool_executor,
        )
        runner = TaskRunner(
            engine_dispatcher=dispatcher,
            task_store=task_store,
        )

        tenant_loader = TenantLoader(tmp_path)

        router = WorkerRouter(
            worker_registry=worker_registry,
            tenant_loader=tenant_loader,
            task_runner=runner,
        )

        events = []
        async for event in router.route_stream(
            task="Some task",
            tenant_id="nonexistent",
        ):
            events.append(event)

        assert len(events) == 1
        assert isinstance(events[0], ErrorEvent)
        assert events[0].code == "TENANT_NOT_FOUND"


class TestTaskManifestStatusFlow:
    """Tests for TaskManifest lifecycle: pending -> running -> completed | error."""

    def test_task_manifest_status_flow_completed(self, tmp_path: Path) -> None:
        """TaskManifest transitions: pending -> running -> completed."""
        manifest = create_task_manifest(
            worker_id="w1",
            tenant_id="t1",
            skill_id="s1",
            task_description="Test task",
        )
        assert manifest.status == TaskStatus.PENDING
        assert manifest.task_id

        manifest = manifest.mark_running(run_id="run-001")
        assert manifest.status == TaskStatus.RUNNING
        assert manifest.started_at
        assert manifest.run_id == "run-001"

        manifest = manifest.mark_completed(result_summary="All good")
        assert manifest.status == TaskStatus.COMPLETED
        assert manifest.completed_at
        assert manifest.result_summary == "All good"

    def test_task_manifest_status_flow_error(self) -> None:
        """TaskManifest transitions: pending -> running -> error."""
        manifest = create_task_manifest(
            worker_id="w1",
            tenant_id="t1",
        )
        manifest = manifest.mark_running()
        manifest = manifest.mark_error("Something failed")

        assert manifest.status == TaskStatus.ERROR
        assert manifest.error_message == "Something failed"
        assert manifest.completed_at

    def test_task_store_save_and_load(self, tmp_path: Path) -> None:
        """TaskStore persists and retrieves manifests."""
        store = TaskStore(workspace_root=tmp_path)

        manifest = create_task_manifest(
            worker_id="w1",
            tenant_id="t1",
            skill_id="s1",
            task_description="Persist test",
        )
        manifest = manifest.mark_running(run_id="r1")
        manifest = manifest.mark_completed(result_summary="Done")

        store.save(manifest)

        loaded = store.load("t1", "w1", manifest.task_id)
        assert loaded is not None
        assert loaded.task_id == manifest.task_id
        assert loaded.status == TaskStatus.COMPLETED
        assert loaded.result_summary == "Done"

    def test_task_store_list_by_worker(self, tmp_path: Path) -> None:
        """TaskStore.list_by_worker returns all active tasks."""
        store = TaskStore(workspace_root=tmp_path)

        for i in range(3):
            m = create_task_manifest(
                worker_id="w1",
                tenant_id="t1",
                skill_id=f"s{i}",
            )
            store.save(m)

        tasks = store.list_by_worker("t1", "w1")
        assert len(tasks) == 3

    @pytest.mark.asyncio
    async def test_task_runner_creates_manifest(
        self,
        tmp_path: Path,
    ) -> None:
        """TaskRunner creates and persists TaskManifest through execution."""
        mock_llm = MockLLMClient(response_text="Runner result.")
        mock_tools = MockToolExecutor()
        dispatcher = EngineDispatcher(
            llm_client=mock_llm,
            tool_executor=mock_tools,
        )
        store = TaskStore(workspace_root=tmp_path)
        runner = TaskRunner(
            engine_dispatcher=dispatcher,
            task_store=store,
        )

        from src.engine.state import WorkerContext
        from src.skills.models import Skill

        ctx = WorkerContext(worker_id="w1", tenant_id="t1")
        skill = Skill(skill_id="test-skill", name="Test Skill")

        events = []
        async for event in runner.execute(
            skill=skill,
            worker_context=ctx,
            task="Test task for runner",
        ):
            events.append(event)

        # Check task was persisted
        tasks = store.list_by_worker("t1", "w1")
        assert len(tasks) == 1
        assert tasks[0].status == TaskStatus.COMPLETED
        assert tasks[0].skill_id == "test-skill"


class TestWorkerContextIsolation:
    """Test that different workers get isolated contexts."""

    def test_different_workers_different_contexts(self) -> None:
        """Two workers produce different WorkerContext objects."""
        worker_a = Worker(
            identity=WorkerIdentity(name="Worker A", worker_id="a"),
            constraints=("Constraint A",),
        )
        worker_b = Worker(
            identity=WorkerIdentity(name="Worker B", worker_id="b"),
            constraints=("Constraint B",),
        )
        tenant = Tenant(
            tenant_id="t1",
            name="T1",
            trust_level=TrustLevel.STANDARD,
        )
        gate = WorkerTrustGate(trusted=True, learned_rules_enabled=True)

        ctx_a = build_worker_context(worker_a, tenant, gate, available_tools=())
        ctx_b = build_worker_context(worker_b, tenant, gate, available_tools=())

        assert ctx_a.worker_id == "a"
        assert ctx_b.worker_id == "b"
        assert "Worker A" in ctx_a.identity
        assert "Worker B" in ctx_b.identity
        assert "Constraint A" in ctx_a.constraints
        assert "Constraint B" in ctx_b.constraints
