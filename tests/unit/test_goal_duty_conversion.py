# edition: baseline
"""
Tests for goal-to-duty conversion and goal proposals.
"""
from __future__ import annotations

import pytest

from src.engine.protocols import LLMResponse, UsageInfo
from src.services.llm.intent import Purpose
from src.worker.goal.models import Goal, GoalProposal, GoalTask, Milestone
from src.worker.goal.planner import DutyFromGoal, goal_to_duty
from src.worker.goal.proposal import propose_goal


# ---------------------------------------------------------------------------
# Mock LLM
# ---------------------------------------------------------------------------

class MockLLMClient:
    """Simple mock LLM that returns configurable content."""

    def __init__(self, content: str = "Generated content") -> None:
        self._content = content
        self.last_intent = None

    async def invoke(
        self,
        messages,
        tools=None,
        tool_choice=None,
        system_blocks=None,
        intent=None,
    ) -> LLMResponse:
        self.last_intent = intent
        return LLMResponse(
            content=self._content,
            usage=UsageInfo(total_tokens=10),
        )


class ErrorLLMClient:
    """LLM that always raises."""

    async def invoke(self, messages, tools=None, tool_choice=None, system_blocks=None, intent=None) -> LLMResponse:
        raise RuntimeError("LLM unavailable")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _completed_goal(on_complete: str | None = "create_duty") -> Goal:
    return Goal(
        goal_id="goal-done",
        title="Improve monitoring",
        status="completed",
        priority="high",
        preferred_skill_ids=("analysis-skill", "report-skill"),
        milestones=(
            Milestone(
                id="ms-1",
                title="Setup dashboards",
                status="completed",
                completed_at="2026-03-01",
                tasks=(
                    GoalTask(id="t-1", title="Install Grafana", status="completed"),
                    GoalTask(id="t-2", title="Create panels", status="completed"),
                ),
            ),
        ),
        on_complete=on_complete,
    )


# ---------------------------------------------------------------------------
# Goal -> Duty conversion tests
# ---------------------------------------------------------------------------

class TestGoalToDuty:
    @pytest.mark.asyncio
    async def test_completed_goal_with_create_duty(self):
        goal = _completed_goal(on_complete="create_duty")
        llm = MockLLMClient("Check dashboards weekly")
        result = await goal_to_duty(goal, llm)
        assert result is not None
        assert isinstance(result, DutyFromGoal)
        assert result.duty_id.startswith("duty-goal-")
        assert result.duty_id.isascii()
        assert "Improve monitoring" in result.title
        assert result.action == "Check dashboards weekly"
        assert result.preferred_skill_ids == ("analysis-skill", "report-skill")
        assert llm.last_intent.purpose is Purpose.PLAN

    @pytest.mark.asyncio
    async def test_goal_to_duty_normalizes_non_ascii_goal_id(self):
        goal = Goal(
            goal_id="目标 2026/Q2",
            title="Improve monitoring",
            status="completed",
            priority="high",
            on_complete="create_duty",
        )
        result = await goal_to_duty(goal, MockLLMClient("Check dashboards weekly"))
        assert result is not None
        assert result.duty_id.startswith("duty-goal-")
        assert result.duty_id.isascii()

    @pytest.mark.asyncio
    async def test_no_create_duty_returns_none(self):
        goal = _completed_goal(on_complete=None)
        llm = MockLLMClient()
        result = await goal_to_duty(goal, llm)
        assert result is None

    @pytest.mark.asyncio
    async def test_active_goal_returns_none(self):
        goal = Goal(
            goal_id="g-active",
            title="Active goal",
            status="active",
            priority="medium",
            on_complete="create_duty",
        )
        llm = MockLLMClient()
        result = await goal_to_duty(goal, llm)
        assert result is None

    @pytest.mark.asyncio
    async def test_llm_error_uses_fallback(self):
        goal = _completed_goal()
        result = await goal_to_duty(goal, ErrorLLMClient())
        assert result is not None
        assert "Maintain outcomes" in result.action


# ---------------------------------------------------------------------------
# Goal proposal tests
# ---------------------------------------------------------------------------

class TestProposeGoal:
    @pytest.mark.asyncio
    async def test_proposal_created_with_pending_status(self):
        llm = MockLLMClient("Phase 1: Research\nPhase 2: Implement\nPhase 3: Test")
        proposal = await propose_goal(
            worker_id="w-1",
            title="Improve API performance",
            justification="Response times have increased 30%",
            llm_client=llm,
        )
        assert isinstance(proposal, GoalProposal)
        assert proposal.approval_status == "pending"
        assert proposal.proposed_by == "worker:w-1"
        assert proposal.proposed_goal.title == "Improve API performance"
        assert proposal.proposed_goal.status == "pending_approval"
        assert len(proposal.proposed_goal.milestones) >= 2

    @pytest.mark.asyncio
    async def test_proposal_with_llm_error_uses_fallback(self):
        proposal = await propose_goal(
            worker_id="w-1",
            title="New feature",
            justification="Customer request",
            llm_client=ErrorLLMClient(),
        )
        assert proposal.approval_status == "pending"
        assert len(proposal.proposed_goal.milestones) >= 2

    @pytest.mark.asyncio
    async def test_proposal_justification_preserved(self):
        llm = MockLLMClient("Step 1\nStep 2")
        proposal = await propose_goal(
            worker_id="w-2",
            title="Test",
            justification="Important reason",
            llm_client=llm,
        )
        assert proposal.justification == "Important reason"
