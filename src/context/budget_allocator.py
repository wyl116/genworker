"""
Budget allocation - distributes token budgets across context segments.

Pure functions with no side effects. Allocation strategy:
1. Fixed segments (max_tokens > 0): use configured cap
2. Elastic segments (max_tokens = 0): split remaining space equally
3. Overflow: select low-priority compressible segments for compression
"""
from __future__ import annotations

from dataclasses import replace

from src.context.models import ContextSegment, ContextWindowConfig


def allocate_budgets(
    segments: tuple[ContextSegment, ...],
    config: ContextWindowConfig,
) -> tuple[ContextSegment, ...]:
    """
    Allocate token budgets to each segment.

    Strategy:
    1. Fixed-budget segments (max_tokens > 0) keep their configured cap.
    2. Elastic segments (max_tokens = 0) split the remaining space equally.
    3. No segment gets a budget smaller than its current token_count
       if total fits within the window.

    Returns new tuple of ContextSegment with max_tokens set.
    """
    effective = config.effective_window

    fixed_total = sum(s.max_tokens for s in segments if s.max_tokens > 0)
    elastic_segments = tuple(s for s in segments if s.max_tokens == 0)
    elastic_count = len(elastic_segments)

    remaining = max(0, effective - fixed_total)
    per_elastic = remaining // elastic_count if elastic_count > 0 else 0

    result: list[ContextSegment] = []
    for seg in segments:
        if seg.max_tokens > 0:
            result.append(seg)
        else:
            result.append(replace(seg, max_tokens=per_elastic))

    return tuple(result)


def trim_segment_to_budget(segment: ContextSegment) -> ContextSegment:
    """
    Trim segment content to fit within its max_tokens budget.

    Strategy: keep the tail (newest content), discard the head (oldest).
    Non-compressible segments (compressible=False) are returned unchanged.
    Segments within budget are returned unchanged.

    Returns a new ContextSegment with token_count <= max_tokens.
    """
    if not segment.compressible:
        return segment
    if segment.max_tokens <= 0:
        return segment
    if segment.token_count <= segment.max_tokens:
        return segment

    content = segment.content
    target_chars = int(segment.max_tokens * 3.5)
    if len(content) <= target_chars:
        return segment

    trimmed = content[-target_chars:]
    # Re-count tokens for the trimmed content
    from src.context.token_counter import count_tokens

    new_count = count_tokens(trimmed)
    return replace(segment, content=trimmed, token_count=new_count)


def compute_overflow(
    segments: tuple[ContextSegment, ...],
    effective_window: int,
) -> int:
    """
    Compute total token overflow beyond the effective window.

    Returns > 0 when compression is needed.
    """
    total = sum(s.token_count for s in segments)
    return max(0, total - effective_window)


def select_segments_to_compress(
    segments: tuple[ContextSegment, ...],
    overflow: int,
) -> tuple[str, ...]:
    """
    Select segment names for compression by priority (lowest priority first).

    Starts from the highest priority value (lowest priority) among
    compressible segments, accumulating releasable tokens until
    overflow is covered.
    """
    if overflow <= 0:
        return ()

    compressible = sorted(
        (s for s in segments if s.compressible and s.token_count > 0),
        key=lambda s: s.priority,
        reverse=True,
    )

    selected: list[str] = []
    accumulated = 0
    for seg in compressible:
        selected.append(seg.name)
        accumulated += seg.token_count
        if accumulated >= overflow:
            break

    return tuple(selected)
