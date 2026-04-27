# edition: baseline
from __future__ import annotations
from types import SimpleNamespace

import pytest

from src.services.llm.intent import LLMCallIntent, Purpose
from src.services.llm.model_tiers import ModelTier
from src.services.llm.router_adapter import LiteLLMRouterAdapter


class _ConfigManager:
    def __init__(
        self,
        aliases: dict[str, str] | None = None,
        llm_kwargs: dict[str, dict[str, str]] | None = None,
        default_tier: str = "standard",
    ) -> None:
        self._aliases = aliases or {}
        self._llm_kwargs = llm_kwargs or {}
        self._default_tier = default_tier

    def get_tier_model(self, tier: str) -> str | None:
        return self._aliases.get(tier)

    def get_available_model_names(self) -> list[str]:
        return ["legacy-model"]

    def get_llm_kwargs(self, model_name: str) -> dict[str, str] | None:
        return self._llm_kwargs.get(model_name)

    def get_default_tier(self) -> str:
        return self._default_tier


class _Router:
    def __init__(
        self,
        aliases: dict[str, str] | None = None,
        response_model: str = "tier-strong",
        llm_kwargs: dict[str, dict[str, str]] | None = None,
    ) -> None:
        self.config_manager = _ConfigManager(aliases, llm_kwargs)
        self.router = SimpleNamespace(acompletion=self._acompletion)
        self.kwargs = None
        self.response_model = response_model

    async def _acompletion(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(
            model=self.response_model,
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok", tool_calls=[]))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=2, total_tokens=3),
        )


@pytest.mark.asyncio
async def test_router_adapter_uses_default_intent_when_missing():
    router = _Router(aliases={"standard": "tier-standard"}, response_model="tier-standard")
    adapter = LiteLLMRouterAdapter(router)

    await adapter.invoke(messages=[{"role": "user", "content": "hello"}])

    assert router.kwargs["model"] == "tier-standard"
    assert router.kwargs["metadata"]["intent_purpose"] == "generate"
    assert router.kwargs["metadata"]["intent_tier"] == "standard"


@pytest.mark.asyncio
async def test_router_adapter_falls_back_to_default_tier_on_policy_failure(monkeypatch):
    class _BrokenPolicy:
        def choose(self, intent):
            raise RuntimeError("boom")

    warnings: list[str] = []
    router = _Router(
        aliases={"standard": "tier-standard"},
        response_model="tier-standard",
    )
    adapter = LiteLLMRouterAdapter(router, policy=_BrokenPolicy(), default_tier=ModelTier.STANDARD)
    monkeypatch.setattr(
        "src.services.llm.router_adapter.logger.warning",
        lambda message, *args: warnings.append(message % args),
    )

    await adapter.invoke(
        messages=[{"role": "user", "content": "hello"}],
        intent=LLMCallIntent(purpose=Purpose.CHAT_TURN, requires_tools=True),
    )

    assert router.kwargs["model"] == "tier-standard"
    assert router.kwargs["metadata"]["intent_tier"] == "standard"
    assert any("policy failed" in item for item in warnings)


@pytest.mark.asyncio
async def test_router_adapter_returns_error_when_tier_alias_missing(monkeypatch):
    errors: list[str] = []
    router = _Router(aliases={}, response_model="legacy-model")
    adapter = LiteLLMRouterAdapter(router)
    monkeypatch.setattr(
        "src.services.llm.router_adapter.logger.error",
        lambda message, *args: errors.append(message % args),
    )

    response = await adapter.invoke(
        messages=[{"role": "user", "content": "hello"}],
        intent=LLMCallIntent(purpose=Purpose.CHAT_TURN),
    )

    assert router.kwargs is None
    assert "No model configured for tier 'strong'" in response.content
    assert any("no model configured for tier=strong" in item for item in errors)


def test_router_adapter_rejects_invalid_router_default_tier():
    router = _Router(aliases={"standard": "tier-standard"})
    router.config_manager = _ConfigManager(
        aliases={"standard": "tier-standard"},
        default_tier="deep",
    )

    with pytest.raises(ValueError, match="valid default_tier"):
        LiteLLMRouterAdapter(router)


@pytest.mark.asyncio
async def test_router_adapter_logs_fallback_when_actual_model_differs(monkeypatch):
    infos: list[str] = []
    router = _Router(aliases={"strong": "tier-strong"}, response_model="legacy-model")
    adapter = LiteLLMRouterAdapter(router)
    monkeypatch.setattr(
        "src.services.llm.router_adapter.logger.info",
        lambda message, *args: infos.append(message % args),
    )

    await adapter.invoke(
        messages=[{"role": "user", "content": "hello"}],
        intent=LLMCallIntent(purpose=Purpose.CHAT_TURN),
    )

    assert any("[LLM Fallback]" in item for item in infos)


@pytest.mark.asyncio
async def test_router_adapter_does_not_mislabel_provider_model_as_fallback(monkeypatch):
    infos: list[str] = []
    router = _Router(aliases={"strong": "tier-strong"}, response_model="azure/gpt-5.2")
    adapter = LiteLLMRouterAdapter(router)
    monkeypatch.setattr(
        "src.services.llm.router_adapter.logger.info",
        lambda message, *args: infos.append(message % args),
    )

    await adapter.invoke(
        messages=[{"role": "user", "content": "hello"}],
        intent=LLMCallIntent(purpose=Purpose.CHAT_TURN),
    )

    assert not any("[LLM Fallback]" in item for item in infos)


@pytest.mark.asyncio
async def test_router_adapter_uses_underlying_provider_name_for_cache_control():
    router = _Router(
        aliases={"standard": "tier-standard"},
        response_model="tier-standard",
        llm_kwargs={"tier-standard": {"model": "anthropic/claude-3-7-sonnet"}},
    )
    adapter = LiteLLMRouterAdapter(router)

    await adapter.invoke(
        messages=[{"role": "user", "content": "hello"}],
        system_blocks=[{"type": "text", "text": "cached"}],
    )

    assert router.kwargs["system"] == [{"type": "text", "text": "cached"}]
