"""
LiteLLM Router Implementation

Provides unified interface for 100+ LLM models using LiteLLM.
Includes automatic failover, circuit breaker, load balancing, and cost tracking.
"""

import asyncio
import logging
import time
from typing import Dict, List, Optional, Any, AsyncIterator, Union
from datetime import datetime

from pydantic import BaseModel
from src.common.logger import get_logger

try:
    from litellm import Router, completion, acompletion
    from litellm.types.utils import ModelResponse, Choices, Message, Usage
    import litellm
    LITELLM_AVAILABLE = True
    litellm.drop_params = True
except ImportError:
    LITELLM_AVAILABLE = False
    Router = None
    ModelResponse = dict
    Choices = dict
    Message = dict
    Usage = dict

from .litellm_config import LiteLLMConfigManager
from .litellm_param_adapter import is_reasoning_model, adapt_completion_params
from .llm_request_logger import (
    LLMCallRecord,
    log_llm_request,
    log_llm_request_messages,
    log_llm_response,
    log_llm_response_content,
    log_llm_error,
    log_llm_stream_complete,
    log_llm_fallback_summary,
)

logger = get_logger()


class ChatMessage(BaseModel):
    """Chat message format."""
    role: str
    content: str


class ChatCompletionRequest(BaseModel):
    """Request model for chat completion."""
    messages: List[ChatMessage]
    model: Optional[str] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    max_completion_tokens: Optional[int] = None
    stream: bool = False
    top_p: Optional[float] = None
    frequency_penalty: Optional[float] = None
    presence_penalty: Optional[float] = None


class ChatCompletionResponse(BaseModel):
    """Response model for chat completion."""
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: List[Dict[str, Any]]
    usage: Optional[Dict[str, int]] = None

    @classmethod
    def from_litellm_response(cls, response: Any) -> "ChatCompletionResponse":
        """Convert LiteLLM response to our response format."""
        if isinstance(response, dict):
            return cls(**response)

        return cls(
            id=getattr(response, 'id', 'chatcmpl-unknown'),
            created=getattr(response, 'created', int(datetime.now().timestamp())),
            model=getattr(response, 'model', 'unknown'),
            choices=[
                {
                    "index": choice.get("index", i),
                    "message": {
                        "role": choice.get("message", {}).get("role", "assistant"),
                        "content": choice.get("message", {}).get("content", ""),
                    },
                    "finish_reason": choice.get("finish_reason", "stop"),
                }
                for i, choice in enumerate(getattr(response, 'choices', []))
            ],
            usage={
                "prompt_tokens": getattr(
                    response.usage, 'prompt_tokens', 0
                ) if hasattr(response, 'usage') else 0,
                "completion_tokens": getattr(
                    response.usage, 'completion_tokens', 0
                ) if hasattr(response, 'usage') else 0,
                "total_tokens": getattr(
                    response.usage, 'total_tokens', 0
                ) if hasattr(response, 'usage') else 0,
            } if hasattr(response, 'usage') else None,
        )


class HealthCheckResult(BaseModel):
    """Health check result for a model."""
    model: str
    healthy: bool
    latency_ms: Optional[float] = None
    error: Optional[str] = None
    timestamp: datetime = None

    def __init__(self, **data):
        if 'timestamp' not in data:
            data['timestamp'] = datetime.now()
        super().__init__(**data)


