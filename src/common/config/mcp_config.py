# -*- coding: utf-8 -*-
"""
MCP Configuration Loader

Pure configuration loading for MCP servers, without business logic.
Supports loading from JSON files, environment variables, or dict.

Multi-Environment Support:
- mcp_servers.json          - Common/base configuration
- mcp_servers.{env}.json    - Environment-specific overrides (dev, test, prod)

Loading order (similar to config.env + config_{env}.env):
1. Load base config (mcp_servers.json)
2. Load environment config (mcp_servers.{env}.json) and merge/override

Example:
    # configs/mcp_servers.json (base)
    {
        "mcpServers": {
            "qm-user-center": {
                "type": "http",
                "url": "http://localhost:8182/api/mcp/http/userCenter"
            }
        }
    }

    # configs/mcp_servers.prod.json (production override)
    {
        "mcpServers": {
            "qm-user-center": {
                "type": "http",
                "url": "https://api.production.com/mcp/userCenter"
            }
        }
    }

Usage:
    loader = MCPConfigLoader(environment="prod")
    config = loader.load_for_environment()  # Loads base + prod overrides
"""

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator


_PROJECT_ROOT = Path(__file__).resolve().parents[3]


class MCPServerConfig(BaseModel):
    """
    Configuration for a single MCP server.

    Attributes:
        type: Transport type (http, stdio, sse)
        url: Server URL (for http/sse types)
        command: Command to execute (for stdio type)
        args: Command arguments (for stdio type)
        env: Environment variables (for stdio type)
        headers: HTTP headers (for http type)
        timeout: Request timeout in seconds
        enabled: Whether this server is enabled (default: True)
    """

    type: Literal["http", "stdio", "sse"] = Field(
        default="http",
        description="Transport type"
    )
    url: Optional[str] = Field(
        default=None,
        description="Server URL for http/sse transport"
    )
    command: Optional[str] = Field(
        default=None,
        description="Command for stdio transport"
    )
    args: Optional[List[str]] = Field(
        default=None,
        description="Command arguments for stdio transport"
    )
    env: Optional[Dict[str, str]] = Field(
        default=None,
        description="Environment variables for stdio transport"
    )
    headers: Optional[Dict[str, str]] = Field(
        default=None,
        description="HTTP headers for http transport"
    )
    timeout: int = Field(
        default=30,
        description="Request timeout in seconds"
    )
    enabled: bool = Field(
        default=True,
        description="Whether this server is enabled"
    )

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: Optional[str], info) -> Optional[str]:
        """Validate URL is provided for http/sse types."""
        if info.data.get("type") in ("http", "sse") and not v:
            raise ValueError("URL is required for http/sse transport type")
        return v

    @field_validator("command")
    @classmethod
    def validate_command(cls, v: Optional[str], info) -> Optional[str]:
        """Validate command is provided for stdio type."""
        if info.data.get("type") == "stdio" and not v:
            raise ValueError("Command is required for stdio transport type")
        return v


class MCPServersConfig(BaseModel):
    """
    Configuration for multiple MCP servers.

    Attributes:
        mcp_servers: Dictionary of server name to configuration
    """

    mcp_servers: Dict[str, MCPServerConfig] = Field(
        default_factory=dict,
        alias="mcpServers",
        description="MCP server configurations"
    )

    model_config = {
        "populate_by_name": True,
        "extra": "ignore"
    }

    def get_server(self, name: str) -> Optional[MCPServerConfig]:
        """Get configuration for a specific server."""
        return self.mcp_servers.get(name)

    def get_http_servers(self) -> Dict[str, MCPServerConfig]:
        """Get all enabled HTTP type servers."""
        return {
            name: config
            for name, config in self.mcp_servers.items()
            if config.type == "http" and config.enabled
        }

    def get_stdio_servers(self) -> Dict[str, MCPServerConfig]:
        """Get all enabled stdio type servers."""
        return {
            name: config
            for name, config in self.mcp_servers.items()
            if config.type == "stdio" and config.enabled
        }

    def get_enabled_servers(self) -> Dict[str, MCPServerConfig]:
        """Get all enabled servers."""
        return {
            name: config
            for name, config in self.mcp_servers.items()
            if config.enabled
        }

    def server_names(self) -> List[str]:
        """Get list of all server names."""
        return list(self.mcp_servers.keys())

    def merge(self, other: "MCPServersConfig") -> "MCPServersConfig":
        """
        Merge another config into this one.

        The other config's servers override same-named servers in this config.

        Args:
            other: Configuration to merge

        Returns:
            New merged MCPServersConfig
        """
        merged_servers = dict(self.mcp_servers)
        merged_servers.update(other.mcp_servers)
        return MCPServersConfig(mcp_servers=merged_servers)


