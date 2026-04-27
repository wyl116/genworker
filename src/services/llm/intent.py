"""Intent metadata carried by LLM call sites."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Purpose(str, Enum):
    """Stable business purposes for LLM invocations."""

    CHAT_TURN = "chat_turn"
    PLAN = "plan"
    REFLECT = "reflect"
    STRATEGIZE = "strategize"
    EXTRACT = "extract"
    SUMMARIZE = "summarize"
    CLASSIFY = "classify"
    GENERATE = "generate"
    TOOL_CALL = "tool_call"


@dataclass(frozen=True)
class LLMCallIntent:
    """Semantic routing intent declared by call sites."""

    purpose: Purpose
    requires_reasoning: bool = False
    requires_long_context: bool = False
    requires_tools: bool = False
    latency_sensitive: bool = False
    quality_critical: bool = False


DEFAULT_INTENT = LLMCallIntent(purpose=Purpose.GENERATE)

