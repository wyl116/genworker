# edition: baseline
from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.bootstrap.context import BootstrapContext
from src.bootstrap.memory_init import MemoryInitializer
from src.common.runtime_status import ComponentStatus


@pytest.mark.asyncio
async def test_memory_initializer_marks_openviking_disabled_when_flag_off():
    context = BootstrapContext(
        settings=SimpleNamespace(
            openviking_enabled=False,
            openviking_endpoint="",
            openviking_timeout_seconds=5.0,
        )
    )
    initializer = MemoryInitializer()

    result = await initializer.initialize(context)

    assert result is True
    snapshot = context.snapshot_runtime_components()
    assert snapshot["openviking"].status == ComponentStatus.DISABLED
    assert context.get_state("openviking_client") is None


@pytest.mark.asyncio
async def test_memory_initializer_marks_openviking_failed_when_endpoint_missing():
    context = BootstrapContext(
        settings=SimpleNamespace(
            openviking_enabled=True,
            openviking_endpoint="",
            openviking_timeout_seconds=5.0,
        )
    )
    initializer = MemoryInitializer()

    result = await initializer.initialize(context)

    assert result is True
    snapshot = context.snapshot_runtime_components()
    assert snapshot["openviking"].status == ComponentStatus.FAILED
    assert snapshot["openviking"].last_error == "endpoint_empty"


@pytest.mark.asyncio
async def test_memory_initializer_marks_openviking_degraded_when_health_unhealthy():
    context = BootstrapContext(
        settings=SimpleNamespace(
            openviking_enabled=True,
            openviking_endpoint="http://openviking.local",
            openviking_timeout_seconds=5.0,
        )
    )
    initializer = MemoryInitializer()
    fake_client = SimpleNamespace(health_check=AsyncMock(return_value=False), close=AsyncMock())
    fake_module = ModuleType("src.memory.backends.openviking")
    fake_module.OpenVikingClient = lambda **_kwargs: fake_client

    original = sys.modules.get("src.memory.backends.openviking")
    sys.modules["src.memory.backends.openviking"] = fake_module
    try:
        result = await initializer.initialize(context)
    finally:
        if original is None:
            sys.modules.pop("src.memory.backends.openviking", None)
        else:
            sys.modules["src.memory.backends.openviking"] = original

    assert result is True
    snapshot = context.snapshot_runtime_components()
    assert snapshot["openviking"].status == ComponentStatus.DEGRADED


@pytest.mark.asyncio
async def test_memory_initializer_marks_openviking_failed_when_health_raises():
    context = BootstrapContext(
        settings=SimpleNamespace(
            openviking_enabled=True,
            openviking_endpoint="http://openviking.local",
            openviking_timeout_seconds=5.0,
        )
    )
    initializer = MemoryInitializer()
    fake_client = SimpleNamespace(
        health_check=AsyncMock(side_effect=RuntimeError("connection refused")),
        close=AsyncMock(),
    )
    fake_module = ModuleType("src.memory.backends.openviking")
    fake_module.OpenVikingClient = lambda **_kwargs: fake_client

    original = sys.modules.get("src.memory.backends.openviking")
    sys.modules["src.memory.backends.openviking"] = fake_module
    try:
        result = await initializer.initialize(context)
    finally:
        if original is None:
            sys.modules.pop("src.memory.backends.openviking", None)
        else:
            sys.modules["src.memory.backends.openviking"] = original

    assert result is True
    snapshot = context.snapshot_runtime_components()
    assert snapshot["openviking"].status == ComponentStatus.FAILED
    assert snapshot["openviking"].last_error == "connection refused"
