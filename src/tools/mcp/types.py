"""
MCP type definitions - enums and shared types for the tool system.
"""
from enum import Enum


class ToolType(str, Enum):
    """
    Tool operation type.

    READ: Read-only operations (query, fetch)
    WRITE: Write operations (create, update, delete)
    SEARCH: Search operations
    EXECUTE: Execute/run operations
    CUSTOM: Custom operations
    """
    READ = "read"
    WRITE = "write"
    SEARCH = "search"
    EXECUTE = "execute"
    CUSTOM = "custom"


class MCPCategory(str, Enum):
    """
    Tool access category.

    GLOBAL: Available to all workers
    SPECIALIZED: Available to specific workers
    RESTRICTED: Requires explicit permission
    """
    GLOBAL = "GLOBAL"
    SPECIALIZED = "SPECIALIZED"
    RESTRICTED = "RESTRICTED"


class RiskLevel(str, Enum):
    """
    Tool risk level for permission checks.

    LOW: Safe read-only operations
    MEDIUM: Write operations with limited scope
    HIGH: Destructive or privileged operations
    CRITICAL: Operations requiring explicit approval
    """
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ConcurrencyLevel(str, Enum):
    """Scheduling granularity for tool execution."""

    SAFE = "safe"
    PATH_SCOPED = "path_scoped"
    EXCLUSIVE = "exclusive"
