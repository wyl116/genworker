# edition: baseline
from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.bootstrap.context import BootstrapContext
from src.bootstrap.llm_init import LLMInitializer
from src.services.llm.config_source import MissingInjectedConfigError


@pytest.mark.asyncio
async def test_llm_initializer_propagates_missing_injected_config(monkeypatch):
    async def _raise_missing(*args, **kwargs):
        raise MissingInjectedConfigError("missing provider")

    monkeypatch.setattr(
        "src.services.llm.initialize_litellm_router",
        _raise_missing,
    )
    monkeypatch.setattr(
        "src.services.llm.warmup_llm_connection",
        lambda: [],
    )

    initializer = LLMInitializer()
    context = BootstrapContext(settings=SimpleNamespace(community_smoke_profile=False))

    with pytest.raises(MissingInjectedConfigError, match="missing provider"):
        await initializer.initialize(context)
