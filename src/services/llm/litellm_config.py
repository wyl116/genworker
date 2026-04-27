# -*- coding: utf-8 -*-
"""
LiteLLM Configuration Manager

Loads LiteLLM Router configuration dictionaries using LiteLLM's native
format. Supports environment variable substitution.

Configuration Format (LiteLLM Native):
    {
        "model_list": [
            {
                "model_name": "gpt-4o",
                "litellm_params": {
                    "model": "azure/gpt-4o",
                    "api_base": "https://node1.openai.azure.com/",
                    "api_key": "${AZURE_API_KEY}"
                }
            },
            {
                "model_name": "gpt-4o",  // Same name for load balancing
                "litellm_params": {
                    "model": "azure/gpt-4o",
                    "api_base": "https://node2.openai.azure.com/",
                    "api_key": "${AZURE_API_KEY_2}"
                }
            }
        ],
        "routing_strategy": "simple-shuffle",
        "num_retries": 3,
        "timeout": 120,
        "fallbacks": [{"gpt-4o": ["claude-3"]}]
    }

Reference: https://docs.litellm.ai/docs/routing
"""

import json
import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Any

from .model_tiers import ModelTier

REQUIRED_TIER_ALIAS_KEYS = frozenset({
    "fast",
    "standard",
    "strong",
    "reasoning",
})


def substitute_env_vars(value: Any) -> Any:
    """
    Recursively substitute environment variables in format ${VAR_NAME}

    Args:
        value: Value to process (str, dict, list, or other)

    Returns:
        Value with environment variables substituted
    """
    if isinstance(value, str):
        pattern = r'\$\{([^}]+)\}'

        def replace_env(match):
            env_var = match.group(1)
            return os.environ.get(env_var, match.group(0))

        return re.sub(pattern, replace_env, value)

    elif isinstance(value, dict):
        return {k: substitute_env_vars(v) for k, v in value.items()}

    elif isinstance(value, list):
        return [substitute_env_vars(item) for item in value]

    return value


