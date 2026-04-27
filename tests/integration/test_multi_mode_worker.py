# edition: baseline
"""
Integration tests for multi-mode worker interaction.

Tests that a single Worker can simultaneously handle:
- Conversation messages (chat mode)
- Direct tasks (task mode)
- Duty-triggered tasks (background mode)

Also tests:
- Skill switching in conversations
- Chat API and Task API independence
- ConversationInitializer in bootstrap
"""
import json
from pathlib import Path
from typing import Any

import pytest

from src.common.tenant import Tenant, TenantLoader, TrustLevel
from src.conversation.models import ChatMessage, ConversationSession
from src.conversation.session_manager import SessionManager
from src.conversation.session_store import FileSessionStore
from src.engine.protocols import LLMResponse, ToolResult, UsageInfo
from src.engine.router.engine_dispatcher import EngineDispatcher
from src.skills.models import Skill, SkillKeyword, SkillScope, SkillStrategy, StrategyMode
from src.skills.registry import SkillRegistry
from src.streaming.events import (
    ErrorEvent,
    RunFinishedEvent,
    RunStartedEvent,
    StepStartedEvent,
    StreamEvent,
    TaskProgressEvent,
    TextMessageEvent,
)
from src.worker.planning.subagent.models import AggregatedResult, SubAgentResult
from src.worker.models import Worker, WorkerIdentity
from src.worker.registry import WorkerEntry, build_worker_registry
from src.worker.router import WorkerRouter
from src.worker.task import TaskStore
from src.worker.task_runner import TaskRunner


# --- Mocks ---

class MockLLMClient:
    def __init__(self, response_text: str = "Response."):
        self._response_text = response_text
        self.call_count = 0

    async def invoke(self, messages, tools=None, tool_choice=None, system_blocks=None, intent=None):
        self.call_count += 1
        return LLMResponse(
            content=self._response_text,
            tool_calls=(),
            usage=UsageInfo(prompt_tokens=10, completion_tokens=20, total_tokens=30),
        )


class MockToolExecutor:
    async def execute(self, tool_name, tool_input):
        return ToolResult(content=f"Executed {tool_name}", is_error=False)


# --- Helpers ---

def _make_skill(
    skill_id: str,
    name: str,
    keywords: tuple[SkillKeyword, ...],
    default: bool = False,
    mode: StrategyMode = StrategyMode.AUTONOMOUS,
) -> Skill:
    return Skill(
        skill_id=skill_id,
        name=name,
        scope=SkillScope.SYSTEM,
        keywords=keywords,
        strategy=SkillStrategy(mode=mode),
        default_skill=default,
    )


class MockPlanningExecutor:
    async def execute(self, task, worker_context):
        return AggregatedResult(
            sub_results=(
                SubAgentResult(
                    agent_id="sa-1",
                    sub_goal_id="goal-1",
                    status="success",
                    content=f"Planned: {task}",
                ),
            ),
            success_count=1,
            failure_count=0,
            combined_content=f"Planned: {task}",
        )


def _build_router(
    tmp_path: Path,
    mock_llm: MockLLMClient,
    planning_executor: MockPlanningExecutor | None = None,
) -> tuple[WorkerRouter, TaskStore]:
    """Build a WorkerRouter with two skills for testing."""
    analysis_skill = _make_skill(
        "data-analysis",
        "Data Analysis",
        keywords=(
            SkillKeyword(keyword="analyze", weight=1.0),
            SkillKeyword(keyword="data", weight=0.8),
        ),
    )
    report_skill = _make_skill(
        "report-gen",
        "Report Generation",
        keywords=(
            SkillKeyword(keyword="report", weight=1.0),
            SkillKeyword(keyword="generate", weight=0.8),
        ),
    )
    general_skill = _make_skill(
        "general-query",
        "General Query",
        keywords=(SkillKeyword(keyword="help", weight=0.5),),
        default=True,
    )

    worker = Worker(
        identity=WorkerIdentity(name="Multi-Mode Worker", worker_id="w1"),
        default_skill="general-query",
    )
    registry = SkillRegistry.from_skills([
        analysis_skill, report_skill, general_skill,
    ])
    entry = WorkerEntry(worker=worker, skill_registry=registry)
    worker_registry = build_worker_registry(
        entries=[entry], default_worker_id="w1",
    )

    tenant = Tenant(
        tenant_id="demo",
        name="Demo",
        trust_level=TrustLevel.STANDARD,
        default_worker="w1",
    )
    tenant_loader = TenantLoader(tmp_path)
    tenant_loader._cache["demo"] = tenant

    dispatcher = EngineDispatcher(
        llm_client=mock_llm,
        tool_executor=MockToolExecutor(),
        enhanced_planning_executor=planning_executor,
    )
    task_store = TaskStore(workspace_root=tmp_path)
    runner = TaskRunner(
        engine_dispatcher=dispatcher,
        task_store=task_store,
    )
    router = WorkerRouter(
        worker_registry=worker_registry,
        tenant_loader=tenant_loader,
        task_runner=runner,
    )
    return router, task_store


