"""
Tenant data model and loader.

Tenant is the core multi-tenant isolation entity. Multiple subsystems
depend on Tenant fields (tool sandbox, TrustGate, MCP remote discovery).
"""
import json
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Mapping

from src.common.exceptions import ConfigException
from src.common.logger import get_logger

logger = get_logger()


class TrustLevel(IntEnum):
    """Tenant trust levels controlling subsystem access."""
    BASIC = 0       # Lowest: all high-risk subsystems disabled
    STANDARD = 1    # Standard: bash/learned rules/episodic write enabled
    ELEVATED = 2    # Elevated: remote MCP discovery enabled
    FULL = 3        # Full: everything enabled


@dataclass(frozen=True)
class TenantToolPolicy:
    """Tenant-level tool policy (security overlay)."""
    denied_tools: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class Tenant:
    """
    Immutable tenant configuration.

    Loaded from workspace/tenants/{tenant_id}/TENANT.json.
    """
    tenant_id: str
    name: str
    trust_level: TrustLevel = TrustLevel.BASIC
    tool_policy: TenantToolPolicy = TenantToolPolicy()
    mcp_remote_allowed: bool = False
    default_worker: str | None = None
    credentials: Mapping[str, str] = field(default_factory=dict)


_TENANT_FILENAME = "TENANT.json"


class TenantLoader:
    """
    Loads Tenant configurations from workspace filesystem.

    Caches loaded tenants by tenant_id for repeated access.
    """

    def __init__(self, workspace_root: Path) -> None:
        self._workspace_root = workspace_root
        self._cache: dict[str, Tenant] = {}

    def load(self, tenant_id: str) -> Tenant:
        """
        Load a Tenant by ID from workspace.

        Args:
            tenant_id: The tenant identifier.

        Returns:
            Frozen Tenant object.

        Raises:
            ConfigException: If tenant config cannot be loaded or parsed.
        """
        if not tenant_id:
            raise ConfigException("tenant_id must not be empty")

        cached = self._cache.get(tenant_id)
        if cached is not None:
            return cached

        tenant = _load_tenant_from_file(self._workspace_root, tenant_id)
        self._cache[tenant_id] = tenant
        logger.info(f"[TenantLoader] Loaded tenant '{tenant_id}'")
        return tenant

    def clear_cache(self) -> None:
        """Clear the tenant cache."""
        self._cache.clear()


def _load_tenant_from_file(workspace_root: Path, tenant_id: str) -> Tenant:
    """Read and parse TENANT.json for the given tenant_id."""
    tenant_dir = workspace_root / "tenants" / tenant_id
    config_path = tenant_dir / _TENANT_FILENAME

    if not config_path.is_file():
        raise ConfigException(
            f"Tenant config not found: {config_path}"
        )

    try:
        raw = config_path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigException(
            f"Cannot read tenant config {config_path}: {exc}"
        ) from exc

    if not isinstance(data, dict):
        raise ConfigException(
            f"Tenant config {config_path} is not a JSON object"
        )

    return _build_tenant(data, tenant_id)


def _build_tenant(data: dict, expected_tenant_id: str) -> Tenant:
    """Build a frozen Tenant from parsed JSON data."""
    tid = data.get("tenant_id", expected_tenant_id)
    if tid != expected_tenant_id:
        raise ConfigException(
            f"Tenant ID mismatch: expected '{expected_tenant_id}', "
            f"got '{tid}' in config"
        )

    trust_raw = data.get("trust_level", 0)
    try:
        trust_level = TrustLevel(int(trust_raw))
    except ValueError:
        raise ConfigException(
            f"Invalid trust_level '{trust_raw}' for tenant '{tid}'"
        )

    tool_policy_raw = data.get("tool_policy", {})
    denied = frozenset(tool_policy_raw.get("denied_tools", []))
    tool_policy = TenantToolPolicy(denied_tools=denied)

    credentials_raw = data.get("credentials", {})
    credentials = dict(credentials_raw) if credentials_raw else {}

    return Tenant(
        tenant_id=tid,
        name=str(data.get("name", tid)),
        trust_level=trust_level,
        tool_policy=tool_policy,
        mcp_remote_allowed=bool(data.get("mcp_remote_allowed", False)),
        default_worker=data.get("default_worker"),
        credentials=credentials,
    )
