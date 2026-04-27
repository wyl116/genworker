"""
Layer 1: Non-destructive tool result trimming.

Replaces content of old tool-role messages with a placeholder,
preserving message structure and tool_call_id references.
"""
from __future__ import annotations

from typing import Any

from src.context.models import CompactionResult, ContextWindowConfig
from src.context.token_counter import count_messages_tokens


def trim_old_tool_results(
    messages: tuple[dict[str, Any], ...],
    current_round: int,
    config: ContextWindowConfig,
) -> tuple[tuple[dict[str, Any], ...], CompactionResult]:
    """
    Layer 1: Non-destructive trimming.

    For tool-role messages older than config.tool_trim_age_rounds,
    replace content with config.tool_trim_placeholder while preserving
    the message structure and tool_call_id.

    Returns (new_messages, CompactionResult).
    """
    tokens_before = count_messages_tokens(messages)
    boundaries = _identify_round_boundaries(messages)

    cutoff_round = current_round - config.tool_trim_age_rounds
    trimmed_count = 0
    result: list[dict[str, Any]] = []

    for idx, msg in enumerate(messages):
        round_idx = _message_round_index(idx, boundaries)
        if (
            msg.get("role") == "tool"
            and round_idx >= 0
            and round_idx < cutoff_round
            and msg.get("content") != config.tool_trim_placeholder
        ):
            new_msg = {**msg, "content": config.tool_trim_placeholder}
            result.append(new_msg)
            trimmed_count += 1
        else:
            result.append(msg)

    new_messages = tuple(result)
    tokens_after = count_messages_tokens(new_messages)

    return new_messages, CompactionResult(
        layer="tool_trim",
        tokens_before=tokens_before,
        tokens_after=tokens_after,
        segments_affected=("tool_results",) if trimmed_count > 0 else (),
        success=True,
    )


def trim_for_compression(
    messages: tuple[dict[str, Any], ...],
    char_threshold: int = 200,
) -> tuple[dict[str, Any], ...]:
    """Aggressively trim long tool outputs before structured summarization."""
    result: list[dict[str, Any]] = []
    for msg in messages:
        content = msg.get("content", "")
        if msg.get("role") != "tool" or not isinstance(content, str):
            result.append(msg)
            continue
        if len(content) <= char_threshold:
            result.append(msg)
            continue
        tool_name = msg.get("name") or msg.get("tool_name") or "tool"
        placeholder = f"[tool result: {tool_name}, {len(content)} chars cleared]"
        result.append({**msg, "content": placeholder})
    return tuple(result)


def _identify_round_boundaries(
    messages: tuple[dict[str, Any], ...],
) -> tuple[tuple[int, int], ...]:
    """
    Identify API round boundaries in messages.

    A round = one assistant response + subsequent tool messages.
    Returns (start_index, end_index) tuples where end_index is exclusive.
    """
    boundaries: list[tuple[int, int]] = []
    i = 0
    while i < len(messages):
        if messages[i].get("role") == "assistant":
            start = i
            i += 1
            while i < len(messages) and messages[i].get("role") == "tool":
                i += 1
            boundaries.append((start, i))
        else:
            i += 1
    return tuple(boundaries)


def _message_round_index(
    msg_index: int,
    boundaries: tuple[tuple[int, int], ...],
) -> int:
    """Return the round index for a message, or -1 if not in any round."""
    for round_idx, (start, end) in enumerate(boundaries):
        if start <= msg_index < end:
            return round_idx
    return -1