class LiteLLMConfigManager:
    """
    Manager for LiteLLM Router configurations.

    Uses LiteLLM's native configuration format directly.
    Provides helper methods for common operations.
    """

    def __init__(self, config: Dict[str, Any]):
        """
        Initialize with LiteLLM config dictionary.

        Args:
            config: LiteLLM Router configuration (native format)
        """
        # Substitute environment variables
        self.config = substitute_env_vars(config)
        self._validate_router_config()

    @classmethod
    def from_config_dict(cls, config_dict: Dict[str, Any]) -> "LiteLLMConfigManager":
        """
        Create manager from a configuration dictionary.

        Args:
            config_dict: LiteLLM Router configuration dict

        Returns:
            LiteLLMConfigManager instance
        """
        return cls(config_dict)

    @classmethod
    def from_file(
        cls,
        file_path: Path | str,
    ) -> Optional["LiteLLMConfigManager"]:
        """
        Create manager from a LiteLLM JSON config file.

        Returns None when the file does not exist.
        """
        path = Path(file_path)
        if not path.exists():
            return None

        with path.open("r", encoding="utf-8") as handle:
            config_dict = json.load(handle)

        if not isinstance(config_dict, dict):
            raise ValueError("LiteLLM config file must contain a JSON object")

        model_list = config_dict.get("model_list")
        if not isinstance(model_list, list) or not model_list:
            raise ValueError("LiteLLM config file must contain a non-empty model_list")

        return cls.from_config_dict(config_dict)

    def to_litellm_router_kwargs(self) -> Dict[str, Any]:
        """
        Get kwargs for litellm.Router() initialization.

        Returns:
            Dictionary suitable for litellm.Router(**kwargs)
        """
        config = self.config.copy()

        # Note: drop_params is a global setting (litellm.drop_params = True),
        # not a Router constructor parameter. It is set in litellm_router.py.
        config.pop("drop_params", None)
        # tier_aliases is an app-level routing abstraction used before dispatch.
        # LiteLLM Router does not recognize it as a constructor argument.
        config.pop("tier_aliases", None)
        # default_tier is app-level policy fallback metadata, not Router config.
        config.pop("default_tier", None)

        return config

    def get_model_list(self) -> List[Dict[str, Any]]:
        """Get the model_list from config"""
        return self.config.get("model_list", [])

    def get_available_model_names(self) -> List[str]:
        """Get unique model names from model_list, preserving model_list order."""
        return list(dict.fromkeys(
            model["model_name"]
            for model in self.get_model_list()
            if "model_name" in model
        ))

    def get_model_deployments(self, model_name: str) -> List[Dict[str, Any]]:
        """
        Get all deployments for a specific model name.

        Args:
            model_name: Model name to lookup

        Returns:
            List of model configurations with this name
        """
        return [
            model for model in self.get_model_list()
            if model.get("model_name") == model_name
        ]

    def get_llm_kwargs(self, model_name: str) -> Optional[Dict[str, Any]]:
        """
        Get LLM kwargs for a specific model (primary deployment).

        For multi-node models, returns the first deployment.
        For full load balancing, use LiteLLM Router directly.

        Args:
            model_name: Model name to lookup

        Returns:
            LiteLLM params dict, or None if not found
        """
        deployments = self.get_model_deployments(model_name)
        if not deployments:
            return None

        # Return first deployment's litellm_params
        return deployments[0].get("litellm_params", {}).copy()

    def get_routing_strategy(self) -> str:
        """Get routing strategy"""
        return self.config.get("routing_strategy", "simple-shuffle")

    def get_fallbacks(self) -> List[Dict[str, List[str]]]:
        """Get fallback configuration"""
        return self.config.get("fallbacks", [])

    def get_fallback_models(self, model_name: str) -> List[str]:
        """
        Get fallback models for a specific model.

        Args:
            model_name: Model name to get fallbacks for

        Returns:
            List of fallback model names, empty if none configured
        """
        fallbacks = self.get_fallbacks()
        for fallback_mapping in fallbacks:
            if model_name in fallback_mapping:
                return fallback_mapping[model_name]
        return []

    def get_tier_aliases(self) -> Dict[str, str]:
        """Get tier alias mapping from config."""
        aliases = self.config.get("tier_aliases", {})
        if not isinstance(aliases, dict):
            return {}
        return {
            str(key): str(value)
            for key, value in aliases.items()
            if str(key).strip() and str(value).strip()
        }

    def get_tier_model(self, tier: str) -> Optional[str]:
        """Resolve a logical tier key to a configured LiteLLM model group."""
        return self.get_tier_aliases().get(str(tier).strip())

    def get_default_tier(self) -> str:
        """Get the config-defined default base tier for policy fallback."""
        return ModelTier.from_value(self.config["default_tier"]).value

    def _validate_router_config(self) -> None:
        """Validate cross-field consistency before router startup."""
        model_names = set(self.get_available_model_names())
        if not model_names:
            raise ValueError("LiteLLM config must define at least one model")

        self._validate_default_tier()
        self._validate_tier_aliases(model_names)
        self._validate_fallbacks(model_names)

    def _validate_default_tier(self) -> None:
        raw_default = self.config.get("default_tier")
        if raw_default is None:
            raise ValueError("default_tier is required")
        if not ModelTier.is_valid(raw_default):
            raise ValueError(
                f"default_tier must be one of: "
                f"{', '.join(tier.value for tier in ModelTier)}"
            )

    def _validate_tier_aliases(self, model_names: set[str]) -> None:
        raw_aliases = self.config.get("tier_aliases")
        if raw_aliases is None:
            return
        if not isinstance(raw_aliases, dict):
            raise ValueError("tier_aliases must be a JSON object")

        aliases = self.get_tier_aliases()
        missing = sorted(REQUIRED_TIER_ALIAS_KEYS - set(aliases))
        if missing:
            raise ValueError(
                f"tier_aliases missing required keys: {', '.join(missing)}"
            )

        unknown = sorted(set(aliases) - REQUIRED_TIER_ALIAS_KEYS)
        if unknown:
            raise ValueError(
                f"tier_aliases contains unknown keys: {', '.join(unknown)}; "
                f"only {sorted(REQUIRED_TIER_ALIAS_KEYS)} are supported"
            )

        invalid_targets = sorted(
            f"{alias}->{target}"
            for alias, target in aliases.items()
            if target not in model_names
        )
        if invalid_targets:
            raise ValueError(
                "tier_aliases reference unknown model_name entries: "
                + ", ".join(invalid_targets)
            )

    def _validate_fallbacks(self, model_names: set[str]) -> None:
        fallbacks = self.get_fallbacks()
        if not isinstance(fallbacks, list):
            raise ValueError("fallbacks must be a list")

        invalid_refs: list[str] = []
        for mapping in fallbacks:
            if not isinstance(mapping, dict):
                raise ValueError("each fallback entry must be an object")
            for source, targets in mapping.items():
                if source not in model_names:
                    invalid_refs.append(f"source:{source}")
                if not isinstance(targets, list):
                    raise ValueError("fallback targets must be a list")
                for target in targets:
                    if target not in model_names:
                        invalid_refs.append(f"target:{source}->{target}")

        if invalid_refs:
            raise ValueError(
                "fallbacks reference unknown model_name entries: "
                + ", ".join(invalid_refs)
            )
