"""
Layer 3: LLM-based history summarization.

Sends conversation history to an LLM for compression into a concise
summary. Includes a circuit breaker that skips after 3 consecutive failures.
"""
from __future__ import annotations

from typing import Any

from src.context.models import CompactionResult, ContextWindowConfig
from src.context.token_counter import count_messages_tokens
from src.services.llm.intent import LLMCallIntent, Purpose

SUMMARIZATION_PROMPT = """\
Please compress the following conversation history into a concise summary.

Preserve key information:
- User's original intent and all requests
- Important technical decisions and discoveries
- Core findings from tool call results
- Errors encountered and solutions
- Pending tasks

Discard redundant information:
- Intermediate exploration processes
- Repeated tool outputs
- Expired temporary data

Output format: structured plain text, no more than {max_summary_tokens} tokens.

Conversation history:
{conversation}
"""

_CONSECUTIVE_FAILURE_LIMIT = 3


async def summarize_history(
    messages: tuple[dict[str, Any], ...],
    llm_client: Any,
    config: ContextWindowConfig,
    consecutive_failures: int = 0,
) -> tuple[tuple[dict[str, Any], ...], CompactionResult]:
    """
    Layer 3: LLM summary compression.

    Preserves system prompt and first user message. Sends remaining
    history to LLM for summarization. The summary replaces the
    original history as an assistant message.

    Circuit breaker: after _CONSECUTIVE_FAILURE_LIMIT consecutive
    failures, skip summarization and return original messages.

    Returns (new_messages, CompactionResult).
    """
    tokens_before = count_messages_tokens(messages)

    if consecutive_failures >= _CONSECUTIVE_FAILURE_LIMIT:
        return messages, CompactionResult(
            layer="history_summarize",
            tokens_before=tokens_before,
            tokens_after=tokens_before,
            success=False,
            error=f"Circuit breaker: {consecutive_failures} consecutive failures",
        )

    # Preserve system + first user
    preserved: list[dict[str, Any]] = []
    rest_start = 0

    if messages and messages[0].get("role") == "system":
        preserved.append(messages[0])
        rest_start = 1

    if rest_start < len(messages) and messages[rest_start].get("role") == "user":
        preserved.append(messages[rest_start])
        rest_start += 1

    history_to_summarize = messages[rest_start:]
    if not history_to_summarize:
        return messages, CompactionResult(
            layer="history_summarize",
            tokens_before=tokens_before,
            tokens_after=tokens_before,
            success=True,
        )

    formatted = _format_messages_for_summary(history_to_summarize)
    max_summary_tokens = max(500, tokens_before // 4)

    prompt = SUMMARIZATION_PROMPT.format(
        max_summary_tokens=max_summary_tokens,
        conversation=formatted,
    )

    try:
        response = await llm_client.invoke(
            messages=[{"role": "user", "content": prompt}],
            intent=LLMCallIntent(
                purpose=Purpose.SUMMARIZE,
                requires_long_context=True,
            ),
        )
        summary_text = response.content
        if not summary_text:
            return messages, CompactionResult(
                layer="history_summarize",
                tokens_before=tokens_before,
                tokens_after=tokens_before,
                success=False,
                error="LLM returned empty summary",
            )

        summary_msg = {
            "role": "assistant",
            "content": f"[Conversation history summary]\n{summary_text}",
        }

        new_messages = tuple([*preserved, summary_msg])
        tokens_after = count_messages_tokens(new_messages)

        return new_messages, CompactionResult(
            layer="history_summarize",
            tokens_before=tokens_before,
            tokens_after=tokens_after,
            summary_generated=summary_text,
            segments_affected=("conversation_history",),
            success=True,
        )
    except Exception as exc:
        return messages, CompactionResult(
            layer="history_summarize",
            tokens_before=tokens_before,
            tokens_after=tokens_before,
            success=False,
            error=str(exc),
        )


def _format_messages_for_summary(
    messages: tuple[dict[str, Any], ...] | list[dict[str, Any]],
) -> str:
    """Format messages into text suitable for LLM summarization."""
    lines: list[str] = []
    for msg in messages:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        if isinstance(content, list):
            content = " ".join(str(block) for block in content)
        elif content is None:
            content = ""

        tool_calls = msg.get("tool_calls", [])
        if tool_calls:
            tc_parts = []
            for tc in tool_calls:
                fn = tc.get("function", {})
                tc_parts.append(f"  call: {fn.get('name', '')}({fn.get('arguments', '')})")
            lines.append(f"[{role}] {content}")
            lines.extend(tc_parts)
        else:
            lines.append(f"[{role}] {content}")

    return "\n".join(lines)
