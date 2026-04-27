"""
Autonomous prompt assembly - builds system prompt for autonomous mode.

Assembles prompt from WorkerContext fields and Skill instructions.
Used by PromptBuilder.build_autonomous().
"""
from __future__ import annotations

from src.engine.react.prompts import build_react_system_prompt
from src.engine.state import WorkerContext
from src.skills.models import Skill


def assemble_autonomous_prompt(
    worker_context: WorkerContext,
    skill: Skill,
    instruction_override: str | None = None,
) -> str:
    """
    Assemble a complete autonomous mode system prompt.

    Args:
        worker_context: Worker identity and context data.
        skill: Active skill with instructions.
        instruction_override: Optional instruction key override
            (for hybrid mode where each step has its own instruction_ref).

    Returns:
        Complete system prompt string.
    """
    # Determine which instructions to use
    if instruction_override:
        skill_instructions = skill.get_instruction(instruction_override)
    else:
        skill_instructions = skill.get_instruction("general")

    # Format recommended tools
    recommended = ""
    if skill.recommended_tools:
        recommended = ", ".join(skill.recommended_tools)

    # Format tool names from worker context
    tool_names = ", ".join(worker_context.tool_names) if worker_context.tool_names else ""

    return build_react_system_prompt(
        identity=worker_context.identity,
        principles=worker_context.principles,
        constraints=worker_context.constraints,
        directives=worker_context.directives,
        learned_rules=worker_context.learned_rules,
        skill_instructions=skill_instructions,
        historical_context=worker_context.historical_context,
        tool_names=tool_names,
        recommended_tools=recommended,
        task_context=worker_context.task_context,
    )
