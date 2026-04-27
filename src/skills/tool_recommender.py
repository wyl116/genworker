"""
Tool recommender - generates advisory prompt hints for recommended tools.

This is purely advisory: the LLM is free to use any available tool,
but the recommended tools are highlighted to guide selection.
"""
from typing import Sequence

from .models import Skill


# Prompt template for tool recommendations
_TOOL_HINT_TEMPLATE = (
    "Recommended tools for this task: {tool_list}. "
    "These tools are especially useful for this skill, "
    "but you may use any available tool as needed."
)

_NO_TOOLS_HINT = ""


class ToolRecommender:
    """
    Generates tool recommendation hints for prompt injection.

    Usage:
        recommender = ToolRecommender()
        hint = recommender.build_hint(skill)
        full_prompt = recommender.inject(skill, base_prompt)
    """

    def __init__(
        self,
        template: str = _TOOL_HINT_TEMPLATE,
    ) -> None:
        self._template = template

    def build_hint(self, skill: Skill) -> str:
        """
        Build a tool recommendation hint string.

        Args:
            skill: The matched skill with recommended_tools.

        Returns:
            Advisory hint string, or empty string if no tools recommended.
        """
        if not skill.recommended_tools:
            return _NO_TOOLS_HINT

        tool_list = ", ".join(skill.recommended_tools)
        return self._template.format(tool_list=tool_list)

    def inject(self, skill: Skill, base_prompt: str) -> str:
        """
        Inject tool recommendation hint into a base prompt.

        Args:
            skill: The matched skill.
            base_prompt: The original prompt text.

        Returns:
            Prompt with tool hint prepended (if any tools recommended),
            or the original prompt unchanged.
        """
        hint = self.build_hint(skill)
        if not hint:
            return base_prompt
        return f"{hint}\n\n{base_prompt}"

    def get_recommended_tool_names(self, skill: Skill) -> tuple[str, ...]:
        """Get the recommended tool names from a skill."""
        return skill.recommended_tools
