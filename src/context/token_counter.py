"""
Token counting - tiktoken preferred, character estimation fallback.

Pure functions with no side effects. Tiktoken is loaded once at module
import time; if unavailable, all functions degrade to character estimation.
"""
from __future__ import annotations

from typing import Any

_TIKTOKEN_AVAILABLE = False
_ENCODING = None


def _try_load_tiktoken() -> None:
    """Attempt to load tiktoken. Silently degrade on failure."""
    global _TIKTOKEN_AVAILABLE, _ENCODING
    try:
        import tiktoken
        _ENCODING = tiktoken.encoding_for_model("gpt-4o")
        _TIKTOKEN_AVAILABLE = True
    except Exception:
        _TIKTOKEN_AVAILABLE = False


_try_load_tiktoken()


def count_tokens(text: str) -> int:
    """
    Count tokens in text.

    Uses tiktoken when available (precise), falls back to chars / 3.5
    (conservative for CJK-heavy text).
    """
    if not text:
        return 0
    if _TIKTOKEN_AVAILABLE and _ENCODING is not None:
        return len(_ENCODING.encode(text))
    return max(1, int(len(text) / 3.5))


def count_message_tokens(message: dict[str, Any]) -> int:
    """
    Count tokens for a single message.

    Includes role overhead (~4 tokens) + content + tool_calls.
    """
    overhead = 4  # role + message structure overhead
    content = message.get("content", "")
    if isinstance(content, list):
        text = "".join(str(block) for block in content)
    else:
        text = str(content) if content else ""

    tokens = overhead + count_tokens(text)

    tool_calls = message.get("tool_calls", [])
    for tc in tool_calls:
        fn = tc.get("function", {})
        tokens += count_tokens(fn.get("name", ""))
        tokens += count_tokens(fn.get("arguments", ""))
        tokens += 4  # tool_call structure overhead

    return tokens


def count_messages_tokens(
    messages: tuple[dict[str, Any], ...] | list[dict[str, Any]],
) -> int:
    """Count total tokens for a message list."""
    return sum(count_message_tokens(m) for m in messages)


def estimate_tokens_from_usage(
    messages: tuple[dict[str, Any], ...],
    last_known_prompt_tokens: int,
    new_messages_since: int,
) -> int:
    """
    Estimate current token total based on last API response usage.prompt_tokens.

    Adds estimated tokens for new messages since the last known count.
    Similar to claude-code's tokenCountWithEstimation strategy.
    """
    if last_known_prompt_tokens <= 0:
        return count_messages_tokens(messages)

    if 0 < new_messages_since <= len(messages):
        new_tokens = sum(
            count_message_tokens(messages[i])
            for i in range(len(messages) - new_messages_since, len(messages))
        )
    else:
        new_tokens = 0

    return last_known_prompt_tokens + new_tokens
