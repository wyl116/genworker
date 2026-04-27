# edition: baseline
"""Tests for PromptBuilder - autonomous, deterministic, and hybrid prompt assembly."""
import pytest

from src.engine.prompt_builder import PromptBuilder
from src.engine.state import WorkerContext
from src.skills.models import (
    FallbackConfig,
    RetryConfig,
    Skill,
    SkillKeyword,
    SkillScope,
    SkillStrategy,
    StrategyMode,
    WorkflowStep,
    WorkflowStepType,
)


def _make_skill(
    mode=StrategyMode.AUTONOMOUS,
    instructions=None,
    recommended_tools=(),
    workflow=(),
):
    return Skill(
        skill_id="test-skill",
        name="Test Skill",
        strategy=SkillStrategy(mode=mode, workflow=workflow),
        instructions=instructions or {"general": "General instructions here."},
        keywords=(),
        recommended_tools=tuple(recommended_tools),
    )


def _make_context(**overrides):
    defaults = {
        "worker_id": "w1",
        "tenant_id": "t1",
        "identity": "You are a data analyst.",
        "principles": "Be accurate.",
        "constraints": "No PII exposure.",
        "tool_names": ("sql_executor", "data_profiler"),
    }
    defaults.update(overrides)
    return WorkerContext(**defaults)


class TestAutonomousPrompt:
    def test_includes_identity(self):
        ctx = _make_context()
        skill = _make_skill()
        prompt = PromptBuilder.build_autonomous(ctx, skill)
        assert "data analyst" in prompt

    def test_includes_full_instructions(self):
        skill = _make_skill(instructions={"general": "Analyze all data thoroughly."})
        prompt = PromptBuilder.build_autonomous(_make_context(), skill)
        assert "Analyze all data thoroughly" in prompt

    def test_includes_principles(self):
        ctx = _make_context(principles="Always verify before reporting.")
        prompt = PromptBuilder.build_autonomous(ctx, _make_skill())
        assert "Always verify before reporting" in prompt

    def test_includes_constraints(self):
        ctx = _make_context(constraints="No raw SQL output.")
        prompt = PromptBuilder.build_autonomous(ctx, _make_skill())
        assert "No raw SQL output" in prompt

    def test_includes_recommended_tools(self):
        skill = _make_skill(recommended_tools=("sql_executor", "chart_builder"))
        prompt = PromptBuilder.build_autonomous(_make_context(), skill)
        assert "sql_executor" in prompt
        assert "chart_builder" in prompt

    def test_instruction_override_for_hybrid_step(self):
        skill = _make_skill(
            instructions={
                "general": "General stuff.",
                "planning": "Plan carefully.",
            }
        )
        prompt = PromptBuilder.build_autonomous(
            _make_context(), skill, instruction_override="planning"
        )
        assert "Plan carefully" in prompt


class TestDeterministicPrompt:
    def _make_step(self, name="exec", tools=("sql_executor",), instruction_ref="execution"):
        return WorkflowStep(
            step=name,
            type=WorkflowStepType.DETERMINISTIC,
            instruction_ref=instruction_ref,
            tools=tuple(tools),
        )

    def test_includes_step_tools(self):
        skill = _make_skill(instructions={"execution": "Run the query."})
        step = self._make_step()
        prompt = PromptBuilder.build_deterministic_step(
            _make_context(), skill, step, "previous data"
        )
        assert "sql_executor" in prompt

    def test_includes_previous_input(self):
        skill = _make_skill(instructions={"execution": "Run query."})
        step = self._make_step()
        prompt = PromptBuilder.build_deterministic_step(
            _make_context(), skill, step, "sales data from Q1"
        )
        assert "sales data from Q1" in prompt

    def test_includes_step_instruction(self):
        skill = _make_skill(instructions={"execution": "Execute the SQL carefully."})
        step = self._make_step()
        prompt = PromptBuilder.build_deterministic_step(
            _make_context(), skill, step, "input"
        )
        assert "Execute the SQL carefully" in prompt

    def test_includes_identity(self):
        ctx = _make_context(identity="Senior data engineer.")
        skill = _make_skill()
        step = self._make_step(instruction_ref="")
        prompt = PromptBuilder.build_deterministic_step(ctx, skill, step, "input")
        assert "Senior data engineer" in prompt

    def test_fallback_instruction_when_ref_missing(self):
        skill = _make_skill(instructions={"general": "General."})
        step = self._make_step(instruction_ref="nonexistent")
        prompt = PromptBuilder.build_deterministic_step(
            _make_context(), skill, step, "input"
        )
        # Should still produce a valid prompt with fallback instruction
        assert "exec" in prompt or "Step" in prompt


class TestHybridPrompt:
    def test_autonomous_step_uses_autonomous_prompt(self):
        skill = _make_skill(
            instructions={"general": "General.", "planning": "Plan first."}
        )
        step = WorkflowStep(
            step="planning",
            type=WorkflowStepType.AUTONOMOUS,
            instruction_ref="planning",
        )
        prompt = PromptBuilder.build_hybrid_step(
            _make_context(), skill, step, "task input"
        )
        assert "Plan first" in prompt

    def test_deterministic_step_uses_deterministic_prompt(self):
        skill = _make_skill(
            instructions={"execution": "Execute query."}
        )
        step = WorkflowStep(
            step="execution",
            type=WorkflowStepType.DETERMINISTIC,
            instruction_ref="execution",
            tools=("sql_executor",),
        )
        prompt = PromptBuilder.build_hybrid_step(
            _make_context(), skill, step, "previous output"
        )
        assert "sql_executor" in prompt
        assert "previous output" in prompt
