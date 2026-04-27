"""
LiteLLM Router Provider

Provides global singleton management for LiteLLM Router:
- Initialization from env-specific LiteLLM JSON config files
- Connection warmup for reduced first-request latency
- Parameter adaptation for reasoning models
"""

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from src.common.logger import get_logger
from src.common.settings import get_settings

try:
    from .config_source import (
        MissingInjectedConfigError,
        build_litellm_config_source,
    )
    from .litellm_config import LiteLLMConfigManager
    from .litellm_router import LITELLM_AVAILABLE, LiteLLMRouter
    from .litellm_param_adapter import (
        setup_litellm_param_callbacks,
        register_reasoning_model_pattern,
    )
    import litellm
except ImportError:
    MissingInjectedConfigError = ValueError
    build_litellm_config_source = None
    LiteLLMConfigManager = None
    LiteLLMRouter = None
    LITELLM_AVAILABLE = False
    setup_litellm_param_callbacks = None
    register_reasoning_model_pattern = None
    litellm = None

logger = get_logger()

_litellm_router: Optional["LiteLLMRouter"] = None
_litellm_config_manager: Optional["LiteLLMConfigManager"] = None
_initialization_lock = asyncio.Lock()
_initialized = False
_DEFAULT_WARMUP_TIER_ORDER = (
    "standard",
    "strong",
    "fast",
    "reasoning",
)


def _setup_litellm_retry_config() -> None:
    """Configure global retry settings for LiteLLM."""
    if litellm is None:
        return

    litellm.num_retries = 3
    litellm.request_timeout = 120

    try:
        from litellm import RetryPolicy
        litellm.retry_policy = RetryPolicy(
            TimeoutErrorRetries=2,
            RateLimitErrorRetries=3,
            ContentPolicyViolationErrorRetries=0,
            InternalServerErrorRetries=2,
            ServiceUnavailableErrorRetries=3,
        )
        logger.info(
            "[LiteLLM] Global retry config applied | "
            "num_retries=3 | timeout=120s"
        )
    except ImportError:
        logger.info(
            "[LiteLLM] Global retry config applied (basic) | "
            "num_retries=3 | timeout=120s"
        )


async def initialize_litellm_router(
    force_reinit: bool = False,
) -> Optional["LiteLLMRouter"]:
    """
    Initialize LiteLLM Router from an env-specific LiteLLM config file.

    Returns:
        LiteLLMRouter instance or None if not available
    """
    global _litellm_router, _litellm_config_manager, _initialized

    if not LITELLM_AVAILABLE:
        logger.warning(
            "LiteLLM is not installed. Install with: pip install litellm"
        )
        return None

    async with _initialization_lock:
        if _initialized and not force_reinit:
            return _litellm_router

        if setup_litellm_param_callbacks:
            setup_litellm_param_callbacks()

        if litellm is not None:
            _setup_litellm_retry_config()

        try:
            settings = get_settings()
            _litellm_config_manager = build_litellm_config_source(settings)
        except MissingInjectedConfigError:
            raise
        except (OSError, ValueError) as e:
            logger.error(f"Failed to load LiteLLM configuration source: {e}")
            return None

        if _litellm_config_manager is None:
            logger.warning("LiteLLM configuration source returned no config")
            return None

        try:
            _litellm_router = LiteLLMRouter(
                config_manager=_litellm_config_manager,
                enable_fallback=True,
                enable_caching=False,
            )
            _initialized = True
            logger.info(
                f"LiteLLM Router initialized | "
                f"models={_litellm_config_manager.get_available_model_names()}"
            )
            return _litellm_router
        except Exception as e:
            logger.error(f"Failed to initialize LiteLLM Router: {e}")
            return None


def get_litellm_router() -> Optional["LiteLLMRouter"]:
    """Get the global LiteLLM Router singleton."""
    return _litellm_router


def get_litellm_config_manager() -> Optional["LiteLLMConfigManager"]:
    """Get the global LiteLLM Config Manager singleton."""
    return _litellm_config_manager


async def cleanup_litellm_router() -> None:
    """Cleanup LiteLLM Router resources."""
    global _litellm_router, _litellm_config_manager, _initialized

    _litellm_router = None
    _litellm_config_manager = None
    _initialized = False
    logger.info("LiteLLM Router cleanup completed")


def reset_litellm_router() -> None:
    """Reset LiteLLM Router (for testing)."""
    global _litellm_router, _litellm_config_manager, _initialized
    _litellm_router = None
    _litellm_config_manager = None
    _initialized = False


@dataclass
class WarmupResult:
    """LLM connection warmup result."""
    model: str
    success: bool
    latency_ms: float
    error: Optional[str] = None


