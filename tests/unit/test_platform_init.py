# edition: baseline
"""Tests for PlatformInitializer."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from src.bootstrap.context import BootstrapContext
from src.bootstrap.platform_init import PlatformInitializer
from src.common.runtime_status import ComponentStatus
from src.tools.mcp.server import get_mcp_server, reset_mcp_server


def test_platform_initializer_marks_redis_disabled_without_connecting():
    settings = SimpleNamespace(redis_enabled=False)
    context = BootstrapContext(settings=settings)
    initializer = PlatformInitializer()

    import asyncio
    with patch("src.bootstrap.platform_init.get_redis_client") as get_redis_client:
        result = asyncio.run(initializer.initialize(context))

    assert result is True
    get_redis_client.assert_not_called()
    assert context.get_state("platform_client_factory") is not None
    assert context.get_state("worker_channel_credential_loader") is not None
    assert context.snapshot_runtime_components()["redis"].status == ComponentStatus.DISABLED
    assert context.snapshot_runtime_components()["mysql"].status == ComponentStatus.DISABLED


def test_platform_initializer_registers_email_tools_when_email_configured():
    reset_mcp_server()
    mcp_server = get_mcp_server(create_if_missing=True)
    settings = SimpleNamespace(redis_enabled=False)
    context = BootstrapContext(settings=settings)
    initializer = PlatformInitializer()

    import asyncio
    with patch("src.bootstrap.platform_init.get_redis_client", return_value=None):
        result = asyncio.run(initializer.initialize(context))

    assert result is True
    assert context.get_state("platform_client_factory") is not None
    assert mcp_server.get_tool("email_search") is not None
    assert mcp_server.get_tool("email_send") is not None
    assert mcp_server.get_tool("email_download_attachment") is not None
    reset_mcp_server()


def test_platform_initializer_marks_redis_failed_when_client_init_raises():
    settings = SimpleNamespace(redis_enabled=True, mysql_enabled=True)
    context = BootstrapContext(settings=settings)
    initializer = PlatformInitializer()

    import asyncio
    with patch(
        "src.bootstrap.platform_init.get_redis_client",
        side_effect=RuntimeError("redis bootstrap failed"),
    ):
        result = asyncio.run(initializer.initialize(context))

    assert result is True
    snapshot = context.snapshot_runtime_components()
    assert snapshot["redis"].status == ComponentStatus.FAILED
    assert snapshot["redis"].last_error == "redis bootstrap failed"
    assert snapshot["mysql"].status == ComponentStatus.FAILED
    assert snapshot["mysql"].last_error == "mysql_initializer_missing"
