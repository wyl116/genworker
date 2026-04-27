"""
LLM Service Module

Provides LiteLLM Router integration for unified LLM access.

Usage:
    from src.services.llm import (
        get_litellm_router,
        initialize_litellm_router,
        warmup_llm_connection,
    )

    # Initialize at startup
    await initialize_litellm_router()
    await warmup_llm_connection()

    # Use router
    router = get_litellm_router()
"""

from .litellm_provider import (
    WarmupResult,
    cleanup_litellm_router,
    get_litellm_config_manager,
    get_litellm_router,
    initialize_litellm_router,
    reset_litellm_router,
    warmup_llm_connection,
)
from .config_source import (
    MissingInjectedConfigError,
    build_litellm_config_source,
    register_injected_provider,
)

from .litellm_param_adapter import (
    is_reasoning_model,
    adapt_completion_params,
    register_reasoning_model_pattern,
    setup_litellm_param_callbacks,
)
from .intent import DEFAULT_INTENT, LLMCallIntent, Purpose
from .model_tiers import DEFAULT_TIER, ModelTier
from .routing_policy import RoutingPolicy, TableRoutingPolicy

__all__ = [
    # Provider functions
    "get_litellm_router",
    "get_litellm_config_manager",
    "initialize_litellm_router",
    "cleanup_litellm_router",
    "reset_litellm_router",
    # Warmup
    "warmup_llm_connection",
    "WarmupResult",
    # Config sources
    "build_litellm_config_source",
    "register_injected_provider",
    "MissingInjectedConfigError",
    # Parameter adaptation
    "is_reasoning_model",
    "adapt_completion_params",
    "register_reasoning_model_pattern",
    "setup_litellm_param_callbacks",
    # Intent routing
    "DEFAULT_INTENT",
    "LLMCallIntent",
    "Purpose",
    "DEFAULT_TIER",
    "ModelTier",
    "RoutingPolicy",
    "TableRoutingPolicy",
]
