"""Preflight injection for non-local LiteLLM config providers."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from src.common.logger import get_logger
from src.common.settings import Settings
from src.services.llm.config_source import (
    _LOCAL_ENV_VALUES,
    register_injected_provider,
)

logger = get_logger()

_SOURCE_ENV = "LITELLM_CONFIG_SOURCE"
_INLINE_ENV = "LITELLM_CONFIG_JSON"
_FILE_ENV = "LITELLM_CONFIG_PATH"


def preflight_litellm_config_provider(settings: Settings) -> None:
    env_name = (getattr(settings, "environment", None) or "local").lower()
    if env_name in _LOCAL_ENV_VALUES:
        return

    source = (os.environ.get(_SOURCE_ENV) or "").strip().lower()
    if source == "json":
        raw = os.environ.get(_INLINE_ENV)
        if not raw:
            _abort(f"{_SOURCE_ENV}=json requires {_INLINE_ENV}")
        config_dict = _parse_json(raw)
        register_injected_provider(lambda _settings: config_dict)
    elif source == "file":
        path = os.environ.get(_FILE_ENV)
        if not path:
            _abort(f"{_SOURCE_ENV}=file requires {_FILE_ENV}")
        config_dict = _parse_json(Path(path).read_text(encoding="utf-8"))
        register_injected_provider(lambda _settings: config_dict)
    elif source == "nacos":
        _abort("LITELLM_CONFIG_SOURCE=nacos not implemented in this release")
    else:
        _abort(
            f"environment={env_name!r} requires {_SOURCE_ENV} "
            f"(supported: json | file; nacos reserved)"
        )

    logger.info(
        "[LLMPreflight] injected LiteLLM config provider | source=%s env=%s",
        source,
        env_name,
    )


def _parse_json(raw: str) -> dict[str, Any]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        _abort(f"invalid JSON in LiteLLM config source: {exc}")
    if not isinstance(data, dict):
        _abort("LiteLLM config source must be a JSON object")
    return data


def _abort(reason: str) -> None:
    logger.error("[LLMPreflight] %s", reason)
    sys.exit(1)
