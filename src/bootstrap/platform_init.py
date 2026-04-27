"""
Platform bootstrap initializer.
"""
from __future__ import annotations

from pathlib import Path

from src.common.logger import get_logger
from src.common.runtime_status import ComponentRuntimeStatus, ComponentStatus
from src.services.redis import get_redis_client
from src.services.worker_channel_credential_loader import (
    WorkerChannelCredentialLoader,
)
from src.services.worker_platform_client_factory import WorkerPlatformClientFactory
from src.tools.mcp.server import get_mcp_server
from src.tools.runtime_scope import ExecutionScopeProvider

from .base import Initializer

logger = get_logger()


class PlatformInitializer(Initializer):
    """Create platform clients lazily from Settings."""

    def __init__(self) -> None:
        self._redis_status = ComponentRuntimeStatus(
            component="redis",
            enabled=False,
            status=ComponentStatus.DISABLED,
            selected_backend="redis",
            primary_backend="redis",
        )
        self._mysql_status = ComponentRuntimeStatus(
            component="mysql",
            enabled=False,
            status=ComponentStatus.DISABLED,
            selected_backend="mysql",
            primary_backend="mysql",
        )

    @property
    def name(self) -> str:
        return "platforms"

    @property
    def priority(self) -> int:
        return 125

    @property
    def depends_on(self) -> list[str]:
        return []

    async def initialize(self, context) -> bool:
        settings = context.settings
        redis_client = None
        context.set_state("mysql_client", None)
        redis_enabled = bool(getattr(settings, "redis_enabled", False))
        mysql_enabled = bool(getattr(settings, "mysql_enabled", False))
        if not redis_enabled:
            self._redis_status = ComponentRuntimeStatus(
                component="redis",
                enabled=False,
                status=ComponentStatus.DISABLED,
                selected_backend="redis",
                primary_backend="redis",
            )
            logger.info("[PlatformInit] component=redis status=disabled backend=redis")
        else:
            try:
                redis_client = get_redis_client()
                self._redis_status = ComponentRuntimeStatus(
                    component="redis",
                    enabled=True,
                    status=ComponentStatus.READY,
                    selected_backend="redis",
                    primary_backend="redis",
                )
                logger.info("[PlatformInit] component=redis status=ready backend=redis")
            except Exception as exc:
                redis_client = None
                self._redis_status = ComponentRuntimeStatus(
                    component="redis",
                    enabled=True,
                    status=ComponentStatus.FAILED,
                    selected_backend="redis",
                    primary_backend="redis",
                    last_error=str(exc).splitlines()[0][:200],
                )
                logger.warning(
                    "[PlatformInit] component=redis status=failed backend=redis last_error=%s",
                    self._redis_status.last_error,
                )
        if not mysql_enabled:
            self._mysql_status = ComponentRuntimeStatus(
                component="mysql",
                enabled=False,
                status=ComponentStatus.DISABLED,
                selected_backend="mysql",
                primary_backend="mysql",
            )
            logger.info("[PlatformInit] component=mysql status=disabled backend=mysql")
        else:
            self._mysql_status = ComponentRuntimeStatus(
                component="mysql",
                enabled=True,
                status=ComponentStatus.FAILED,
                selected_backend="mysql",
                primary_backend="mysql",
                last_error="mysql_initializer_missing",
            )
            logger.warning(
                "[PlatformInit] component=mysql status=failed backend=mysql last_error=%s",
                self._mysql_status.last_error,
            )
        workspace_root = Path(context.get_state("workspace_root", "workspace"))
        credential_loader = WorkerChannelCredentialLoader(workspace_root)
        platform_client_factory = WorkerPlatformClientFactory(credential_loader)
        scope_provider = context.get_state("execution_scope_provider")
        if scope_provider is None:
            scope_provider = ExecutionScopeProvider()
            context.set_state("execution_scope_provider", scope_provider)
        context.set_state("redis_client", redis_client)
        context.set_state("worker_channel_credential_loader", credential_loader)
        context.set_state("platform_client_factory", platform_client_factory)
        mcp_server = get_mcp_server(create_if_missing=False)
        if mcp_server is not None:
            from src.tools.builtin.email_tools import (
                create_worker_scoped_email_download_attachment_tool,
                create_worker_scoped_email_search_tool,
                create_worker_scoped_email_send_tool,
            )

            mcp_server.register_tool(
                create_worker_scoped_email_search_tool(
                    platform_client_factory,
                    scope_provider,
                )
            )
            mcp_server.register_tool(
                create_worker_scoped_email_send_tool(
                    platform_client_factory,
                    scope_provider,
                )
            )
            mcp_server.register_tool(
                create_worker_scoped_email_download_attachment_tool(
                    platform_client_factory,
                    scope_provider,
                )
            )
        context.register_runtime_component(
            "redis",
            lambda: self._redis_status,
            required=self.required,
        )
        context.register_runtime_component(
            "mysql",
            lambda: self._mysql_status,
            required=self.required,
        )
        logger.info("[PlatformInit] Worker-scoped platform services initialized")
        return True

    async def cleanup(self) -> None:
        return None
