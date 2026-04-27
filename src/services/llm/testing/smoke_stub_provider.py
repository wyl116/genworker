"""Offline smoke-profile LLM stub."""
from __future__ import annotations

import hashlib
from typing import Any

from src.engine.protocols import LLMResponse


class SmokeStubProvider:
    """Return deterministic assistant text for smoke verification."""

    async def invoke(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        system_blocks: list[dict[str, Any]] | None = None,
        intent=None,
    ) -> LLMResponse:
        prompt = _serialize_prompt(
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
            system_blocks=system_blocks,
        )
        digest = hashlib.sha1(prompt.encode("utf-8")).hexdigest()[:8]
        return LLMResponse(content=f"smoke-ok {digest}")


def _serialize_prompt(
    *,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    tool_choice: str | dict[str, Any] | None,
    system_blocks: list[dict[str, Any]] | None,
) -> str:
    parts: list[str] = []
    for message in messages or []:
        role = str(message.get("role", "")).strip()
        content = str(message.get("content", "")).strip()
        parts.append(f"{role}:{content}")
    if tools:
        parts.append(f"tools={len(tools)}")
    if tool_choice:
        parts.append(f"tool_choice={tool_choice}")
    if system_blocks:
        parts.append(f"system={len(system_blocks)}")
    return "\n".join(parts)
