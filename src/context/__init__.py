"""
Context window management - token budgeting, progressive compaction, and assembly.

Provides segment-level token budget allocation, 4-layer progressive compaction
pipeline, and context assembly for Engine consumption.
"""

from src.context.models import (
    AssembledContext,
    CompactionResult,
    ContextSegment,
    ContextWindowConfig,
    MessageGroupMetrics,
    SegmentPriority,
)

__all__ = [
    "AssembledContext",
    "CompactionResult",
    "ContextSegment",
    "ContextWindowConfig",
    "MessageGroupMetrics",
    "SegmentPriority",
]
