"""
ReAct prompt templates - system prompt construction for autonomous mode.

Used by ReactEngine to build the initial system message.
These are the raw templates; PromptBuilder.build_autonomous() calls into
autonomous_prompt.py which uses these templates with WorkerContext data.
"""

REACT_SYSTEM_TEMPLATE = """\
{identity}

## Principles
{principles}

{directives_section}

{learned_rules_section}

## Constraints
{constraints}

## Skill Knowledge
{skill_instructions}

{historical_context_section}

## Available Tools
{tool_names}

{recommended_tools_section}

## Task Context
{task_context}

## General Rules
- Use camelCase for parameters, numeric types for numbers, YYYY-MM-DD for dates
- Do not expose internal system IDs to users
- When tools can solve the problem, always call tools first instead of asking the user
- Only ask the user when tools cannot solve it and critical business info is missing
"""


def build_react_system_prompt(
    identity: str = "",
    principles: str = "",
    constraints: str = "",
    directives: str = "",
    learned_rules: str = "",
    skill_instructions: str = "",
    historical_context: str = "",
    tool_names: str = "",
    recommended_tools: str = "",
    task_context: str = "",
) -> str:
    """
    Build a complete ReAct system prompt from components.

    All parameters are optional strings; empty sections are omitted.
    """
    directives_section = f"## Directives\n{directives}" if directives else ""
    learned_rules_section = f"## Learned Rules\n{learned_rules}" if learned_rules else ""
    historical_context_section = (
        f"## Historical Context\n{historical_context}" if historical_context else ""
    )
    recommended_tools_section = (
        f"## Recommended Tools\n{recommended_tools}" if recommended_tools else ""
    )

    prompt = REACT_SYSTEM_TEMPLATE.format(
        identity=identity or "You are a professional AI assistant.",
        principles=principles or "Be helpful, accurate, and thorough.",
        directives_section=directives_section,
        learned_rules_section=learned_rules_section,
        constraints=constraints or "Follow all applicable policies.",
        skill_instructions=skill_instructions or "Use your knowledge to assist.",
        historical_context_section=historical_context_section,
        tool_names=tool_names or "No tools available.",
        recommended_tools_section=recommended_tools_section,
        task_context=task_context or "",
    )

    # Clean up multiple blank lines
    lines = prompt.split("\n")
    cleaned: list[str] = []
    prev_blank = False
    for line in lines:
        is_blank = line.strip() == ""
        if is_blank and prev_blank:
            continue
        cleaned.append(line)
        prev_blank = is_blank

    return "\n".join(cleaned).strip()