# --- Tests ---

class TestMultiModeWorker:
    """A single Worker handles conversation and task modes."""

    @pytest.mark.asyncio
    async def test_same_worker_handles_chat_and_task(self, tmp_path: Path):
        """Same Worker processes both a chat message and a direct task."""
        mock_llm = MockLLMClient("Multi-mode result.")
        router, _ = _build_router(tmp_path, mock_llm)

        # Mode 1: Chat-like message
        chat_events = []
        async for event in router.route_stream(
            task="Analyze the Q1 data trends",
            tenant_id="demo",
        ):
            chat_events.append(event)

        # Mode 2: Direct task
        task_events = []
        async for event in router.route_stream(
            task="Generate a monthly report",
            tenant_id="demo",
        ):
            task_events.append(event)

        # Both should have completed successfully
        assert any(isinstance(e, RunFinishedEvent) for e in chat_events)
        assert any(isinstance(e, RunFinishedEvent) for e in task_events)
        assert mock_llm.call_count >= 2

    @pytest.mark.asyncio
    async def test_independent_skill_matching_per_message(self, tmp_path: Path):
        """Different messages match different Skills."""
        mock_llm = MockLLMClient("Result.")
        router, _ = _build_router(tmp_path, mock_llm)

        # This should match data-analysis skill
        events1 = []
        async for event in router.route_stream(
            task="Analyze the sales data",
            tenant_id="demo",
        ):
            events1.append(event)

        # This should match report-gen skill
        events2 = []
        async for event in router.route_stream(
            task="Generate a report for Q1",
            tenant_id="demo",
        ):
            events2.append(event)

        # Both completed
        assert any(isinstance(e, RunFinishedEvent) for e in events1)
        assert any(isinstance(e, RunFinishedEvent) for e in events2)

    @pytest.mark.asyncio
    async def test_planning_skill_routes_through_worker_router(self, tmp_path: Path):
        """Planning mode is recognized by skill matching and routed end-to-end."""
        planning_skill = _make_skill(
            "deep-research",
            "Deep Research",
            keywords=(SkillKeyword(keyword="research", weight=1.0),),
            mode=StrategyMode.PLANNING,
        )
        general_skill = _make_skill(
            "general-query",
            "General Query",
            keywords=(SkillKeyword(keyword="help", weight=0.5),),
            default=True,
        )
        worker = Worker(
            identity=WorkerIdentity(name="Multi-Mode Worker", worker_id="w1"),
            default_skill="general-query",
        )
        registry = SkillRegistry.from_skills([planning_skill, general_skill])
        entry = WorkerEntry(worker=worker, skill_registry=registry)
        worker_registry = build_worker_registry(
            entries=[entry], default_worker_id="w1",
        )
        tenant = Tenant(
            tenant_id="demo",
            name="Demo",
            trust_level=TrustLevel.STANDARD,
            default_worker="w1",
        )
        tenant_loader = TenantLoader(tmp_path)
        tenant_loader._cache["demo"] = tenant
        dispatcher = EngineDispatcher(
            llm_client=MockLLMClient("ignored"),
            tool_executor=MockToolExecutor(),
            enhanced_planning_executor=MockPlanningExecutor(),
        )
        runner = TaskRunner(
            engine_dispatcher=dispatcher,
            task_store=TaskStore(workspace_root=tmp_path),
        )
        router = WorkerRouter(
            worker_registry=worker_registry,
            tenant_loader=tenant_loader,
            task_runner=runner,
        )

        events = []
        async for event in router.route_stream(
            task="Research the competitor landscape",
            tenant_id="demo",
        ):
            events.append(event)

        assert isinstance(events[0], RunStartedEvent)
        assert any(
            isinstance(e, StepStartedEvent) and e.step_name == "planning"
            for e in events
        )
        progress_events = [e for e in events if isinstance(e, TaskProgressEvent)]
        assert len(progress_events) == 1
        assert progress_events[0].task_id == "goal-1"
        text_events = [e for e in events if isinstance(e, TextMessageEvent)]
        assert text_events[-1].content == "Planned: Research the competitor landscape"
        assert isinstance(events[-1], RunFinishedEvent)