async def warmup_llm_connection(
    models: Optional[List[str]] = None,
    timeout_seconds: float = 30.0,
) -> List[WarmupResult]:
    """
    Warmup LLM connections to reduce first-request latency.

    Args:
        models: List of models to warmup. If None, warmup default model.
        timeout_seconds: Warmup timeout in seconds.

    Returns:
        List of warmup results.
    """
    if _litellm_router is None:
        logger.warning("[LLM Warmup] Router not initialized, skipping warmup")
        return []

    if models is None:
        models = _resolve_default_warmup_models(_litellm_router)
        if not models:
            logger.warning("[LLM Warmup] No available models")
            return []

    logger.info(f"[LLM Warmup] Starting warmup | models={models}")

    tasks = [_warmup_single_model(model, timeout_seconds) for model in models]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    warmup_results: List[WarmupResult] = []
    for model, result in zip(models, results):
        if isinstance(result, Exception):
            warmup_results.append(WarmupResult(
                model=model, success=False, latency_ms=0, error=str(result),
            ))
        else:
            warmup_results.append(result)

    success_count = sum(1 for r in warmup_results if r.success)
    if success_count > 0:
        avg_latency = sum(
            r.latency_ms for r in warmup_results if r.success
        ) / success_count
        logger.info(
            f"[LLM Warmup] Complete | "
            f"success={success_count}/{len(warmup_results)} | "
            f"avg_latency={avg_latency:.0f}ms"
        )
    else:
        logger.warning(
            f"[LLM Warmup] Failed | "
            f"errors={[r.error for r in warmup_results if r.error]}"
        )

    return warmup_results


def _resolve_default_warmup_models(router: "LiteLLMRouter") -> List[str]:
    """Pick representative model groups for startup warmup."""
    config_manager = getattr(router, "config_manager", None)
    aliases = {}
    if config_manager is not None and hasattr(config_manager, "get_tier_aliases"):
        aliases = config_manager.get_tier_aliases() or {}

    selected: list[str] = []
    seen: set[tuple[str, ...]] = set()
    for tier in _DEFAULT_WARMUP_TIER_ORDER:
        model_name = aliases.get(tier)
        if not model_name:
            continue
        warmup_key = _resolve_warmup_identity(config_manager, model_name)
        if warmup_key in seen:
            continue
        seen.add(warmup_key)
        selected.append(model_name)

    if selected:
        return selected

    if (
        config_manager is not None
        and hasattr(config_manager, "get_default_tier")
        and hasattr(config_manager, "get_tier_model")
    ):
        try:
            default_model = config_manager.get_tier_model(
                config_manager.get_default_tier()
            )
        except Exception:
            default_model = None
        if default_model:
            return [default_model]

    return []


def _resolve_warmup_identity(
    config_manager: Any | None,
    model_name: str,
) -> tuple[str, ...]:
    """
    Resolve the effective warmup identity for a model group.

    Warmup should only run once per underlying deployment signature. This
    prevents alias tiers like ``tier-strong`` and ``tier-reasoning`` from
    concurrently probing the same upstream model + endpoint + credential.
    """
    if config_manager is None or not hasattr(config_manager, "get_llm_kwargs"):
        return ("model_group", model_name)

    llm_kwargs = config_manager.get_llm_kwargs(model_name) or {}
    if not llm_kwargs:
        return ("model_group", model_name)

    return (
        "deployment",
        str(llm_kwargs.get("model") or model_name).strip(),
        str(llm_kwargs.get("api_base") or "").strip(),
        str(llm_kwargs.get("api_version") or "").strip(),
        str(llm_kwargs.get("api_key") or "").strip(),
    )


async def _warmup_single_model(
    model: str, timeout_seconds: float,
) -> WarmupResult:
    """Warmup a single model's connection."""
    from .litellm_router import ChatCompletionRequest, ChatMessage

    start_time = time.monotonic()

    try:
        request = ChatCompletionRequest(
            messages=[ChatMessage(role="user", content="hi")],
            model=model,
            max_tokens=1,
            temperature=0,
        )
        await asyncio.wait_for(
            _litellm_router.chat_completion(request),
            timeout=timeout_seconds,
        )
        latency_ms = (time.monotonic() - start_time) * 1000
        return WarmupResult(model=model, success=True, latency_ms=latency_ms)

    except asyncio.TimeoutError:
        latency_ms = (time.monotonic() - start_time) * 1000
        return WarmupResult(
            model=model, success=False, latency_ms=latency_ms,
            error=f"Warmup timeout ({timeout_seconds}s)",
        )
    except Exception as e:
        latency_ms = (time.monotonic() - start_time) * 1000
        return WarmupResult(
            model=model, success=False, latency_ms=latency_ms, error=str(e),
        )
