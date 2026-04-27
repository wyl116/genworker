# edition: baseline
from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.services.llm.litellm_router import ChatCompletionRequest, ChatMessage, LiteLLMRouter


class _ConfigManager:
    def __init__(self, aliases=None, llm_kwargs=None, available=None, default_tier="standard"):
        self._aliases = aliases or {}
        self._llm_kwargs = llm_kwargs or {}
        self._available = available or []
        self._default_tier = default_tier

    def get_tier_model(self, tier: str):
        return self._aliases.get(tier)

    def get_available_model_names(self):
        return list(self._available)

    def get_llm_kwargs(self, model_name: str):
        return self._llm_kwargs.get(model_name)

    def get_default_tier(self):
        return self._default_tier


@pytest.mark.asyncio
async def test_chat_completion_defaults_to_standard_tier_model():
    router = object.__new__(LiteLLMRouter)
    router.config_manager = _ConfigManager(
        aliases={"standard": "tier-standard"},
        available=["legacy-model"],
    )
    captured = {}

    async def _acompletion(**kwargs):
        captured.update(kwargs)
        return {
            "id": "chatcmpl-1",
            "created": 1,
            "model": kwargs["model"],
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
        }

    router.router = SimpleNamespace(acompletion=_acompletion)

    response = await router.chat_completion(
        ChatCompletionRequest(messages=[ChatMessage(role="user", content="hi")])
    )

    assert captured["model"] == "tier-standard"
    assert response.model == "tier-standard"


@pytest.mark.asyncio
async def test_chat_completion_uses_provider_model_for_reasoning_detection():
    router = object.__new__(LiteLLMRouter)
    router.config_manager = _ConfigManager(
        llm_kwargs={"tier-reasoning": {"model": "azure/gpt-5.2"}},
        available=["tier-reasoning"],
    )
    captured = {}

    async def _acompletion(**kwargs):
        captured.update(kwargs)
        return {
            "id": "chatcmpl-2",
            "created": 1,
            "model": kwargs["model"],
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
        }

    router.router = SimpleNamespace(acompletion=_acompletion)

    await router.chat_completion(
        ChatCompletionRequest(
            messages=[ChatMessage(role="user", content="hi")],
            model="tier-reasoning",
            max_tokens=16,
            temperature=0,
        )
    )

    assert "max_completion_tokens" in captured
    assert captured["max_completion_tokens"] == 16
    assert "max_tokens" not in captured


@pytest.mark.asyncio
async def test_chat_completion_rejects_implicit_available_model_fallback():
    router = object.__new__(LiteLLMRouter)
    router.config_manager = _ConfigManager(
        aliases={},
        available=["legacy-model"],
    )
    router.router = SimpleNamespace()

    with pytest.raises(ValueError, match="no valid default_tier/model could be resolved"):
        await router.chat_completion(
            ChatCompletionRequest(messages=[ChatMessage(role="user", content="hi")])
        )
