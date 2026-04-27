"""
MCP Scanner - Discovers and registers tools from local JSON config.

All Nacos references have been removed. Tool definitions are loaded
from a local JSON configuration file (configs/mcp_servers.json).
"""
import json
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

import httpx

from src.common.logger import get_logger
from src.common.exceptions import MCPException

from .mcp.server import MCPServer
from .mcp.tool import Tool
from .mcp.types import MCPCategory, RiskLevel, ToolType

logger = get_logger()

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = str(_PROJECT_ROOT / "configs" / "mcp_servers.json")


class MCPScanner:
    """
    Scans and registers MCP tools from local JSON configuration.

    Config format (configs/mcp_servers.json):
    {
      "servers": [
        {
          "name": "service-name",
          "url": "http://host:port",
          "tools_list_path": "tools/list",
          "enabled": true
        }
      ]
    }
    """

    def __init__(
        self,
        mcp_server: MCPServer,
        config_path: str = DEFAULT_CONFIG_PATH,
    ):
        self._mcp_server = mcp_server
        self._config_path = config_path
        self._scanned_tools: dict[str, dict[str, Any]] = {}
        self._http_client: Optional[httpx.AsyncClient] = None

    async def scan_and_register(self) -> int:
        """
        Load config and register all tools from configured servers.

        Returns:
            Number of tools registered.
        """
        config = self._load_config()
        if config is None:
            return 0

        servers = config.get("servers", [])
        if not servers:
            logger.info("[MCPScanner] No servers configured")
            return 0

        total_registered = 0
        for server_def in servers:
            if not server_def.get("enabled", True):
                continue
            count = await self._scan_server(server_def)
            total_registered += count

        logger.info(
            f"[MCPScanner] Scan complete: {total_registered} tools registered"
        )
        return total_registered

    def _load_config(self) -> Optional[dict[str, Any]]:
        """Load the MCP server config from local JSON file."""
        path = Path(self._config_path)
        if not path.exists():
            logger.warning(f"[MCPScanner] Config not found: {self._config_path}")
            return None

        try:
            content = path.read_text(encoding="utf-8")
            return json.loads(content)
        except (json.JSONDecodeError, OSError) as e:
            logger.error(f"[MCPScanner] Failed to load config: {e}")
            return None

    async def _scan_server(self, server_def: dict[str, Any]) -> int:
        """Scan a single server and register its tools."""
        server_name = server_def.get("name", "unknown")
        server_url = server_def.get("url", "")
        tools_path = server_def.get("tools_list_path", "tools/list")

        if not server_url:
            logger.warning(
                f"[MCPScanner] Server '{server_name}' missing url, skipping"
            )
            return 0

        try:
            tool_defs = await self._fetch_tools(server_url, tools_path)
        except Exception as e:
            logger.error(
                f"[MCPScanner] Failed to fetch tools from '{server_name}': {e}"
            )
            return 0

        registered = 0
        for tool_def in tool_defs:
            try:
                tool = _create_remote_tool(tool_def, server_url, server_name)
                self._mcp_server.register_tool(tool)
                self._scanned_tools[tool.name] = {
                    "server_name": server_name,
                    "server_url": server_url,
                }
                registered += 1
            except Exception as e:
                logger.error(
                    f"[MCPScanner] Failed to register tool "
                    f"'{tool_def.get('name', 'unknown')}': {e}"
                )

        logger.info(
            f"[MCPScanner] Server '{server_name}': {registered} tools registered"
        )
        return registered

    async def _fetch_tools(
        self, server_url: str, tools_path: str
    ) -> list[dict[str, Any]]:
        """Fetch tool list from a server's tools/list endpoint."""
        url = f"{server_url.rstrip('/')}/{tools_path.lstrip('/')}"
        client = await self._get_http_client()

        response = await client.get(url, timeout=10.0)
        if response.status_code != 200:
            raise MCPException(
                f"Tools endpoint returned status {response.status_code}: {url}"
            )

        data = response.json()
        return _extract_tools_from_response(data)

    async def _get_http_client(self) -> httpx.AsyncClient:
        """Get or create HTTP client."""
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(timeout=10.0)
        return self._http_client

    @property
    def scanned_tools(self) -> dict[str, dict[str, Any]]:
        return dict(self._scanned_tools)

    async def refresh(self) -> int:
        """Unregister all scanned tools and re-scan."""
        for tool_name in list(self._scanned_tools.keys()):
            self._mcp_server.unregister_tool(tool_name)
        self._scanned_tools.clear()
        return await self.scan_and_register()

    async def close(self) -> None:
        """Close HTTP client."""
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None


def _extract_tools_from_response(data: Any) -> list[dict[str, Any]]:
    """Extract tool list from various response formats."""
    if isinstance(data, list):
        return data

    if isinstance(data, dict):
        # MCP JSON-RPC 2.0: {"jsonrpc":"2.0","result":{"tools":[...]}}
        if "jsonrpc" in data and "result" in data:
            inner = data["result"]
            if isinstance(inner, dict) and "tools" in inner:
                return inner["tools"]
        # Simple: {"tools": [...]}
        if "tools" in data:
            return data["tools"]
        # Generic: {"data": [...]}
        if "data" in data and isinstance(data["data"], list):
            return data["data"]

    return []


def _create_remote_tool(
    tool_def: dict[str, Any],
    server_url: str,
    server_name: str,
) -> Tool:
    """Create a Tool from a remote tool definition."""
    name = tool_def.get("name")
    if not name:
        raise MCPException("Tool definition missing 'name' field")

    description = tool_def.get("description", "")
    input_schema = tool_def.get("inputSchema", {})
    properties = input_schema.get("properties", {})
    required = tuple(input_schema.get("required", []))
    execute_path = tool_def.get("path", "/execute")

    risk_str = tool_def.get("risk_level", "low")
    try:
        risk_level = RiskLevel(risk_str)
    except ValueError:
        risk_level = RiskLevel.LOW

    category_str = tool_def.get("category", "specialized").upper()
    try:
        category = MCPCategory(category_str)
    except ValueError:
        category = MCPCategory.SPECIALIZED

    tool_url = f"{server_url.rstrip('/')}/{execute_path.lstrip('/')}"

    async def remote_handler(**kwargs: Any) -> Any:
        """Execute remote tool via HTTP POST."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(tool_url, json=kwargs)
            if response.status_code != 200:
                raise MCPException(
                    f"Remote tool call failed: status={response.status_code}"
                )
            result = response.json()
            if isinstance(result, dict):
                return result.get("data") or result.get("result") or result
            return result

    return Tool(
        name=name,
        description=description,
        handler=remote_handler,
        parameters=properties,
        required_params=required,
        tool_type=ToolType.CUSTOM,
        category=category,
        risk_level=risk_level,
        tags=frozenset(tool_def.get("tags", [])),
    )
