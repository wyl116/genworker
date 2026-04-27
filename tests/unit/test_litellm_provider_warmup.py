# edition: baseline
from __future__ import annotations

from types import SimpleNamespace

import pytest

import src.services.llm.litellm_provider as provider


def test_resolve_default_warmup_models_prefers_tier_aliases():
    router = SimpleNamespace(
        config_manager=SimpleNamespace(
            get_tier_aliases=lambda: {
                "standard": "tier-standard",
                "strong": "tier-strong",
                "fast": "tier-fast",
                "reasoning": "tier-reasoning",
            }
        ),
        get_available_models=lambda: ["legacy-a", "legacy-b"],
    )

    models = provider._resolve_default_warmup_models(router)

    assert models == [
        "tier-standard",
        "tier-strong",
        "tier-fast",
        "tier-reasoning",
    ]


def test_resolve_default_warmup_models_deduplicates_alias_targets():
    router = SimpleNamespace(
        config_manager=SimpleNamespace(
            get_tier_aliases=lambda: {
                "standard": "shared",
                "strong": "shared-strong",
                "fast": "shared",
                "reasoning": "shared-strong",
            }
        ),
        get_available_models=lambda: ["legacy-a"],
    )

    models = provider._resolve_default_warmup_models(router)

    assert models == ["shared", "shared-strong"]


def test_resolve_default_warmup_models_deduplicates_shared_deployments():
    llm_kwargs = {
        "tier-standard": {
            "model": "openai/glm-4.7",
            "api_base": "https://example.test/v4",
            "api_key": "k-standard",
        },
        "tier-strong": {
            "model": "openai/glm-5.1",
            "api_base": "https://example.test/v4",
            "api_key": "k-shared",
        },
        "tier-fast": {
            "model": "openai/glm-4.7-flash",
            "api_base": "https://example.test/v4",
            "api_key": "k-fast",
        },
        "tier-reasoning": {
            "model": "openai/glm-5.1",
            "api_base": "https://example.test/v4",
            "api_key": "k-shared",
        },
    }
    router = SimpleNamespace(
        config_manager=SimpleNamespace(
            get_tier_aliases=lambda: {
                "standard": "tier-standard",
                "strong": "tier-strong",
                "fast": "tier-fast",
                "reasoning": "tier-reasoning",
            },
            get_llm_kwargs=lambda model_name: llm_kwargs[model_name],
        ),
        get_available_models=lambda: ["legacy-a"],
    )

    models = provider._resolve_default_warmup_models(router)

    assert models == ["tier-standard", "tier-strong", "tier-fast"]


def test_resolve_default_warmup_models_falls_back_to_configured_default_tier():
    router = SimpleNamespace(
        config_manager=SimpleNamespace(
            get_tier_aliases=lambda: {},
            get_default_tier=lambda: "standard",
            get_tier_model=lambda tier: "tier-standard" if tier == "standard" else None,
        ),
        get_available_models=lambda: ["legacy-a", "legacy-b"],
    )

    models = provider._resolve_default_warmup_models(router)

    assert models == ["tier-standard"]


def test_resolve_default_warmup_models_returns_empty_when_no_configured_default():
    router = SimpleNamespace(
        config_manager=SimpleNamespace(get_tier_aliases=lambda: {}),
        get_available_models=lambda: ["legacy-a", "legacy-b"],
    )

    models = provider._resolve_default_warmup_models(router)

    assert models == []


@pytest.mark.asyncio
async def test_warmup_llm_connection_uses_resolved_default_models(monkeypatch):
    provider.reset_litellm_router()
    provider._litellm_router = SimpleNamespace(
        config_manager=SimpleNamespace(
            get_tier_aliases=lambda: {
                "standard": "tier-standard",
                "strong": "tier-strong",
            }
        ),
        get_available_models=lambda: ["legacy"],
    )
    warmed: list[str] = []

    async def _fake_warmup(model: str, timeout_seconds: float):
        warmed.append(model)
        return provider.WarmupResult(model=model, success=True, latency_ms=10.0)

    monkeypatch.setattr(provider, "_warmup_single_model", _fake_warmup)

    results = await provider.warmup_llm_connection()

    assert [item.model for item in results] == ["tier-standard", "tier-strong"]
    assert warmed == ["tier-standard", "tier-strong"]

    provider.reset_litellm_router()
