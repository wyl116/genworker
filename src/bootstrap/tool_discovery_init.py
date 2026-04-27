"""
ToolDiscovery initialization bootstrap.

Depends on mcp_init. Creates ToolDiscovery instance that
categorizes tools into core_tools and deferred_tools,
and registers the tool_search meta-tool.
"""
from typing import List

from src.common.logger import get_logger

from .base import Initializer
from .context import BootstrapContext

logger = get_logger()


class ToolDiscoveryInitializer(Initializer):
    """Bootstrap initializer for ToolDiscovery."""

    @property
    def name(self) -> str:
        return "tool_discovery"

    @property
    def depends_on(self) -> List[str]:
        return ["mcp"]

    @property
    def priority(self) -> int:
        return 20

    async def initialize(self, context: BootstrapContext) -> bool:
        """
        Initialize ToolDiscovery with tools from MCP server.

        Reads the MCP server from context (set by mcp_init),
        categorizes all registered tools, and stores the
        ToolDiscovery instance in context.
        """
        try:
            from src.tools.mcp.server import MCPServer
            from src.tools.discovery import ToolDiscovery

            mcp_server: MCPServer = context.get_state("mcp_server")
            if mcp_server is None:
                context.record_error(
                    self.name, "MCP server not found in context"
                )
                return False

            all_tools = mcp_server.get_all_tools()

            # Create discovery with default core tool names
            discovery = ToolDiscovery(all_tools=all_tools)

            # Register tool_search meta-tool back to MCP server
            for tool in discovery.core_tools:
                if tool.name == "tool_search":
                    if mcp_server.get_tool("tool_search") is None:
                        mcp_server.register_tool(tool)
                    break

            context.set_state("tool_discovery", discovery)
            context.tools_ready = True

            logger.info(
                f"[ToolDiscoveryInit] Initialized: "
                f"{len(discovery.core_tools)} core, "
                f"{len(discovery.deferred_tools)} deferred"
            )
            return True

        except Exception as e:
            context.record_error(self.name, str(e))
            logger.error(
                f"[ToolDiscoveryInit] Failed: {e}", exc_info=True
            )
            return False

    async def cleanup(self) -> None:
        """No resources to clean up."""
        pass