class LiteLLMRouter:
    """
    LiteLLM Router wrapper providing unified interface for multiple LLM models.

    Features:
    - Unified interface for 100+ models
    - Automatic failover with circuit breaker
    - Load balancing across model instances
    - Health checks
    """

    def __init__(
        self,
        config_manager: LiteLLMConfigManager,
        enable_fallback: bool = True,
        enable_caching: bool = False,
    ):
        if not LITELLM_AVAILABLE:
            raise ImportError(
                "LiteLLM is not installed. Install with: pip install litellm"
            )

        self.config_manager = config_manager
        self.enable_fallback = enable_fallback
        self.enable_caching = enable_caching

        router_config = config_manager.to_litellm_router_kwargs()

        try:
            self.router = Router(**router_config)
            logger.info(
                f"LiteLLM Router initialized with "
                f"{len(router_config['model_list'])} models"
            )
        except Exception as e:
            logger.error(f"Failed to initialize LiteLLM Router: {e}")
            raise

        self._health_status: Dict[str, HealthCheckResult] = {}

    async def chat_completion(
        self,
        request: ChatCompletionRequest,
    ) -> Union[ChatCompletionResponse, AsyncIterator[str]]:
        """Perform chat completion."""
        messages = [
            {"role": msg.role, "content": msg.content}
            for msg in request.messages
        ]

        kwargs = {"messages": messages, "stream": request.stream}

        model = request.model
        if not model:
            model = self._resolve_default_model()
            if not model:
                raise ValueError(
                    "No model specified and no valid default_tier/model could be "
                    "resolved from litellm.json"
                )

        kwargs["model"] = model
        if request.temperature is not None:
            kwargs["temperature"] = request.temperature

        is_reasoning = is_reasoning_model(self._resolve_provider_model_name(model))
        if request.max_completion_tokens is not None:
            kwargs["max_completion_tokens"] = request.max_completion_tokens
        elif request.max_tokens is not None:
            if is_reasoning:
                kwargs["max_completion_tokens"] = request.max_tokens
            else:
                kwargs["max_tokens"] = request.max_tokens

        if request.top_p is not None:
            kwargs["top_p"] = request.top_p
        if request.frequency_penalty is not None:
            kwargs["frequency_penalty"] = request.frequency_penalty
        if request.presence_penalty is not None:
            kwargs["presence_penalty"] = request.presence_penalty

        log_llm_request(model, len(messages), request.stream, kwargs)
        if logger.isEnabledFor(logging.DEBUG):
            log_llm_request_messages(messages, model)

        t0 = time.monotonic()

        try:
            if request.stream:
                return self._stream_completion_with_logging(kwargs, model, t0)
            else:
                response = await self.router.acompletion(**kwargs)
                duration_ms = (time.monotonic() - t0) * 1000
                result = ChatCompletionResponse.from_litellm_response(response)

                usage = result.usage or {}
                record = LLMCallRecord(
                    model=result.model,
                    duration_ms=duration_ms,
                    success=True,
                    prompt_tokens=usage.get("prompt_tokens", 0),
                    completion_tokens=usage.get("completion_tokens", 0),
                    total_tokens=usage.get("total_tokens", 0),
                    is_stream=False,
                )
                log_llm_response(record)
                return result
        except Exception as e:
            duration_ms = (time.monotonic() - t0) * 1000
            log_llm_error(model, duration_ms, e)
            raise

    async def _stream_completion_with_logging(
        self,
        kwargs: Dict[str, Any],
        model: str,
        t0: float,
    ) -> AsyncIterator[str]:
        """Stream chat completion with logging wrapper."""
        chunk_count = 0
        total_chars = 0

        try:
            response = await self.router.acompletion(**kwargs)
            async for chunk in response:
                if hasattr(chunk, 'choices') and chunk.choices:
                    delta = chunk.choices[0].delta
                    if hasattr(delta, 'content') and delta.content:
                        chunk_count += 1
                        total_chars += len(delta.content)
                        yield delta.content
        except Exception as e:
            duration_ms = (time.monotonic() - t0) * 1000
            log_llm_error(model, duration_ms, e)
            raise
        finally:
            duration_ms = (time.monotonic() - t0) * 1000
            log_llm_stream_complete(model, duration_ms, chunk_count, total_chars)

    def get_available_models(self) -> List[str]:
        """Get list of available model names."""
        return self.config_manager.get_available_model_names()

    def _resolve_default_model(self) -> Optional[str]:
        """Resolve the default model group strictly from tier config."""
        try:
            return self.config_manager.get_tier_model(
                self.config_manager.get_default_tier()
            )
        except Exception:
            return None

    def _resolve_provider_model_name(self, model_name: str) -> str:
        """Map a model group back to the underlying provider model when possible."""
        llm_kwargs = self.config_manager.get_llm_kwargs(model_name) or {}
        resolved = llm_kwargs.get("model")
        if isinstance(resolved, str) and resolved.strip():
            return resolved
        return model_name
