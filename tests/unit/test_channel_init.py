# edition: baseline
from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.bootstrap.channel_init import ChannelInitializer


@pytest.mark.asyncio
async def test_channel_initializer_skips_runtime_when_im_disabled() -> None:
    state: dict[str, object] = {}

    class _Context:
        settings = SimpleNamespace(im_channel_enabled=False)

        def set_state(self, key: str, value: object) -> None:
            state[key] = value

    initializer = ChannelInitializer()

    result = await initializer.initialize(_Context())

    assert result is True
    assert state["channel_manager"] is None
    assert state["im_channel_registry"] is None
