# edition: baseline
from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.services.llm.intent import LLMCallIntent, Purpose
from src.services.llm.model_tiers import ModelTier
from src.services.llm.router_adapter import LiteLLMRouterAdapter


class _DummyRouter:
    def __init__(self) -> None:
        self.config_manager = SimpleNamespace(
            get_tier_model=lambda tier: "primary-model" if tier == "strong" else None,
            get_available_model_names=lambda: ["primary-model"],
        )
        self.router = SimpleNamespace(acompletion=self._acompletion)

    async def _acompletion(self, **kwargs):
        assert kwargs["model"] == "primary-model"
        assert kwargs["tools"][0]["function"]["name"] == "lookup"
        assert kwargs["tools"][0]["type"] == "function"
        assert kwargs["tool_choice"] == "required"
        assert kwargs["metadata"]["intent_tier"] == "strong"
        return SimpleNamespace(
            model="primary-model",
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content="done",
                        tool_calls=[
                            SimpleNamespace(
                                id="call-1",
                                function=SimpleNamespace(
                                    name="lookup",
                                    arguments='{"q":"abc"}',
                                ),
                            )
                        ],
                    )
                )
            ],
            usage=SimpleNamespace(
                prompt_tokens=10,
                completion_tokens=5,
                total_tokens=15,
            ),
        )


@pytest.mark.asyncio
async def test_router_adapter_maps_response_to_llm_protocol():
    adapter = LiteLLMRouterAdapter(_DummyRouter(), default_tier=ModelTier.STANDARD)

    response = await adapter.invoke(
        messages=[{"role": "user", "content": "hello"}],
        tools=[{"type": "function", "function": {"name": "lookup"}}],
        tool_choice="required",
        intent=LLMCallIntent(
            purpose=Purpose.CHAT_TURN,
            requires_tools=True,
        ),
    )

    assert response.content == "done"
    assert len(response.tool_calls) == 1
    assert response.tool_calls[0].tool_name == "lookup"
    assert response.tool_calls[0].tool_input == {"q": "abc"}
    assert response.usage.total_tokens == 15


@pytest.mark.asyncio
async def test_router_adapter_normalizes_legacy_bare_tool_schema():
    class _LegacyToolRouter(_DummyRouter):
        async def _acompletion(self, **kwargs):
            assert kwargs["tools"][0]["type"] == "function"
            assert kwargs["tools"][0]["function"]["name"] == "lookup"
            return await super()._acompletion(**kwargs)

    adapter = LiteLLMRouterAdapter(_LegacyToolRouter(), default_tier=ModelTier.STANDARD)

    response = await adapter.invoke(
        messages=[{"role": "user", "content": "hello"}],
        tools=[{"name": "lookup", "description": "Lookup", "parameters": {"type": "object", "properties": {}}}],
        tool_choice="required",
        intent=LLMCallIntent(
            purpose=Purpose.CHAT_TURN,
            requires_tools=True,
        ),
    )

    assert response.content == "done"
