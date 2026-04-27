"""
Layer 2: History pruning by API round.

Groups messages into API rounds and discards the oldest rounds
atomically until token budget is met. Always preserves the system
message and first user message.
"""
from __future__ import annotations

from typing import Any

from src.context.models import CompactionResult, ContextWindowConfig, MessageGroupMetrics
from src.context.token_counter import count_message_tokens, count_messages_tokens


def prune_oldest_rounds(
    messages: tuple[dict[str, Any], ...],
    target_tokens: int,
    config: ContextWindowConfig,
) -> tuple[tuple[dict[str, Any], ...], CompactionResult]:
    """
    Layer 2: Selective pruning.

    Groups messages by API round. Preserves system (messages[0]) and
    first user message. Discards oldest rounds atomically until
    total tokens <= target_tokens.

    Returns (pruned_messages, CompactionResult).
    """
    tokens_before = count_messages_tokens(messages)

    if tokens_before <= target_tokens:
        return messages, CompactionResult(
            layer="history_prune",
            tokens_before=tokens_before,
            tokens_after=tokens_before,
            success=True,
        )

    preserved, groups = group_by_api_round(messages)
    if not groups:
        return messages, CompactionResult(
            layer="history_prune",
            tokens_before=tokens_before,
            tokens_after=tokens_before,
            success=True,
        )

    preserved_tokens = count_messages_tokens(preserved)
    group_tokens = tuple(count_messages_tokens(g) for g in groups)
    total = preserved_tokens + sum(group_tokens)

    groups_to_keep = len(groups)
    for i in range(len(groups)):
        if total <= target_tokens:
            break
        if groups_to_keep <= 1:
            break
        total -= group_tokens[i]
        groups_to_keep = len(groups) - (i + 1)

    kept_groups = groups[len(groups) - groups_to_keep:]
    pruned_count = len(groups) - groups_to_keep

    result_messages: list[dict[str, Any]] = list(preserved)
    for group in kept_groups:
        result_messages.extend(group)

    new_messages = tuple(result_messages)
    tokens_after = count_messages_tokens(new_messages)

    return new_messages, CompactionResult(
        layer="history_prune",
        tokens_before=tokens_before,
        tokens_after=tokens_after,
        segments_affected=("conversation_history",) if pruned_count > 0 else (),
        success=True,
    )


def group_by_api_round(
    messages: tuple[dict[str, Any], ...],
) -> tuple[tuple[dict[str, Any], ...], tuple[tuple[dict[str, Any], ...], ...]]:
    """
    Group messages into API rounds.

    Skips the system message and first user message (always preserved).
    Each group contains one user message + assistant response + tool messages.

    Returns (preserved_messages, groups) where preserved is always kept.
    """
    if len(messages) == 0:
        return (), ()

    preserved: list[dict[str, Any]] = []
    rest_start = 0

    # Preserve system message
    if messages[0].get("role") == "system":
        preserved.append(messages[0])
        rest_start = 1

    # Preserve first user message
    if rest_start < len(messages) and messages[rest_start].get("role") == "user":
        preserved.append(messages[rest_start])
        rest_start += 1

    remaining = messages[rest_start:]
    if not remaining:
        return tuple(preserved), ()

    groups: list[tuple[dict[str, Any], ...]] = []
    current_group: list[dict[str, Any]] = []

    for msg in remaining:
        role = msg.get("role", "")
        if role == "user" and current_group:
            groups.append(tuple(current_group))
            current_group = [msg]
        else:
            current_group.append(msg)

    if current_group:
        groups.append(tuple(current_group))

    return tuple(preserved), tuple(groups)


def compute_group_metrics(
    groups: tuple[tuple[dict[str, Any], ...], ...],
    current_round: int,
) -> tuple[MessageGroupMetrics, ...]:
    """Compute token count, message count, and round age for each group."""
    metrics: list[MessageGroupMetrics] = []
    for i, group in enumerate(groups):
        token_count = sum(count_message_tokens(m) for m in group)
        has_tool_calls = any(
            bool(m.get("tool_calls")) for m in group
        )
        metrics.append(MessageGroupMetrics(
            group_index=i,
            message_count=len(group),
            token_count=token_count,
            has_tool_calls=has_tool_calls,
            round_age=current_round - i,
        ))
    return tuple(metrics)
