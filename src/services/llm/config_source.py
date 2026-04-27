"""LiteLLM config loading helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from src.common.settings import Settings

from .litellm_config import LiteLLMConfigManager


_LOCAL_ENV_VALUES = frozenset({"local", "dev", "development"})


class MissingInjectedConfigError(ValueError):
    """Non-local environments require an injected LiteLLM config provider."""


def _project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _resolve_local_config_path() -> Path:
    return _project_root() / "configs" / "litellm_local.json"


_injected_provider: Callable[[Settings], dict[str, Any] | None] | None = None


def register_injected_provider(
    provider: Callable[[Settings], dict[str, Any] | None] | None,
) -> None:
    """Register a non-local LiteLLM config provider."""
    global _injected_provider
    _injected_provider = provider


def _is_local_env(env_name: str | None) -> bool:
    return (env_name or "local").lower() in _LOCAL_ENV_VALUES


def build_litellm_config_source(
    settings: Settings,
) -> LiteLLMConfigManager | None:
    """Load LiteLLM config manager from local file or injected provider."""
    env_name = getattr(settings, "environment", None)
    if _is_local_env(env_name):
        return LiteLLMConfigManager.from_file(_resolve_local_config_path())

    if _injected_provider is None:
        raise MissingInjectedConfigError(
            f"LiteLLM config for environment={env_name!r} requires injected provider "
            f"(register_injected_provider(fn)); local-only file load disabled"
        )

    config_dict = _injected_provider(settings)
    if config_dict is None:
        raise MissingInjectedConfigError(
            f"injected LiteLLM config provider returned None for environment={env_name!r}"
        )

    return LiteLLMConfigManager.from_config_dict(config_dict)
