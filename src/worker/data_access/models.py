"""
Data access layer data models - all frozen dataclasses for immutability.

Defines configuration and record types for workspace access, temp links,
query scoping, operation policies, and external mounts.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class DataSpaceConfig:
    """Workspace sandbox configuration."""
    root: str = "data/"
    shared_refs: bool = True


@dataclass(frozen=True)
class ExternalAccessConfig:
    """Temporary external link access configuration."""
    mode: str = "strict"  # "strict" | "permissive"
    allowed_domains: tuple[str, ...] = ()
    auto_expire: str = "24h"


@dataclass(frozen=True)
class QueryDimension:
    """A single dimension for SQL auto-injection."""
    column: str
    value: str


@dataclass(frozen=True)
class QueryPolicy:
    """Policy for a specific query tool."""
    auto_inject: tuple[QueryDimension, ...]
    forbidden_tables: tuple[str, ...] = ()
    forbidden_operations: tuple[str, ...] = ()


@dataclass(frozen=True)
class DataScopeConfig:
    """Scoped data access dimensions and per-tool query policies."""
    dimensions: tuple[tuple[str, str], ...] = ()
    query_policies: tuple[tuple[str, QueryPolicy], ...] = ()


@dataclass(frozen=True)
class OperationLevel:
    """Policy for a single operation level."""
    policy: str  # "auto" | "confirm" | "approval"
    audit: bool = False
    daily_limit: int | None = None
    approval_target: str | None = None


@dataclass(frozen=True)
class OperationPolicyConfig:
    """Operation policy configuration with four levels and optional overrides."""
    level_0_read: OperationLevel = OperationLevel(policy="auto")
    level_1_create: OperationLevel = OperationLevel(policy="confirm")
    level_2_modify: OperationLevel = OperationLevel(policy="confirm", audit=True)
    level_3_delete: OperationLevel = OperationLevel(policy="approval", audit=True)
    tool_overrides: tuple[tuple[str, OperationLevel], ...] = ()


@dataclass(frozen=True)
class MountConfig:
    """External storage mount configuration."""
    mount_id: str
    type: str  # "feishu" | "wecom" | "dingtalk"
    source: tuple[tuple[str, str], ...] = ()
    mount_path: str = ""
    permissions: tuple[str, ...] = ("read",)
    sync_strategy: str = "on_demand"
    cache_ttl: int = 300


@dataclass(frozen=True)
class TempAccessRecord:
    """Record of a temporary external access grant."""
    access_id: str
    source_url: str
    granted_by: str
    granted_to: str
    expires_at: str
    permissions: tuple[str, ...] = ("read",)
    local_path: str | None = None


@dataclass(frozen=True)
class OperationAuditEntry:
    """Audit trail entry for an operation."""
    tool_name: str
    operation_level: int
    policy_applied: str
    worker_id: str
    tenant_id: str
    timestamp: str
    approved: bool
    approved_by: str | None = None
