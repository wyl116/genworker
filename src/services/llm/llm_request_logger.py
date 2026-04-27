"""
LLM Request/Response Logger

Standalone logging module for LLM calls. Does NOT invoke any LLM API.
Responsible only for formatting and emitting structured log messages.

All log messages use the [LLM] prefix tag for easy searching.

Log levels:
- INFO:  Request/response summaries, stream completion, fallback chain
- DEBUG: Full message content (truncated), response content
- ERROR: Request failures
"""

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

from src.common.logger import get_logger

logger = get_logger()

# --- Truncation constants ---
MAX_CONTENT_LENGTH = 500    # Single message content
MAX_RESPONSE_LENGTH = 1000  # Response content


@dataclass(frozen=True)
class LLMCallRecord:
    """Immutable record of a single LLM call attempt."""

    model: str
    duration_ms: float
    success: bool
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    error: Optional[str] = None
    is_fallback: bool = False
    is_stream: bool = False
    intent_purpose: str = ""
    intent_tier: str = ""


def truncate_content(content: str, max_length: int = MAX_CONTENT_LENGTH) -> str:
    """
    Truncate content and append metadata when exceeding max_length.

    Args:
        content: Original content string.
        max_length: Maximum allowed length before truncation.

    Returns:
        Original content if within limit, otherwise truncated with
        ``...[truncated, total=N chars]`` suffix.
    """
    if not content:
        return ""
    if len(content) <= max_length:
        return content
    return f"{content[:max_length]}...[truncated, total={len(content)} chars]"


# ---------------------------------------------------------------------------
# Request logging
# ---------------------------------------------------------------------------

def log_llm_request(
    model: str,
    message_count: int,
    is_stream: bool,
    params: Optional[Dict[str, Any]] = None,
) -> None:
    """INFO-level request summary."""
    extra_parts: List[str] = []
    if params:
        for key in ("temperature", "max_tokens", "max_completion_tokens", "top_p"):
            if key in params:
                extra_parts.append(f"{key}={params[key]}")

    extra = f" | {', '.join(extra_parts)}" if extra_parts else ""
    logger.info(
        f"[LLM] Request | model={model} | messages={message_count} "
        f"| stream={is_stream}{extra}"
    )


def log_llm_request_messages(
    messages: Sequence[Dict[str, str]],
    model: str,
) -> None:
    """DEBUG-level full request messages (truncated per message)."""
    parts: List[str] = []
    for idx, msg in enumerate(messages):
        role = msg.get("role", "unknown")
        content = truncate_content(str(msg.get("content", "")), MAX_CONTENT_LENGTH)
        parts.append(f"[{idx}] {role}: {content}")

    logger.debug(
        f"[LLM] Request messages | model={model} | {' | '.join(parts)}"
    )


# ---------------------------------------------------------------------------
# Response logging
# ---------------------------------------------------------------------------

def log_llm_response(record: LLMCallRecord) -> None:
    """INFO-level response summary with model, duration, and token usage."""
    tokens_str = (
        f"tokens={{prompt={record.prompt_tokens}, "
        f"completion={record.completion_tokens}, "
        f"total={record.total_tokens}}}"
    )
    intent_str = ""
    if record.intent_purpose or record.intent_tier:
        intent_str = (
            f" | purpose={record.intent_purpose or 'unknown'}"
            f" | tier={record.intent_tier or 'unknown'}"
        )
    logger.info(
        f"[LLM] Response OK | model={record.model} "
        f"| duration_ms={record.duration_ms:.0f}{intent_str} | {tokens_str}"
    )


def log_llm_response_content(content: str, model: str) -> None:
    """DEBUG-level response content (truncated)."""
    truncated = truncate_content(content, MAX_RESPONSE_LENGTH)
    logger.debug(f"[LLM] Response content | model={model} | content={truncated}")


# ---------------------------------------------------------------------------
# Error logging
# ---------------------------------------------------------------------------

def log_llm_error(
    model: str,
    duration_ms: float,
    error: Exception,
    is_fallback: bool = False,
) -> None:
    """ERROR-level failure log."""
    label = "Fallback failed" if is_fallback else "Request failed"
    logger.error(
        f"[LLM] {label} | model={model} | duration_ms={duration_ms:.0f} "
        f"| error={error}"
    )


# ---------------------------------------------------------------------------
# Stream logging
# ---------------------------------------------------------------------------

def log_llm_stream_complete(
    model: str,
    duration_ms: float,
    chunk_count: int,
    total_chars: int,
) -> None:
    """INFO-level stream completion summary."""
    logger.info(
        f"[LLM] Stream complete | model={model} "
        f"| duration_ms={duration_ms:.0f} | chunks={chunk_count} "
        f"| chars={total_chars}"
    )


# ---------------------------------------------------------------------------
# Fallback chain logging
# ---------------------------------------------------------------------------

def log_llm_fallback_summary(
    original_model: str,
    attempts: List[LLMCallRecord],
    resolved_model: Optional[str] = None,
) -> None:
    """
    INFO/WARNING-level fallback chain summary.

    Args:
        original_model: The originally requested model.
        attempts: Ordered list of call records in the fallback chain.
        resolved_model: The model that ultimately succeeded, or None if all failed.
    """
    chain_parts: List[str] = []
    for rec in attempts:
        if rec.success:
            chain_parts.append(f"{rec.model}(OK:{rec.duration_ms:.0f}ms)")
        else:
            err_short = str(rec.error)[:80] if rec.error else "unknown"
            chain_parts.append(f"{rec.model}(FAIL:{rec.duration_ms:.0f}ms)")

    chain_str = " -> ".join(chain_parts)

    if resolved_model:
        logger.info(
            f"[LLM] Fallback resolved | original={original_model} "
            f"| chain=[{chain_str}]"
        )
    else:
        logger.warning(
            f"[LLM] Fallback exhausted | original={original_model} "
            f"| chain=[{chain_str}]"
        )