class MCPConfigLoader:
    """
    MCP Configuration Loader with multi-environment support.

    Responsible for loading MCP server configurations from various sources.
    Supports environment-specific configuration overlays.

    Configuration file naming:
    - mcp_servers.json          - Base/common configuration
    - mcp_servers.dev.json      - Development environment
    - mcp_servers.test.json     - Testing environment
    - mcp_servers.prod.json     - Production environment

    Usage:
        # Auto-detect environment from ENVIRONMENT env var
        loader = MCPConfigLoader()
        config = loader.load_for_environment()

        # Explicit environment
        loader = MCPConfigLoader(environment="prod")
        config = loader.load_for_environment()

        # Load specific file only
        config = loader.load_from_file("configs/mcp_servers.json")
    """

    DEFAULT_CONFIG_DIR = "configs"
    BASE_CONFIG_NAME = "mcp_servers.json"

    # Environment aliases
    ENV_ALIASES = {
        "development": "dev",
        "testing": "test",
        "production": "prod",
        "staging": "stage",
    }

    def __init__(
        self,
        base_path: Optional[str] = None,
        environment: Optional[str] = None,
        config_dir: Optional[str] = None,
    ):
        """
        Initialize the config loader.

        Args:
            base_path: Base path for relative config file paths
            environment: Environment name (dev, test, prod). Auto-detected if not provided.
            config_dir: Configuration directory (default: "configs")
        """
        self._base_path = Path(base_path) if base_path else _PROJECT_ROOT
        self._config_dir = config_dir or self.DEFAULT_CONFIG_DIR
        self._environment = self._resolve_environment(environment)

    def _resolve_environment(self, environment: Optional[str]) -> str:
        """
        Resolve the environment name.

        Priority:
        1. Explicitly provided environment
        2. ENVIRONMENT env var
        3. Default to "dev"
        """
        if environment:
            env = environment.lower()
        else:
            env = os.environ.get("ENVIRONMENT", "development").lower()

        # Apply aliases
        return self.ENV_ALIASES.get(env, env)

    @property
    def environment(self) -> str:
        """Get the current environment."""
        return self._environment

    def _get_config_path(self, filename: str) -> Path:
        """Get the full path to a config file."""
        return self._base_path / self._config_dir / filename

    def _get_env_config_name(self, env: str) -> str:
        """Get the environment-specific config filename."""
        return f"mcp_servers.{env}.json"

    def load_from_file(self, path: str) -> MCPServersConfig:
        """
        Load configuration from a JSON file.

        Args:
            path: Path to the configuration file (absolute or relative)

        Returns:
            MCPServersConfig instance

        Raises:
            FileNotFoundError: If the file doesn't exist
            json.JSONDecodeError: If the file is not valid JSON
            ValidationError: If the configuration is invalid
        """
        file_path = Path(path)
        if not file_path.is_absolute():
            file_path = self._base_path / file_path

        if not file_path.exists():
            raise FileNotFoundError(f"Configuration file not found: {file_path}")

        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        return self.load_from_dict(data)

    def load_from_dict(self, data: Dict[str, Any]) -> MCPServersConfig:
        """
        Load configuration from a dictionary.

        Args:
            data: Configuration dictionary

        Returns:
            MCPServersConfig instance
        """
        return MCPServersConfig.model_validate(data)

    def load_from_env(self, prefix: str = "MCP_SERVER_") -> MCPServersConfig:
        """
        Load configuration from environment variables.

        Environment variable format:
        - MCP_SERVER_<NAME>_URL: Server URL
        - MCP_SERVER_<NAME>_TYPE: Transport type (default: http)
        - MCP_SERVER_<NAME>_TIMEOUT: Request timeout

        Args:
            prefix: Environment variable prefix

        Returns:
            MCPServersConfig instance
        """
        servers: Dict[str, Dict[str, Any]] = {}

        for key, value in os.environ.items():
            if not key.startswith(prefix):
                continue

            # Parse: MCP_SERVER_<NAME>_<FIELD>
            parts = key[len(prefix):].split("_", 1)
            if len(parts) != 2:
                continue

            server_name = parts[0].lower().replace("_", "-")
            field_name = parts[1].lower()

            if server_name not in servers:
                servers[server_name] = {"type": "http"}

            # Map environment variable fields to config fields
            field_map = {
                "url": "url",
                "type": "type",
                "timeout": "timeout",
                "command": "command",
                "enabled": "enabled",
            }

            if field_name in field_map:
                config_field = field_map[field_name]
                if field_name == "timeout":
                    servers[server_name][config_field] = int(value)
                elif field_name == "enabled":
                    servers[server_name][config_field] = value.lower() in ("true", "1", "yes")
                else:
                    servers[server_name][config_field] = value

        return MCPServersConfig(mcp_servers={
            name: MCPServerConfig(**config)
            for name, config in servers.items()
        })

    def load_for_environment(
        self,
        environment: Optional[str] = None,
    ) -> MCPServersConfig:
        """
        Load configuration for a specific environment.

        Loading order:
        1. Load base config (mcp_servers.json)
        2. Load environment config (mcp_servers.{env}.json) and merge
        3. Load from environment variables and merge

        Args:
            environment: Environment name (uses instance default if not provided)

        Returns:
            Merged MCPServersConfig instance
        """
        env = environment or self._environment
        env = self.ENV_ALIASES.get(env.lower(), env.lower())

        config = MCPServersConfig()

        # 1. Load base config
        base_path = self._get_config_path(self.BASE_CONFIG_NAME)
        if base_path.exists():
            try:
                base_config = self.load_from_file(str(base_path))
                config = config.merge(base_config)
            except Exception as e:
                # Log but continue - base config is optional
                pass

        # 2. Load environment-specific config
        env_config_name = self._get_env_config_name(env)
        env_path = self._get_config_path(env_config_name)
        if env_path.exists():
            try:
                env_config = self.load_from_file(str(env_path))
                config = config.merge(env_config)
            except Exception as e:
                # Log but continue - env config is optional
                pass

        # 3. Load from environment variables (highest priority)
        env_var_config = self.load_from_env()
        if env_var_config.mcp_servers:
            config = config.merge(env_var_config)

        return config

    def load_auto(self) -> MCPServersConfig:
        """
        Auto-load configuration using environment detection.

        Same as load_for_environment() but uses auto-detected environment.

        Returns:
            MCPServersConfig instance
        """
        return self.load_for_environment()

    def to_claude_sdk_format(self, config: MCPServersConfig) -> Dict[str, Any]:
        """
        Convert configuration to Claude SDK mcp_servers format.

        Only includes enabled servers.

        Args:
            config: MCPServersConfig instance

        Returns:
            Dictionary in Claude SDK format
        """
        result = {}
        for name, server_config in config.mcp_servers.items():
            if not server_config.enabled:
                continue

            if server_config.type == "http":
                result[name] = {
                    "type": "http",
                    "url": server_config.url,
                }
                if server_config.headers:
                    result[name]["headers"] = server_config.headers
            elif server_config.type == "stdio":
                result[name] = {
                    "type": "stdio",
                    "command": server_config.command,
                    "args": server_config.args or [],
                }
                if server_config.env:
                    result[name]["env"] = server_config.env

        return result

    def list_available_configs(self) -> Dict[str, bool]:
        """
        List available configuration files.

        Returns:
            Dictionary of config name to exists status
        """
        config_dir = self._base_path / self._config_dir
        envs = ["", "dev", "test", "stage", "prod"]

        result = {}
        for env in envs:
            if env:
                filename = self._get_env_config_name(env)
            else:
                filename = self.BASE_CONFIG_NAME

            path = config_dir / filename
            result[filename] = path.exists()

        return result


# Global singleton with environment support
_mcp_config_loader: Optional[MCPConfigLoader] = None


def get_mcp_config_loader(environment: Optional[str] = None) -> MCPConfigLoader:
    """
    Get the global MCP config loader instance.

    Args:
        environment: Optional environment override

    Returns:
        MCPConfigLoader instance
    """
    global _mcp_config_loader
    if _mcp_config_loader is None or environment:
        _mcp_config_loader = MCPConfigLoader(environment=environment)
    return _mcp_config_loader


def load_mcp_config(environment: Optional[str] = None) -> MCPServersConfig:
    """
    Convenience function to load MCP config for current environment.

    Args:
        environment: Optional environment override

    Returns:
        MCPServersConfig instance
    """
    loader = get_mcp_config_loader(environment)
    return loader.load_for_environment()