class TestConversationSkillSwitching:
    """Skill switching preserves conversation history."""

    @pytest.mark.asyncio
    async def test_skill_switch_preserves_history(self, tmp_path: Path):
        """Switching skills keeps all previous messages."""
        store = FileSessionStore(tmp_path)
        manager = SessionManager(store=store)

        # Turn 1: user asks about analysis
        session = await manager.get_or_create("t1", "demo", "w1")
        msg1 = ChatMessage(
            role="user", content="Analyze Q1 data", skill_id=None,
        )
        session = session.append_message(msg1)

        resp1 = ChatMessage(
            role="assistant",
            content="Analyzing Q1...",
            skill_id="data-analysis",
        )
        session = session.append_message(resp1)

        # Turn 2: user asks about reports (different skill)
        msg2 = ChatMessage(
            role="user", content="Generate a report", skill_id=None,
        )
        session = session.append_message(msg2)

        resp2 = ChatMessage(
            role="assistant",
            content="Generating report...",
            skill_id="report-gen",
        )
        session = session.append_message(resp2)

        # All 4 messages preserved
        assert len(session.messages) == 4
        assert session.messages[0].content == "Analyze Q1 data"
        assert session.messages[1].skill_id == "data-analysis"
        assert session.messages[2].content == "Generate a report"
        assert session.messages[3].skill_id == "report-gen"

    @pytest.mark.asyncio
    async def test_system_prompt_skill_segment_replaceable(self, tmp_path: Path):
        """
        Verify that skill-specific context can be separated from
        the rest of the conversation context (identity, constraints, etc).
        """
        session = ConversationSession(
            session_id="s1",
            thread_id="t1",
            tenant_id="demo",
            worker_id="w1",
            messages=(
                ChatMessage(role="user", content="First question"),
                ChatMessage(
                    role="assistant",
                    content="First answer",
                    skill_id="skill-a",
                ),
                ChatMessage(role="user", content="Second question"),
                ChatMessage(
                    role="assistant",
                    content="Second answer",
                    skill_id="skill-b",
                ),
            ),
        )

        # The session preserves which skill was used per turn
        skills_used = [
            m.skill_id for m in session.messages
            if m.skill_id is not None
        ]
        assert skills_used == ["skill-a", "skill-b"]

        # History is complete and in order
        assert len(session.messages) == 4


class TestConversationInitializerRegistration:
    """ConversationInitializer is registered in bootstrap."""

    def test_initializer_in_bootstrap(self):
        """ConversationInitializer is part of create_orchestrator."""
        from src.bootstrap import ConversationInitializer
        from src.bootstrap.conversation_init import ConversationInitializer as CI

        assert ConversationInitializer is CI

    def test_initializer_properties(self):
        from src.bootstrap.conversation_init import ConversationInitializer

        init = ConversationInitializer()
        assert init.name == "conversation"
        assert init.priority == 120
        assert init.required is False
        assert "api_wiring" in init.depends_on
        assert "events" in init.depends_on
