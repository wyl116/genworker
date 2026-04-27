# edition: baseline
"""
Tests for TaskRunner Phase 7 post-processing hooks.
"""
from __future__ import annotations

import pytest

from src.engine.protocols import LLMResponse, ToolCall, ToolResult, UsageInfo
from src.engine.router.engine_dispatcher import EngineDispatcher
from src.engine.state import UsageBudget, WorkerContext
from src.skills.models import Skill, SkillStrategy, StrategyMode
from src.streaming.events import (
    ErrorEvent,
    RunFinishedEvent,
    RunStartedEvent,
    TextMessageEvent,
    ToolCallEvent,
)
from src.worker.task import TaskStore
from src.worker.task_runner import (
    PostRunExtraction,
    TaskRunner,
    extract_post_run_data,
)


# ---------------------------------------------------------------------------
# Mocks
# ---------------------------------------------------------------------------

class MockLLMClient:
    def __init__(self, responses=None):
        self._responses = list(responses or [])
        self._idx = 0

    async def invoke(self, messages, tools=None, tool_choice=None, system_blocks=None, intent=None):
        if self._idx < len(self._responses):
            r = self._responses[self._idx]
            self._idx += 1
            return r
        return LLMResponse(content="default")


class MockToolExecutor:
    async def execute(self, tool_name, tool_input):
        return ToolResult(content=f"result of {tool_name}")


class MockTaskStore:
    def __init__(self):
        self.saved = []

    def save(self, manifest):
        self.saved.append(manifest)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_skill() -> Skill:
    return Skill(
        skill_id="test-skill",
        name="Test Skill",
        version="1.0",
        scope="system",
        strategy=SkillStrategy(mode=StrategyMode.AUTONOMOUS),
        keywords=(),
    )


def _make_context() -> WorkerContext:
    return WorkerContext(
        worker_id="w-1",
        tenant_id="t-1",
    )


async def _collect_events(runner, **kwargs):
    events = []
    async for event in runner.execute(**kwargs):
        events.append(event)
    return events


# ---------------------------------------------------------------------------
# Tests: extract_post_run_data
# ---------------------------------------------------------------------------

class TestExtractPostRunData:
    def test_basic_extraction(self):
        result = extract_post_run_data(
            task="Check monitoring",
            collected_text=["Found 3 anomalies", "Result: all fixed"],
            tool_names_used=("bash", "search"),
        )
        assert isinstance(result, PostRunExtraction)
        assert "Found 3 anomalies" in result.episode_summary
        assert result.tool_names_used == ("bash", "search")

    def test_empty_text_uses_task(self):
        result = extract_post_run_data(
            task="My task description",
            collected_text=[],
            tool_names_used=(),
        )
        assert "My task description" in result.episode_summary

    def test_findings_extraction(self):
        result = extract_post_run_data(
            task="check",
            collected_text=[
                "Found a critical issue",
                "Regular line",
                "Result: positive outcome",
            ],
            tool_names_used=(),
        )
        finding_texts = " ".join(result.key_findings).lower()
        assert "found" in finding_texts or "result" in finding_texts

    def test_rule_candidates_extraction(self):
        result = extract_post_run_data(
            task="review",
            collected_text=[
                "We should always validate inputs",
                "Normal text here",
                "Best practice: use pagination",
            ],
            tool_names_used=(),
        )
        assert len(result.rule_candidates) >= 1


# ---------------------------------------------------------------------------
# Tests: TaskRunner post-processing
# ---------------------------------------------------------------------------

class TestTaskRunnerPostHooks:
    @pytest.mark.asyncio
    async def test_extraction_populated_on_success(self):
        llm = MockLLMClient([
            LLMResponse(
                content="",
                tool_calls=(ToolCall(tool_name="search", tool_input={}, tool_call_id="tc1"),),
                usage=UsageInfo(total_tokens=10),
            ),
            LLMResponse(
                content="Found important results",
                usage=UsageInfo(total_tokens=10),
            ),
        ])
        dispatcher = EngineDispatcher(
            llm_client=llm,
            tool_executor=MockToolExecutor(),
        )
        store = MockTaskStore()
        runner = TaskRunner(engine_dispatcher=dispatcher, task_store=store)

        events = await _collect_events(
            runner,
            skill=_make_skill(),
            worker_context=_make_context(),
            task="Search for data",
            applied_rule_ids=("rule-1", "rule-2"),
        )

        # Should have extraction
        extraction = runner.last_extraction
        assert extraction is not None
        assert "search" in extraction.tool_names_used
        assert extraction.applied_rule_ids == ("rule-1", "rule-2")

    @pytest.mark.asyncio
    async def test_no_extraction_on_error(self):
        """On error, last_extraction should not be set."""

        class FailLLM:
            async def invoke(self, messages, tools=None, tool_choice=None, system_blocks=None, intent=None):
                raise RuntimeError("fail")

        dispatcher = EngineDispatcher(
            llm_client=FailLLM(),
            tool_executor=MockToolExecutor(),
        )
        store = MockTaskStore()
        runner = TaskRunner(engine_dispatcher=dispatcher, task_store=store)

        events = await _collect_events(
            runner,
            skill=_make_skill(),
            worker_context=_make_context(),
            task="Will fail",
        )

        assert runner.last_extraction is None

    @pytest.mark.asyncio
    async def test_error_feedback_handler_called(self):
        calls = []

        class ErrorDispatcher:
            async def dispatch(self, **kwargs):
                yield ErrorEvent(run_id="r1", message="boom")
                yield RunFinishedEvent(run_id="r1", success=False, stop_reason="boom")

        async def on_error(manifest, worker_context, applied_rule_ids):
            calls.append((manifest.task_id, worker_context.worker_id, applied_rule_ids))

        runner = TaskRunner(
            engine_dispatcher=ErrorDispatcher(),
            task_store=MockTaskStore(),
            error_feedback_handler=on_error,
        )

        await _collect_events(
            runner,
            skill=_make_skill(),
            worker_context=_make_context(),
            task="Will fail",
            applied_rule_ids=("rule-x",),
        )
        assert calls
        assert calls[0][1] == "w-1"
        assert calls[0][2] == ("rule-x",)
