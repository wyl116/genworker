# edition: baseline
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import src.services.llm.config_source as config_source
import src.services.llm.litellm_provider as provider
from src.services.llm.config_source import (
    MissingInjectedConfigError,
    build_litellm_config_source,
    register_injected_provider,
)
from src.services.llm.litellm_config import LiteLLMConfigManager


def _config_dict(model_name: str = "default") -> dict[str, object]:
    return {
        "default_tier": "standard",
        "model_list": [
            {
                "model_name": model_name,
                "litellm_params": {
                    "model": "dashscope/qwen-flash",
                    "api_base": "https://example.com/v1",
                    "api_key": "${TEST_LITELLM_API_KEY}",
                },
            }
        ],
        "tier_aliases": {
            "fast": model_name,
            "standard": model_name,
            "strong": model_name,
            "reasoning": model_name,
        },
        "num_retries": 3,
        "timeout": 120,
        "fallbacks": [],
    }


def _write_config(path, model_name: str = "default") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_config_dict(model_name)), encoding="utf-8")


@pytest.fixture(autouse=True)
def _reset_injected_provider():
    register_injected_provider(None)
    yield
    register_injected_provider(None)


def test_local_env_reads_litellm_local_file(tmp_path, monkeypatch):
    _write_config(tmp_path / "configs" / "litellm_local.json", model_name="custom")
    monkeypatch.setenv("TEST_LITELLM_API_KEY", "local-key")
    monkeypatch.setattr(config_source, "_project_root", lambda: tmp_path)

    manager = build_litellm_config_source(SimpleNamespace(environment="local"))

    assert manager is not None
    assert manager.get_available_model_names() == ["custom"]
    assert manager.get_llm_kwargs("custom")["api_key"] == "local-key"


def test_build_litellm_config_source_defaults_to_local_environment(tmp_path, monkeypatch):
    _write_config(tmp_path / "configs" / "litellm_local.json", model_name="custom")
    monkeypatch.setattr(config_source, "_project_root", lambda: tmp_path)

    manager = build_litellm_config_source(SimpleNamespace())

    assert manager is not None
    assert manager.get_available_model_names() == ["custom"]


def test_non_local_env_requires_injected_provider():
    with pytest.raises(MissingInjectedConfigError, match="requires injected provider"):
        build_litellm_config_source(SimpleNamespace(environment="production"))


def test_injected_provider_returning_none_raises():
    register_injected_provider(lambda settings: None)

    with pytest.raises(MissingInjectedConfigError, match="returned None"):
        build_litellm_config_source(SimpleNamespace(environment="prod"))


def test_injected_provider_returns_dict():
    register_injected_provider(lambda settings: _config_dict("injected"))

    manager = build_litellm_config_source(SimpleNamespace(environment="prod"))

    assert manager is not None
    assert manager.get_available_model_names() == ["injected"]


@pytest.mark.asyncio
async def test_initialize_litellm_router_reads_from_config_source(tmp_path, monkeypatch):
    _write_config(tmp_path / "configs" / "litellm_local.json")
    monkeypatch.setenv("TEST_LITELLM_API_KEY", "dev-key")
    monkeypatch.setattr(config_source, "_project_root", lambda: tmp_path)

    class DummyRouter:
        def __init__(self, config_manager, enable_fallback, enable_caching):
            self.config_manager = config_manager
            self.enable_fallback = enable_fallback
            self.enable_caching = enable_caching

        def get_available_models(self):
            return self.config_manager.get_available_model_names()

    monkeypatch.setattr(provider, "LITELLM_AVAILABLE", True)
    monkeypatch.setattr(provider, "LiteLLMRouter", DummyRouter)
    monkeypatch.setattr(provider, "setup_litellm_param_callbacks", lambda: None)
    monkeypatch.setattr(provider, "_setup_litellm_retry_config", lambda: None)
    monkeypatch.setattr(provider, "litellm", None)
    monkeypatch.setattr(
        provider,
        "get_settings",
        lambda: SimpleNamespace(environment="development"),
    )

    provider.reset_litellm_router()
    router = await provider.initialize_litellm_router(force_reinit=True)

    assert isinstance(router, DummyRouter)
    assert provider.get_litellm_config_manager() is not None
    assert provider.get_litellm_config_manager().get_llm_kwargs("default")["api_key"] == "dev-key"
    assert provider.get_litellm_config_manager().get_tier_model("reasoning") == "default"

    provider.reset_litellm_router()


