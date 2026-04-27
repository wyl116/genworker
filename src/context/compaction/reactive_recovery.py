"""
Layer 4: Reactive recovery from prompt_too_long errors.

Aggressively compresses context to reach reactive_target_ratio (default 0.70)
of the effective window. Only triggered when the LLM API returns a
prompt_too_long or HTTP 413 error.
"""
from __future__ import annotations

from typing import Any

from src.context.compaction.history_pruner import prune_oldest_rounds
from src.context.compaction.history_summarizer import summarize_history
from src.context.compaction.tool_trimmer import trim_old_tool_results
from src.context.models import CompactionResult, ContextWindowConfig
from src.context.token_counter import count_messages_tokens


async def recover_from_prompt_too_long(
    messages: tuple[dict[str, Any], ...],
    llm_client: Any,
    config: ContextWindowConfig,
) -> tuple[tuple[dict[str, Any], ...], CompactionResult]:
    """
    Layer 4: Reactive recovery.

    Target: reduce tokens to effective_window * reactive_target_ratio.
    Executes in order:
    1. Strip oversized single messages (>10K tokens)
    2. Layer 1: trim ALL old tool results (age=0)
    3. Layer 2: aggressive pruning to target
    4. Layer 3: LLM summarization (if client available)

    Returns (compressed_messages, CompactionResult).
    """
    tokens_before = count_messages_tokens(messages)
    target = int(config.effective_window * config.reactive_target_ratio)
    current = messages

    # Step 1: Strip oversized content
    current = _strip_oversized_content(current)

    # Step 2: Layer 1 - trim ALL tool results (use age_rounds=0)
    from dataclasses import replace
    aggressive_config = replace(config, tool_trim_age_rounds=0)
    current_round = _estimate_current_round(current)
    current, _ = trim_old_tool_results(current, current_round, aggressive_config)

    current_tokens = count_messages_tokens(current)
    if current_tokens <= target:
        return current, _make_result(tokens_before, current_tokens, True)

    # Step 3: Layer 2 - aggressive pruning
    current, _ = prune_oldest_rounds(current, target, config)

    current_tokens = count_messages_tokens(current)
    if current_tokens <= target:
        return current, _make_result(tokens_before, current_tokens, True)

    # Step 4: Layer 3 - summarization
    if llm_client is not None:
        current, _ = await summarize_history(current, llm_client, config)

    tokens_after = count_messages_tokens(current)
    success = tokens_after <= target

    return current, _make_result(tokens_before, tokens_after, success)


def _strip_oversized_content(
    messages: tuple[dict[str, Any], ...],
    max_single_message_tokens: int = 10_000,
) -> tuple[dict[str, Any], ...]:
    """
    Truncate single messages with content exceeding max_single_message_tokens.

    Appends "[content truncated]" to truncated messages.
    """
    from src.context.token_counter import count_tokens

    result: list[dict[str, Any]] = []
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str) and content:
            token_count = count_tokens(content)
            if token_count > max_single_message_tokens:
                target_chars = int(max_single_message_tokens * 3.5)
                truncated = content[:target_chars] + "\n[content truncated]"
                result.append({**msg, "content": truncated})
                continue
        result.append(msg)
    return tuple(result)


def _estimate_current_round(messages: tuple[dict[str, Any], ...]) -> int:
    """Estimate the current round number from assistant message count."""
    return sum(1 for m in messages if m.get("role") == "assistant")


def _make_result(
    tokens_before: int,
    tokens_after: int,
    success: bool,
) -> CompactionResult:
    """Build a CompactionResult for reactive recovery."""
    return CompactionResult(
        layer="reactive_recovery",
        tokens_before=tokens_before,
        tokens_after=tokens_after,
        segments_affected=("conversation_history", "tool_results"),
        success=success,
        error="" if success else "Could not reach target utilization",
    )
