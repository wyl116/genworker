# edition: baseline
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import src.bootstrap.llm_preflight as llm_preflight
from src.services.llm.config_source import build_litellm_config_source, register_injected_provider


@pytest.fixture(autouse=True)
def _reset_provider_and_env(monkeypatch):
    register_injected_provider(None)
    for key in (
        "LITELLM_CONFIG_SOURCE",
        "LITELLM_CONFIG_JSON",
        "LITELLM_CONFIG_PATH",
    ):
        monkeypatch.delenv(key, raising=False)
    yield
    register_injected_provider(None)


def test_preflight_non_local_requires_source():
    with pytest.raises(SystemExit):
        llm_preflight.preflight_litellm_config_provider(
            SimpleNamespace(environment="production")
        )


def test_preflight_json_registers_provider(monkeypatch):
    config = {
        "default_tier": "standard",
        "model_list": [{"model_name": "default", "litellm_params": {"model": "x"}}],
        "tier_aliases": {
            "fast": "default",
            "standard": "default",
            "strong": "default",
            "reasoning": "default",
        },
        "fallbacks": [],
    }
    monkeypatch.setenv("LITELLM_CONFIG_SOURCE", "json")
    monkeypatch.setenv("LITELLM_CONFIG_JSON", json.dumps(config))

    llm_preflight.preflight_litellm_config_provider(
        SimpleNamespace(environment="production")
    )

    manager = build_litellm_config_source(SimpleNamespace(environment="production"))
    assert manager is not None
    assert manager.get_available_model_names() == ["default"]


def test_preflight_json_invalid_json_raises(monkeypatch):
    monkeypatch.setenv("LITELLM_CONFIG_SOURCE", "json")
    monkeypatch.setenv("LITELLM_CONFIG_JSON", "{broken")

    with pytest.raises(SystemExit):
        llm_preflight.preflight_litellm_config_provider(
            SimpleNamespace(environment="production")
        )


def test_preflight_file_registers_provider(tmp_path, monkeypatch):
    path = tmp_path / "litellm.json"
    path.write_text(
        json.dumps(
            {
                "default_tier": "standard",
                "model_list": [{"model_name": "default", "litellm_params": {"model": "x"}}],
                "tier_aliases": {
                    "fast": "default",
                    "standard": "default",
                    "strong": "default",
                    "reasoning": "default",
                },
                "fallbacks": [],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("LITELLM_CONFIG_SOURCE", "file")
    monkeypatch.setenv("LITELLM_CONFIG_PATH", str(path))

    llm_preflight.preflight_litellm_config_provider(
        SimpleNamespace(environment="production")
    )

    manager = build_litellm_config_source(SimpleNamespace(environment="production"))
    assert manager is not None
    assert manager.get_tier_model("standard") == "default"


def test_preflight_nacos_not_implemented(monkeypatch):
    monkeypatch.setenv("LITELLM_CONFIG_SOURCE", "nacos")

    with pytest.raises(SystemExit):
        llm_preflight.preflight_litellm_config_provider(
            SimpleNamespace(environment="production")
        )


def test_preflight_local_env_returns_without_source(monkeypatch):
    llm_preflight.preflight_litellm_config_provider(
        SimpleNamespace(environment="local")
    )
