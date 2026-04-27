"""
Context window data models.

All models use @dataclass(frozen=True) for immutability.
Mutations use dataclasses.replace() to return new instances.
"""
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SegmentPriority:
    """
    Segment priority definitions. Lower values = higher priority.
    When total tokens exceed the window, compression starts from
    segments with the highest priority value (lowest priority).
    """
    IDENTITY: int = 0
    PRINCIPLES: int = 1
    CONSTRAINTS: int = 2
    DIRECTIVES: int = 10
    TASK_CONTEXT: int = 15
    TOOL_DEFINITIONS: int = 20
    CONTACT_CONTEXT: int = 25
    LEARNED_RULES: int = 30
    GOAL_CONTEXT: int = 35
    DUTY_CONTEXT: int = 40
    EPISODIC_MEMORY: int = 50
    CONVERSATION_HISTORY: int = 60
    TOOL_RESULTS: int = 70


# Module-level singleton for convenient access
SEGMENT_PRIORITY = SegmentPriority()


@dataclass(frozen=True)
class ContextSegment:
    """
    An independent segment within the context window.
    Each segment has a name, content, token count, priority, and budget cap.
    """
    name: str
    content: str
    token_count: int
    priority: int
    max_tokens: int = 0
    compressible: bool = True
    metadata: tuple[tuple[str, Any], ...] = ()


@dataclass(frozen=True)
class ContextWindowConfig:
    """
    Context window configuration. Can be overridden in PERSONA.md.
    """
    model_context_window: int = 128_000
    output_reserved_tokens: int = 4_000
    safety_buffer_tokens: int = 2_000

    # Per-segment token budget caps (0 = unlimited, dynamically allocated)
    identity_max_tokens: int = 500
    principles_max_tokens: int = 500
    constraints_max_tokens: int = 300
    directives_max_tokens: int = 1_000
    contact_context_max_tokens: int = 1_500
    learned_rules_max_tokens: int = 1_500
    episodic_memory_max_tokens: int = 3_000
    duty_context_max_tokens: int = 2_000
    goal_context_max_tokens: int = 2_000
    task_context_max_tokens: int = 1_000
    conversation_history_max_tokens: int = 0
    tool_results_max_tokens: int = 0

    # Layer 1: tool result trimming
    tool_trim_age_rounds: int = 3
    tool_trim_placeholder: str = "[tool result cleared]"

    # Layer 2: history pruning threshold (used / effective_window)
    history_prune_threshold: float = 0.85

    # Layer 3: summarization threshold
    summarize_threshold: float = 0.92

    # Layer 4: reactive recovery target ratio
    reactive_target_ratio: float = 0.70

    @property
    def effective_window(self) -> int:
        """Usable input token window size."""
        return (
            self.model_context_window
            - self.output_reserved_tokens
            - self.safety_buffer_tokens
        )


@dataclass(frozen=True)
class CompactionResult:
    """
    Result of a compaction operation. Records before/after state and layer.
    """
    layer: str  # "tool_trim" | "history_prune" | "history_summarize" | "reactive_recovery"
    tokens_before: int
    tokens_after: int
    segments_affected: tuple[str, ...] = ()
    summary_generated: str = ""
    success: bool = True
    error: str = ""


@dataclass(frozen=True)
class AssembledContext:
    """
    Fully assembled context ready for Engine consumption.
    """
    system_prompt: str
    messages: tuple[dict[str, Any], ...]
    segments: tuple[ContextSegment, ...]
    total_tokens: int
    compactions_applied: tuple[CompactionResult, ...]
    budget_utilization: float
    stable_prefix: str = ""
    dynamic_context: str = ""
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class MessageGroupMetrics:
    """
    Metrics for a message group, used for history pruning decisions.
    """
    group_index: int
    message_count: int
    token_count: int
    has_tool_calls: bool
    round_age: int
