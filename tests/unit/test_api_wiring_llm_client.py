# edition: baseline
from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest

from src.runtime.api_wiring import build_llm_client
from src.runtime.bootstrap_builders import (
    build_direct_llm_client,
    build_unavailable_llm_client,
)
from src.services.llm.intent import LLMCallIntent, Purpose
from src.services.llm.model_tiers import ModelTier


class _Context:
    def __init__(self, router=None, settings=None):
        self._state = {"litellm_router": router}
        self.settings = settings

    def get_state(self, key, default=None):
        return self._state.get(key, default)


def test_build_llm_client_uses_default_tier_from_router_config():
    router = SimpleNamespace(
        config_manager=SimpleNamespace(get_default_tier=lambda: "reasoning"),
    )
    context = _Context(router=router, settings=SimpleNamespace())

    client = build_llm_client(context)

    assert client._router is router
    assert client._default_tier is ModelTier.REASONING


def test_build_llm_client_loads_default_tier_from_config_source_when_router_lacks_config(monkeypatch):
    monkeypatch.setattr(
        "src.services.llm.config_source.build_litellm_config_source",
        lambda settings: SimpleNamespace(get_default_tier=lambda: "standard"),
    )
    context = _Context(router=SimpleNamespace(), settings=SimpleNamespace())
    client = build_llm_client(context)

    assert client._router is context.get_state("litellm_router")
    assert client._default_tier is ModelTier.STANDARD


def test_build_llm_client_constructs_local_router_from_litellm_config(monkeypatch):
    constructed = {}

    class _LocalRouter:
        def __init__(self, config_manager, enable_fallback, enable_caching):
            constructed["config_manager"] = config_manager
            constructed["enable_fallback"] = enable_fallback
            constructed["enable_caching"] = enable_caching
            self.config_manager = config_manager

    monkeypatch.setattr(
        "src.services.llm.config_source.build_litellm_config_source",
        lambda settings: SimpleNamespace(get_default_tier=lambda: "strong"),
    )
    monkeypatch.setattr("src.services.llm.litellm_router.LiteLLMRouter", _LocalRouter)

    client = build_llm_client(_Context(router=None, settings=SimpleNamespace()))

    assert client._default_tier is ModelTier.STRONG
    assert constructed["enable_fallback"] is True
    assert constructed["enable_caching"] is False


def test_build_llm_client_uses_unavailable_client_only_when_local_router_construction_fails(monkeypatch):
    monkeypatch.setattr(
        "src.services.llm.config_source.build_litellm_config_source",
        lambda settings: SimpleNamespace(
            get_default_tier=lambda: "standard",
            get_tier_model=lambda tier: "tier-standard",
            get_llm_kwargs=lambda model_name: {
                "model": "openai/test-model",
                "api_base": "https://example.com/v1",
                "api_key": "test-key",
            },
        ),
    )

    class _BrokenRouter:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("router boom")

    monkeypatch.setattr("src.services.llm.litellm_router.LiteLLMRouter", _BrokenRouter)

    client = build_llm_client(_Context(router=None, settings=SimpleNamespace()))

    assert hasattr(client, "invoke")
    assert not hasattr(client, "_default_tier")


def test_build_llm_client_refuses_implicit_standard_when_router_default_tier_unresolved(monkeypatch):
    monkeypatch.setattr(
        "src.services.llm.config_source.build_litellm_config_source",
        lambda settings: None,
    )

    client = build_llm_client(_Context(router=SimpleNamespace(), settings=SimpleNamespace()))

    assert hasattr(client, "invoke")
    assert not hasattr(client, "_default_tier")


def test_build_llm_client_refuses_invalid_router_default_tier(monkeypatch):
    monkeypatch.setattr(
        "src.services.llm.config_source.build_litellm_config_source",
        lambda settings: None,
    )

    client = build_llm_client(
        _Context(
            router=SimpleNamespace(
                config_manager=SimpleNamespace(get_default_tier=lambda: "deep"),
            ),
            settings=SimpleNamespace(),
        )
    )

    assert hasattr(client, "invoke")
    assert not hasattr(client, "_default_tier")


@pytest.mark.asyncio
async def test_build_unavailable_llm_client_accepts_intent_and_returns_router_unavailable_error():
    client = build_unavailable_llm_client(SimpleNamespace(environment="development"))

    response = await client.invoke(
        messages=[{"role": "user", "content": "hello"}],
        intent=LLMCallIntent(purpose=Purpose.GENERATE),
    )

    assert "LiteLLM Router unavailable" in response.content
    assert (
        "direct provider invocation is disabled" in response.content
        or "no valid default_tier/model could be resolved from litellm.json" in response.content
    )


@pytest.mark.asyncio
async def test_build_unavailable_llm_client_reports_invalid_litellm_config_resolution():
    client = build_unavailable_llm_client(
        None,
        config_manager=SimpleNamespace(
            get_default_tier=lambda: "standard",
            get_tier_model=lambda tier: None,
        ),
    )

    response = await client.invoke(
        messages=[{"role": "user", "content": "hello"}],
        system_blocks=[{"type": "text", "text": "cached"}],
        intent=LLMCallIntent(purpose=Purpose.GENERATE),
    )

    assert "no valid default_tier/model could be resolved from litellm.json" in response.content


@pytest.mark.asyncio
async def test_build_unavailable_llm_client_swallows_broken_config_manager_resolution():
    client = build_unavailable_llm_client(
        None,
        config_manager=SimpleNamespace(
            get_default_tier=lambda: (_ for _ in ()).throw(RuntimeError("boom")),
            get_tier_model=lambda tier: "tier-standard",
        ),
    )

    response = await client.invoke(
        messages=[{"role": "user", "content": "hello"}],
        intent=LLMCallIntent(purpose=Purpose.GENERATE),
    )

    assert "no valid default_tier/model could be resolved from litellm.json" in response.content


@pytest.mark.asyncio
async def test_build_direct_llm_client_remains_deprecated_alias_of_unavailable_client():
    with pytest.deprecated_call(match="build_direct_llm_client\\(\\) is deprecated"):
        client = build_direct_llm_client(SimpleNamespace(environment="development"))

    response = await client.invoke(
        messages=[{"role": "user", "content": "hello"}],
        intent=LLMCallIntent(purpose=Purpose.GENERATE),
    )

    assert "LiteLLM Router unavailable" in response.content


def test_bootstrap_deprecated_direct_builder_export_warns():
    bootstrap = importlib.import_module("src.bootstrap")

    with pytest.deprecated_call(match="_build_direct_llm_client is deprecated"):
        builder = bootstrap._build_direct_llm_client

    assert callable(builder)