def test_litellm_config_rejects_missing_required_tier_aliases():
    with pytest.raises(ValueError, match="missing required keys"):
        LiteLLMConfigManager.from_config_dict(
            {
                "default_tier": "standard",
                "model_list": [
                    {
                        "model_name": "default",
                        "litellm_params": {"model": "dashscope/qwen-flash"},
                    }
                ],
                "tier_aliases": {
                    "fast": "default",
                },
            }
        )


def test_unknown_tier_alias_keys_raises():
    with pytest.raises(ValueError, match="unknown keys"):
        LiteLLMConfigManager.from_config_dict(
            {
                "default_tier": "standard",
                "model_list": [
                    {
                        "model_name": "default",
                        "litellm_params": {"model": "dashscope/qwen-flash"},
                    }
                ],
                "tier_aliases": {
                    "fast": "default",
                    "standard": "default",
                    "strong": "default",
                    "reasoning": "default",
                    "standard-tools": "default",
                },
            }
        )


def test_litellm_config_rejects_unknown_tier_alias_target():
    with pytest.raises(ValueError, match="unknown model_name"):
        LiteLLMConfigManager.from_config_dict(
            {
                "default_tier": "standard",
                "model_list": [
                    {
                        "model_name": "default",
                        "litellm_params": {"model": "dashscope/qwen-flash"},
                    }
                ],
                "tier_aliases": {
                    "fast": "default",
                    "standard": "default",
                    "strong": "missing",
                    "reasoning": "default",
                },
            }
        )


def test_litellm_config_rejects_unknown_fallback_model():
    with pytest.raises(ValueError, match="fallbacks reference unknown model_name"):
        LiteLLMConfigManager.from_config_dict(
            {
                "default_tier": "standard",
                "model_list": [
                    {
                        "model_name": "default",
                        "litellm_params": {"model": "dashscope/qwen-flash"},
                    }
                ],
                "tier_aliases": {
                    "fast": "default",
                    "standard": "default",
                    "strong": "default",
                    "reasoning": "default",
                },
                "fallbacks": [{"default": ["missing"]}],
            }
        )


def test_litellm_config_rejects_invalid_default_tier():
    with pytest.raises(ValueError, match="default_tier must be one of"):
        LiteLLMConfigManager.from_config_dict(
            {
                "default_tier": "deep",
                "model_list": [
                    {
                        "model_name": "default",
                        "litellm_params": {"model": "dashscope/qwen-flash"},
                    }
                ],
                "tier_aliases": {
                    "fast": "default",
                    "standard": "default",
                    "strong": "default",
                    "reasoning": "default",
                },
            }
        )


def test_litellm_config_requires_default_tier():
    with pytest.raises(ValueError, match="default_tier is required"):
        LiteLLMConfigManager.from_config_dict(
            {
                "model_list": [
                    {
                        "model_name": "default",
                        "litellm_params": {"model": "dashscope/qwen-flash"},
                    }
                ],
                "tier_aliases": {
                    "fast": "default",
                    "standard": "default",
                    "strong": "default",
                    "reasoning": "default",
                },
            }
        )


def test_to_litellm_router_kwargs_strips_app_only_fields():
    manager = LiteLLMConfigManager.from_config_dict(
        {
            "default_tier": "standard",
            "model_list": [
                {
                    "model_name": "default",
                    "litellm_params": {"model": "dashscope/qwen-flash"},
                }
            ],
            "tier_aliases": {
                "fast": "default",
                "standard": "default",
                "strong": "default",
                "reasoning": "default",
            },
            "drop_params": True,
            "timeout": 30,
        }
    )

    kwargs = manager.to_litellm_router_kwargs()

    assert kwargs["model_list"][0]["model_name"] == "default"
    assert kwargs["timeout"] == 30
    assert "tier_aliases" not in kwargs
    assert "drop_params" not in kwargs
    assert "default_tier" not in kwargs
