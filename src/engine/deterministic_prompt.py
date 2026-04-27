"""
Deterministic prompt assembly - builds system prompt for each workflow step.

Simplified prompt compared to autonomous mode:
- Worker identity (simplified)
- Step instruction
- Step tools
- Step input (previous step output)
- Output format requirements
"""
from __future__ import annotations

from src.engine.state import WorkerContext
from src.skills.models import Skill, WorkflowStep


_DETERMINISTIC_STEP_TEMPLATE = """\
## Role
{identity}

## Constraints
{constraints}

## Step Instruction
{step_instruction}

## Available Tools for This Step
{step_tools}

## Input
{previous_input}

## Output Requirements
{output_format}
"""


def assemble_deterministic_step_prompt(
    worker_context: WorkerContext,
    skill: Skill,
    step: WorkflowStep,
    previous_input: str,
) -> str:
    """
    Assemble a deterministic step system prompt.

    Args:
        worker_context: Worker identity and context (simplified usage).
        skill: Active skill with instructions.
        step: Current workflow step definition.
        previous_input: Output from previous step (or user task for first step).

    Returns:
        Complete step system prompt string.
    """
    # Get step-specific instruction
    step_instruction = skill.get_instruction(step.instruction_ref) if step.instruction_ref else ""
    if not step_instruction:
        step_instruction = f"Execute the '{step.step}' step."

    # Format step tools
    step_tools = ", ".join(step.tools) if step.tools else "No specific tools required."

    # Simple output format hint
    output_format = "Provide a clear, structured response."

    identity = worker_context.identity or "You are a professional AI assistant."
    constraints = worker_context.constraints or "Follow all applicable policies."

    prompt = _DETERMINISTIC_STEP_TEMPLATE.format(
        identity=identity,
        constraints=constraints,
        step_instruction=step_instruction,
        step_tools=step_tools,
        previous_input=previous_input,
        output_format=output_format,
    )

    return prompt.strip()
