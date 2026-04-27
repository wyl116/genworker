# -*- coding: utf-8 -*-
"""
LiteLLM Parameter Adapter

Provides parameter adaptation for different model types.
Centralizes parameter transformation logic following domain cohesion principle.

Key Features:
- Reasoning model detection (GPT-5, o1, o3, o4 series)
- max_tokens -> max_completion_tokens conversion
- Extensible adapter pattern for future model-specific adaptations

Usage:
    This module is automatically integrated via litellm_provider.py
    during LiteLLM Router initialization. No manual usage required.
"""

import re
from typing import Any, Dict, FrozenSet, Optional, Set

from src.common.logger import get_logger

logger = get_logger()

# =============================================================================
# Reasoning Model Detection
# =============================================================================

# Default reasoning model patterns (can be overridden via configuration)
DEFAULT_REASONING_MODEL_PATTERNS: FrozenSet[str] = frozenset({
    # GPT-5 series
    'gpt-5', 'gpt5',
    # o-series reasoning models
    'o1', 'o3', 'o4',
    # Azure prefixed variants
    'azure/gpt-5', 'azure/gpt5',
    'azure/o1', 'azure/o3', 'azure/o4',
})

# Compiled regex for efficient matching
_REASONING_MODEL_REGEX: Optional[re.Pattern] = None
_custom_reasoning_patterns: Set[str] = set()


def _compile_reasoning_pattern() -> re.Pattern:
    """Compile regex pattern for reasoning model detection."""
    all_patterns = DEFAULT_REASONING_MODEL_PATTERNS | _custom_reasoning_patterns
    # Escape special regex chars and create alternation pattern
    escaped = [re.escape(p) for p in all_patterns]
    pattern = '|'.join(escaped)
    return re.compile(f'({pattern})', re.IGNORECASE)


def register_reasoning_model_pattern(pattern: str) -> None:
    """
    Register additional reasoning model pattern.

    Allows dynamic extension of reasoning model detection
    without code changes (e.g., from Nacos configuration).

    Args:
        pattern: Model name pattern to register (case-insensitive)
    """
    global _REASONING_MODEL_REGEX
    _custom_reasoning_patterns.add(pattern.lower())
    _REASONING_MODEL_REGEX = _compile_reasoning_pattern()
    logger.debug(f"Registered reasoning model pattern: {pattern}")


def clear_custom_reasoning_patterns() -> None:
    """Clear all custom reasoning model patterns (for testing)."""
    global _REASONING_MODEL_REGEX
    _custom_reasoning_patterns.clear()
    _REASONING_MODEL_REGEX = _compile_reasoning_pattern()


def is_reasoning_model(model_name: Optional[str]) -> bool:
    """
    Check if a model is a reasoning model that requires max_completion_tokens.

    GPT-5, o1, o3, o4 series models don't support max_tokens parameter,
    they require max_completion_tokens instead.

    Args:
        model_name: Model name to check

    Returns:
        True if model is a reasoning model
    """
    global _REASONING_MODEL_REGEX

    if not model_name:
        return False

    # Lazy initialization
    if _REASONING_MODEL_REGEX is None:
        _REASONING_MODEL_REGEX = _compile_reasoning_pattern()

    model_lower = model_name.lower()
    return bool(_REASONING_MODEL_REGEX.search(model_lower))


# =============================================================================
# Parameter Adaptation
# =============================================================================

def adapt_completion_params(
    params: Dict[str, Any],
    model: Optional[str] = None
) -> Dict[str, Any]:
    """
    Adapt completion parameters for specific model requirements.

    Primary adaptation:
    - Convert max_tokens to max_completion_tokens for reasoning models

    Args:
        params: Original completion parameters
        model: Model name (if not in params)

    Returns:
        Adapted parameters (modified in-place and returned)
    """
    # Get model name
    model_name = model or params.get('model')

    if not model_name:
        return params

    # Adapt for reasoning models
    if is_reasoning_model(model_name):
        _adapt_for_reasoning_model(params, model_name)

    return params


def _adapt_for_reasoning_model(params: Dict[str, Any], model_name: str) -> None:
    """
    Adapt parameters for reasoning models (GPT-5, o1, o3, o4).

    These models require max_completion_tokens instead of max_tokens.
    """
    if 'max_tokens' in params and 'max_completion_tokens' not in params:
        max_tokens = params.pop('max_tokens')
        params['max_completion_tokens'] = max_tokens
        logger.debug(
            f"Adapted max_tokens -> max_completion_tokens "
            f"for reasoning model {model_name}"
        )


# =============================================================================
# LiteLLM Callback Integration
# =============================================================================

def litellm_pre_call_adapter(
    model: str,
    messages: list,
    optional_params: Dict[str, Any],
    *args,
    **kwargs
) -> Dict[str, Any]:
    """
    LiteLLM pre-call hook for parameter adaptation.

    This function is called by LiteLLM before every API call,
    including during internal fallback operations.

    Integration:
        import litellm
        litellm.pre_call_rules = [litellm_pre_call_adapter]

    Args:
        model: Model being called
        messages: Chat messages
        optional_params: Parameters to adapt

    Returns:
        Adapted optional_params
    """
    return adapt_completion_params(optional_params, model)


def setup_litellm_param_callbacks() -> None:
    """
    Setup LiteLLM global callbacks for parameter adaptation.

    This should be called once during application startup,
    after LiteLLM is imported but before any API calls.

    The callback ensures parameter adaptation happens at the
    framework level, including during LiteLLM's internal
    fallback operations.
    """
    try:
        import litellm

        # Method 1: Use modify_params callback (LiteLLM 1.40+)
        if hasattr(litellm, 'modify_params'):
            original_modify = getattr(litellm, 'modify_params', None)

            def param_adapter_callback(data: dict) -> dict:
                """Global parameter adapter callback."""
                model = data.get('model', '')

                # Apply adaptation
                adapt_completion_params(data, model)

                # Chain to original callback if exists
                if original_modify and callable(original_modify):
                    return original_modify(data)
                return data

            litellm.modify_params = param_adapter_callback
            logger.info(
                "LiteLLM param adapter registered via modify_params callback"
            )
            return

        # Method 2: Use input_callback (alternative)
        if hasattr(litellm, 'input_callback'):
            def input_adapter(model, messages, kwargs):
                """Input callback for parameter adaptation."""
                adapt_completion_params(kwargs, model)

            if litellm.input_callback is None:
                litellm.input_callback = []
            litellm.input_callback.append(input_adapter)
            logger.info(
                "LiteLLM param adapter registered via input_callback"
            )
            return

        logger.warning(
            "LiteLLM param adapter: No suitable callback mechanism found. "
            "Parameter adaptation will rely on application-level handling."
        )

    except ImportError:
        logger.debug("LiteLLM not available, skipping callback setup")
    except Exception as e:
        logger.warning(f"Failed to setup LiteLLM param callbacks: {e}")
