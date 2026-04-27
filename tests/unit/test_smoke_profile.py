# edition: baseline
from __future__ import annotations

from types import SimpleNamespace

from src.runtime.api_wiring import build_llm_client
from src.services.llm.testing import SmokeStubProvider


async def _invoke(client) -> str:
    response = await client.invoke(messages=[{"role": "user", "content": "ping"}])
    return response.content


def test_build_llm_client_returns_smoke_stub_when_enabled() -> None:
    client = build_llm_client(SimpleNamespace(
        settings=SimpleNamespace(community_smoke_profile=True),
        get_state=lambda _key: None,
    ))
    assert isinstance(client, SmokeStubProvider)


async def test_smoke_stub_provider_returns_smoke_ok_prefix() -> None:
    content = await _invoke(SmokeStubProvider())
    assert content.startswith("smoke-ok")
