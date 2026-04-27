"""Adapt LiteLLM Router to the engine LLMClient protocol."""

from __future__ import annotations

import json
import time
from typing import Any

from src.common.logger import get_logger
from src.engine.protocols import LLMResponse, ToolCall, UsageInfo

from .intent import DEFAULT_INTENT, LLMCallIntent
from .llm_request_logger import LLMCallRecord, log_llm_error, log_llm_request, log_llm_response
from .litellm_router import LiteLLMRouter
from .model_tiers import ModelTier
from .routing_policy import RoutingPolicy, TableRoutingPolicy

logger = get_logger()


class LiteLLMRouterAdapter:
    """Bridge LiteLLMRouter to the engine-facing LLMClient protocol."""

    def __init__(
        self,
        router: LiteLLMRouter,
        policy: RoutingPolicy | None = None,
        default_tier: ModelTier | None = None,
    ) -> None:
        self._router = router
        self._policy = policy or TableRoutingPolicy()
        self._default_tier = default_tier or _resolve_config_default_tier(
            getattr(router, "config_manager", None)
        )

    async def invoke(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        system_blocks: list[dict[str, Any]] | None = None,
        intent: LLMCallIntent | None = None,
    ) -> LLMResponse:
        resolved_intent = intent or DEFAULT_INTENT
        try:
            tier_name = self._policy.choose(resolved_intent)
        except Exception as exc:
            tier_name = self._default_tier_name()
            logger.warning(
                "[LLM Tier] policy failed: %s, fallback to default_tier=%s",
                exc,
                tier_name,
            )

        model_name = (
            self._router.config_manager.get_tier_model(tier_name)
        )
        if not model_name:
            logger.error(
                "[LLM Tier] no model configured for tier=%s purpose=%s",
                tier_name,
                resolved_intent.purpose.value,
            )
            return LLMResponse(
                content=f"LLM Error: No model configured for tier '{tier_name}'"
            )

        kwargs: dict[str, Any] = {
            "messages": messages,
            "model": model_name,
            "metadata": {
                "intent_purpose": resolved_intent.purpose.value,
                "intent_tier": tier_name,
            },
        }
        if system_blocks and self._supports_cache_control(model_name):
            kwargs["system"] = system_blocks
        if tools:
            kwargs["tools"] = [_normalize_tool_schema(tool) for tool in tools]
        if tool_choice:
            kwargs["tool_choice"] = tool_choice

        logger.info(
            "[LLM Tier] purpose=%s tier=%s model_group=%s",
            resolved_intent.purpose.value,
            tier_name,
            model_name,
        )
        log_llm_request(model_name, len(messages), False, kwargs)
        t0 = time.monotonic()

        try:
            response = await self._router.router.acompletion(**kwargs)
            duration_ms = (time.monotonic() - t0) * 1000
            message = response.choices[0].message
            tool_calls_raw = getattr(message, "tool_calls", None) or []
            tool_calls = tuple(
                ToolCall(
                    tool_name=tc.function.name,
                    tool_input=_parse_tool_args(tc.function.arguments),
                    tool_call_id=tc.id or "",
                )
                for tc in tool_calls_raw
            )
            usage_obj = getattr(response, "usage", None)
            usage = UsageInfo(
                prompt_tokens=getattr(usage_obj, "prompt_tokens", 0) or 0,
                completion_tokens=getattr(usage_obj, "completion_tokens", 0) or 0,
                total_tokens=getattr(usage_obj, "total_tokens", 0) or 0,
            )
            actual_model = getattr(response, "model", "") or model_name
            is_fallback = self._did_router_fallback(model_name, actual_model)
            if is_fallback:
                logger.info(
                    "[LLM Fallback] purpose=%s requested=%s actual=%s",
                    resolved_intent.purpose.value,
                    model_name,
                    actual_model,
                )
            log_llm_response(
                LLMCallRecord(
                    model=actual_model,
                    duration_ms=duration_ms,
                    success=True,
                    prompt_tokens=usage.prompt_tokens,
                    completion_tokens=usage.completion_tokens,
                    total_tokens=usage.total_tokens,
                    is_fallback=is_fallback,
                    intent_purpose=resolved_intent.purpose.value,
                    intent_tier=tier_name,
                )
            )
            return LLMResponse(
                content=getattr(message, "content", "") or "",
                tool_calls=tool_calls,
                usage=usage,
            )
        except Exception as exc:
            duration_ms = (time.monotonic() - t0) * 1000
            log_llm_error(model_name, duration_ms, exc)
            return LLMResponse(content=f"LLM Error: {exc}")

    def _supports_cache_control(self, model_name: str) -> bool:
        lowered = self._resolved_provider_model(model_name).lower()
        return "claude" in lowered or "anthropic" in lowered

    def _default_tier_name(self) -> str:
        return self._default_tier.value

    def _resolved_provider_model(self, model_name: str) -> str:
        llm_kwargs = self._router.config_manager.get_llm_kwargs(model_name) or {}
        resolved = llm_kwargs.get("model")
        if isinstance(resolved, str) and resolved.strip():
            return resolved
        return model_name

    def _did_router_fallback(self, requested_model: str, actual_model: str) -> bool:
        if not actual_model or actual_model == requested_model:
            return False
        available = set(self._router.config_manager.get_available_model_names())
        return actual_model in available


def _parse_tool_args(args_str: Any) -> dict[str, Any]:
    if isinstance(args_str, dict):
        return args_str
    try:
        return json.loads(args_str) if args_str else {}
    except (json.JSONDecodeError, TypeError):
        return {"raw": args_str}


def _normalize_tool_schema(tool: dict[str, Any]) -> dict[str, Any]:
    """Normalize legacy bare function schemas to OpenAI tools format."""
    if not isinstance(tool, dict):
        return tool

    if "function" in tool:
        normalized = dict(tool)
        normalized["type"] = str(normalized.get("type") or "function")
        return normalized

    if {"name", "parameters"} & set(tool):
        return {
            "type": "function",
            "function": {
                "name": tool.get("name", ""),
                "description": tool.get("description", ""),
                "parameters": tool.get(
                    "parameters",
                    {"type": "object", "properties": {}, "required": []},
                ),
            },
        }

    return tool


def _resolve_config_default_tier(config_manager: Any) -> ModelTier:
    if config_manager is None or not hasattr(config_manager, "get_default_tier"):
        raise ValueError(
            "LiteLLMRouterAdapter requires an explicit default_tier or a config_manager "
            "with valid default_tier from litellm.json"
        )
    raw_tier = config_manager.get_default_tier()
    if not ModelTier.is_valid(raw_tier):
        raise ValueError(
            "LiteLLMRouterAdapter requires a valid default_tier from litellm.json"
        )
    return ModelTier(str(raw_tier).strip().lower())
