"""
MCP tool initialization bootstrap.

Initializes the MCP server, registers built-in tools,
and runs the MCP scanner for remote tool discovery.
"""
from src.common.logger import get_logger

from .base import Initializer
from .context import BootstrapContext

logger = get_logger()


class MCPInitializer(Initializer):
    """Bootstrap initializer for the MCP tool system."""

    @property
    def name(self) -> str:
        return "mcp"

    @property
    def priority(self) -> int:
        return 10

    @property
    def required(self) -> bool:
        return True

    async def initialize(self, context: BootstrapContext) -> bool:
        """
        Initialize MCP server and register tools.

        Steps:
        1. Create MCP server singleton
        2. Register built-in tools (time, bash)
        3. Run MCP scanner for remote tools
        """
        try:
            # 1. Create MCP server
            from src.tools.mcp.server import get_mcp_server
            mcp_server = get_mcp_server(create_if_missing=True)
            if mcp_server is None:
                context.record_error(self.name, "Failed to create MCP server")
                return False

            # 2. Register pure built-in tools via decorator registry
            from src.tools.builtin.scanner import scan_builtin_tools

            for spec in scan_builtin_tools():
                kwargs = {
                    name: context.get_state(name)
                    for name in spec.requires
                }
                created = spec.factory(**kwargs)
                tools = created if spec.multi else (created,)
                for tool in tools:
                    mcp_server.register_tool(tool)

            from src.tools.builtin.script_tool_registry import ScriptToolRegistry

            script_tool_registry = ScriptToolRegistry()
            mcp_server.register_refresh_hook(script_tool_registry.sync_to_server)
            script_tool_registry.sync_to_server(mcp_server)
            context.set_state("script_tool_registry", script_tool_registry)

            # Note: Task management tools (task_create/get/list/update) are
            # injected per-run in WorkerRouter.route_stream() with a fresh
            # TaskStore per execution, not registered globally here.

            # 3. Run MCP scanner for remote tools
            from src.tools.scanner import MCPScanner
            scanner = MCPScanner(mcp_server=mcp_server)
            try:
                await scanner.scan_and_register()
            except Exception as e:
                logger.warning(
                    f"[MCPInit] Scanner failed (non-fatal): {e}"
                )

            context.set_state("mcp_server", mcp_server)
            context.set_state("mcp_scanner", scanner)
            context.mcp_ready = True

            logger.info(
                f"[MCPInit] MCP initialized: {mcp_server.tool_count} tools"
            )
            return True

        except Exception as e:
            context.record_error(self.name, str(e))
            logger.error(f"[MCPInit] Failed: {e}", exc_info=True)
            return False

    async def cleanup(self) -> None:
        """Reset MCP server."""
        from src.tools.mcp.server import reset_mcp_server
        reset_mcp_server()
