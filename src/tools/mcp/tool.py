"""
MCP Tool - Frozen dataclass tool definition with risk_level.
"""
from dataclasses import dataclass, field
from typing import Any, Callable, FrozenSet, Mapping, Optional, Sequence

from .types import ConcurrencyLevel, MCPCategory, RiskLevel, ToolType


@dataclass(frozen=True)
class Tool:
    """
    Immutable MCP tool definition.

    Registered with MCPServer and used throughout the pipeline.
    """
    name: str
    description: str
    handler: Callable
    parameters: Mapping[str, Any] = field(default_factory=dict)
    required_params: Sequence[str] = field(default_factory=tuple)
    tool_type: ToolType = ToolType.CUSTOM
    category: MCPCategory = MCPCategory.GLOBAL
    risk_level: RiskLevel = RiskLevel.LOW
    concurrency: ConcurrencyLevel = ConcurrencyLevel.EXCLUSIVE
    resource_key_param: str = ""
    tags: FrozenSet[str] = field(default_factory=frozenset)
    enabled: bool = True

    def to_openai_schema(self) -> dict[str, Any]:
        """Convert to OpenAI function calling schema."""
        properties: dict[str, Any] = {}
        params = dict(self.parameters)

        if "properties" in params:
            properties = params["properties"]
        else:
            for param_name, param_def in params.items():
                if isinstance(param_def, dict):
                    properties[param_name] = param_def
                else:
                    properties[param_name] = {
                        "type": "string",
                        "description": str(param_def),
                    }

        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": list(self.required_params),
                },
            },
        }

    @property
    def is_concurrent_safe(self) -> bool:
        """Whether this tool can be executed concurrently with others.

        READ and SEARCH tools are safe for parallel execution.
        WRITE, EXECUTE, and CUSTOM tools must run serially.
        """
        return self.concurrency == ConcurrencyLevel.SAFE

    def matches_keyword(self, keyword: str) -> bool:
        """Check if tool matches a search keyword (name, description, or tags)."""
        kw_lower = keyword.lower()
        if kw_lower in self.name.lower():
            return True
        if kw_lower in self.description.lower():
            return True
        return any(kw_lower in tag.lower() for tag in self.tags)
