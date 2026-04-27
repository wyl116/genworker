"""
MCP Server - Central registry for tool registration and lookup.
"""
from typing import Callable, Optional

from src.common.logger import get_logger

from .tool import Tool
from .types import MCPCategory

logger = get_logger()


class MCPServer:
    """
    Central MCP Server for tool management.

    Provides tool registration, lookup by name/category/tag,
    and OpenAI schema generation.
    """

    def __init__(self, name: str = "genworker-mcp", version: str = "1.0.0"):
        self._name = name
        self._version = version
        self._tools: dict[str, Tool] = {}
        self._tools_by_category: dict[MCPCategory, set[str]] = {
            cat: set() for cat in MCPCategory
        }
        self._tools_by_tag: dict[str, set[str]] = {}
        self._refresh_hooks: list[Callable[["MCPServer"], None]] = []
        self._refreshing = False

    @property
    def name(self) -> str:
        return self._name

    @property
    def version(self) -> str:
        return self._version

    @property
    def tool_count(self) -> int:
        return len(self._tools)

    def register_tool(self, tool: Tool) -> None:
        """Register a tool. Replaces existing tool with same name."""
        if tool.name in self._tools:
            logger.warning(f"Tool '{tool.name}' already registered, replacing")
            self._remove_from_indexes(tool.name)

        self._tools[tool.name] = tool
        self._tools_by_category[tool.category].add(tool.name)
        for tag in tool.tags:
            self._tools_by_tag.setdefault(tag, set()).add(tool.name)

        logger.debug(f"Registered tool: {tool.name} (category={tool.category})")

    def register_refresh_hook(self, hook: Callable[["MCPServer"], None]) -> None:
        """Register a callback that refreshes dynamic tool definitions on reads."""
        self._refresh_hooks.append(hook)

    def unregister_tool(self, name: str) -> bool:
        """Unregister a tool by name. Returns True if found."""
        if name not in self._tools:
            return False
        self._remove_from_indexes(name)
        del self._tools[name]
        logger.debug(f"Unregistered tool: {name}")
        return True

    def _remove_from_indexes(self, name: str) -> None:
        """Remove a tool from category and tag indexes."""
        tool = self._tools.get(name)
        if tool is None:
            return
        self._tools_by_category[tool.category].discard(name)
        for tag in tool.tags:
            tag_set = self._tools_by_tag.get(tag)
            if tag_set:
                tag_set.discard(name)

    def get_tool(self, name: str) -> Optional[Tool]:
        """Get a tool by name."""
        self._run_refresh_hooks()
        return self._tools.get(name)

    def get_all_tools(self) -> list[Tool]:
        """Get all registered tools."""
        self._run_refresh_hooks()
        return list(self._tools.values())

    def get_tools_by_names(self, names: list[str]) -> list[Tool]:
        """Get tools by a list of names. Missing names are skipped."""
        self._run_refresh_hooks()
        return [
            self._tools[n]
            for n in names
            if n in self._tools
        ]

    def list_tools(
        self,
        category: Optional[MCPCategory] = None,
        tags: Optional[frozenset[str]] = None,
    ) -> list[Tool]:
        """List tools with optional category/tag filtering."""
        self._run_refresh_hooks()
        names = set(self._tools.keys())

        if category is not None:
            names &= self._tools_by_category.get(category, set())

        if tags:
            for tag in tags:
                names &= self._tools_by_tag.get(tag, set())

        return [self._tools[n] for n in names]

    def get_openai_tools(
        self,
        category: Optional[MCPCategory] = None,
        tags: Optional[frozenset[str]] = None,
    ) -> list[dict]:
        """Get OpenAI function calling schemas for filtered tools."""
        return [t.to_openai_schema() for t in self.list_tools(category, tags)]

    def get_tool_summary(self) -> dict:
        """Summary of registered tools by category and tag."""
        self._run_refresh_hooks()
        return {
            "total": len(self._tools),
            "by_category": {
                cat.value: len(names)
                for cat, names in self._tools_by_category.items()
            },
            "by_tag": {
                tag: len(names)
                for tag, names in self._tools_by_tag.items()
            },
            "tools": list(self._tools.keys()),
        }

    def _run_refresh_hooks(self) -> None:
        if self._refreshing or not self._refresh_hooks:
            return
        self._refreshing = True
        try:
            for hook in self._refresh_hooks:
                hook(self)
        finally:
            self._refreshing = False


# --- Singleton ---

_mcp_server: Optional[MCPServer] = None


def get_mcp_server(
    create_if_missing: bool = True,
    name: str = "genworker-mcp",
    version: str = "1.0.0",
) -> Optional[MCPServer]:
    """Get the global MCP server singleton."""
    global _mcp_server
    if _mcp_server is None and create_if_missing:
        _mcp_server = MCPServer(name=name, version=version)
        logger.info(f"Created MCP server: {name} v{version}")
    return _mcp_server


def reset_mcp_server() -> None:
    """Reset the global MCP server (for testing)."""
    global _mcp_server
    _mcp_server = None
