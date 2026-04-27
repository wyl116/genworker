"""
Prompt assembly dispatch entry point.

Routes to autonomous_prompt or deterministic_prompt based on mode.
For hybrid mode, dispatches per-step based on step.type.
"""
from __future__ import annotations

from src.context.assembler import assemble_system_prompt, build_segments
from src.context.models import ContextWindowConfig
from src.engine.autonomous_prompt import assemble_autonomous_prompt
from src.engine.deterministic_prompt import assemble_deterministic_step_prompt
from src.engine.state import WorkerContext
from src.skills.models import Skill, WorkflowStep


class PromptBuilder:
    """
    Central prompt assembly dispatcher.

    Stateless - all methods are class methods that delegate to
    the appropriate prompt assembly module.
    """

    @staticmethod
    def build_autonomous(
        worker_context: WorkerContext,
        skill: Skill,
        instruction_override: str | None = None,
    ) -> str:
        """
        Build system prompt for autonomous mode.

        Args:
            worker_context: Worker identity and context.
            skill: Active skill.
            instruction_override: Optional instruction key override.

        Returns:
            Complete system prompt string.
        """
        return assemble_autonomous_prompt(
            worker_context=worker_context,
            skill=skill,
            instruction_override=instruction_override,
        )

    @staticmethod
    def build_deterministic_step(
        worker_context: WorkerContext,
        skill: Skill,
        step: WorkflowStep,
        previous_input: str,
    ) -> str:
        """
        Build system prompt for a deterministic workflow step.

        Args:
            worker_context: Worker identity and context.
            skill: Active skill.
            step: Current workflow step.
            previous_input: Output from previous step or user task.

        Returns:
            Complete step system prompt string.
        """
        return assemble_deterministic_step_prompt(
            worker_context=worker_context,
            skill=skill,
            step=step,
            previous_input=previous_input,
        )

    @staticmethod
    def build_hybrid_step(
        worker_context: WorkerContext,
        skill: Skill,
        step: WorkflowStep,
        previous_input: str,
    ) -> str:
        """
        Build system prompt for a hybrid workflow step.

        Dispatches to autonomous or deterministic based on step.type.

        Args:
            worker_context: Worker identity and context.
            skill: Active skill.
            step: Current workflow step.
            previous_input: Output from previous step or user task.

        Returns:
            Complete step system prompt string.
        """
        from src.skills.models import WorkflowStepType

        if step.type == WorkflowStepType.AUTONOMOUS:
            return assemble_autonomous_prompt(
                worker_context=worker_context,
                skill=skill,
                instruction_override=step.instruction_ref or None,
            )

        return assemble_deterministic_step_prompt(
            worker_context=worker_context,
            skill=skill,
            step=step,
            previous_input=previous_input,
        )

    @staticmethod
    def build_autonomous_managed(
        worker_context: WorkerContext,
        config: ContextWindowConfig,
        episodic_context: str = "",
        duty_context: str = "",
        goal_context: str = "",
    ) -> str:
        """
        Build system prompt using context window management.

        Uses build_segments() + assemble_system_prompt() instead of
        string concatenation. Applies segment-level token budgets.

        Args:
            worker_context: Worker identity and context data.
            config: Context window configuration.
            episodic_context: Phase 7 episodic memory context.
            duty_context: Phase 7 duty context.
            goal_context: Phase 7 goal context.

        Returns:
            Complete system prompt string with budget-managed segments.
        """
        from src.context.budget_allocator import allocate_budgets, trim_segment_to_budget

        segments = build_segments(
            identity=worker_context.identity,
            principles=worker_context.principles,
            constraints=worker_context.constraints,
            directives=worker_context.directives,
            contact_context=worker_context.contact_context,
            learned_rules=worker_context.learned_rules,
            episodic_context=episodic_context,
            duty_context=duty_context,
            goal_context=goal_context,
            task_context=worker_context.task_context,
            config=config,
        )
        segments = allocate_budgets(segments, config)
        segments = tuple(trim_segment_to_budget(s) for s in segments)
        return assemble_system_prompt(segments)
