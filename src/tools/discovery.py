"""
ToolDiscovery - Categorizes tools into core_tools and deferred_tools.

core_tools: Always injected into LLM's tools parameter.
deferred_tools: Discovered on-demand via the tool_search meta-tool.

The tool_search meta-tool is always included in core_tools,
enabling LLM to find deferred tools by keyword.
"""
from dataclasses import dataclass, field
from typing import Sequence

from src.common.logger import get_logger

from .mcp.tool import Tool
from .mcp.types import MCPCategory, RiskLevel, ToolType

logger = get_logger()
LLM_HIDDEN_TAG = "hidden_from_llm"

# Default core tool names (always injected)
DEFAULT_CORE_TOOL_NAMES: frozenset[str] = frozenset({
    "tool_search",
    "file_read",
    "file_write",
    "file_edit",
})


@dataclass(frozen=True)
class DiscoveryResult:
    """Immutable result of tool discovery categorization."""
    core_tools: tuple[Tool, ...]
    deferred_tools: tuple[Tool, ...]


class ToolDiscovery:
    """
    Categorizes available tools into core and deferred sets.

    core_tools: Recommended tools + high-frequency tools + tool_search.
    deferred_tools: Everything else (discoverable via tool_search).
    """

    def __init__(
        self,
        all_tools: Sequence[Tool],
        core_tool_names: frozenset[str] | None = None,
        recommended_tool_names: frozenset[str] | None = None,
    ):
        self._all_tools = tuple(
            tool for tool in all_tools
            if LLM_HIDDEN_TAG not in getattr(tool, "tags", frozenset())
        )
        self._core_names = (
            (core_tool_names or DEFAULT_CORE_TOOL_NAMES)
            | (recommended_tool_names or frozenset())
        )

        # Build the tool_search meta-tool
        self._tool_search = _build_tool_search_tool(self)

        # Categorize
        self._core_tools, self._deferred_tools = self._categorize()

    @property
    def core_tools(self) -> tuple[Tool, ...]:
        return self._core_tools

    @property
    def deferred_tools(self) -> tuple[Tool, ...]:
        return self._deferred_tools

    def search(self, keyword: str) -> tuple[Tool, ...]:
        """
        Search deferred tools by keyword.

        Matches against tool name, description, and tags.
        """
        if not keyword.strip():
            return ()
        return tuple(
            t for t in self._deferred_tools if t.matches_keyword(keyword)
        )

    def get_result(self) -> DiscoveryResult:
        """Get the full discovery result."""
        return DiscoveryResult(
            core_tools=self._core_tools,
            deferred_tools=self._deferred_tools,
        )

    def _categorize(self) -> tuple[tuple[Tool, ...], tuple[Tool, ...]]:
        """Split tools into core and deferred."""
        core: list[Tool] = []
        deferred: list[Tool] = []

        for tool in self._all_tools:
            if tool.name in self._core_names:
                core.append(tool)
            else:
                deferred.append(tool)

        # Always include tool_search in core
        if not any(t.name == "tool_search" for t in core):
            core.insert(0, self._tool_search)

        logger.info(
            f"[ToolDiscovery] Categorized {len(core)} core tools, "
            f"{len(deferred)} deferred tools"
        )
        return tuple(core), tuple(deferred)


def _build_tool_search_tool(discovery: "ToolDiscovery") -> Tool:
    """Build the tool_search meta-tool that searches deferred tools."""

    async def tool_search_handler(keyword: str) -> str:
        """Search for tools by keyword."""
        matches = discovery.search(keyword)
        if not matches:
            return f"No tools found matching '{keyword}'"

        lines = [f"Found {len(matches)} tool(s) matching '{keyword}':"]
        for t in matches:
            lines.append(f"  - {t.name}: {t.description}")
        return "\n".join(lines)

    return Tool(
        name="tool_search",
        description=(
            "Search for available tools by keyword. "
            "Use this to discover tools for specific tasks "
            "(e.g., 'database', 'file', 'time')."
        ),
        handler=tool_search_handler,
        parameters={
            "keyword": {
                "type": "string",
                "description": "Search keyword to match tool names, descriptions, or tags",
            }
        },
        required_params=("keyword",),
        tool_type=ToolType.SEARCH,
        category=MCPCategory.GLOBAL,
        risk_level=RiskLevel.LOW,
        tags=frozenset({"meta", "discovery", "search"}),
    )
