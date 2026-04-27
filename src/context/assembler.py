"""
Context assembler - builds segments, allocates budgets, runs compaction,
and assembles the final context for Engine consumption.

Main entry point is assemble_context() which orchestrates the full pipeline.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable
from pathlib import Path

from src.context.budget_allocator import allocate_budgets, trim_segment_to_budget
from src.context.compaction.identity_anchor import anchor_to_context, build_identity_anchor
from src.context.compaction.history_pruner import prune_oldest_rounds
from src.context.compaction.structured_summarizer import (
    load_previous_summary,
    save_summary,
    summarize_structured,
    summary_to_message,
)
from src.context.compaction.tool_trimmer import (
    trim_for_compression,
    trim_old_tool_results,
)
from src.context.models import (
    AssembledContext,
    CompactionResult,
    ContextSegment,
    ContextWindowConfig,
    SegmentPriority,
)
from src.context.prefix_cache import StablePrefixCache
from src.context.token_counter import count_messages_tokens, count_tokens

_PRIORITY = SegmentPriority()


def build_segments(
    identity: str,
    principles: str,
    constraints: str,
    directives: str,
    contact_context: str,
    learned_rules: str,
    episodic_context: str,
    duty_context: str,
    goal_context: str,
    task_context: str,
    config: ContextWindowConfig,
) -> tuple[ContextSegment, ...]:
    """
    Wrap each prompt field into a ContextSegment with token count and priority.

    Empty-content segments are created with token_count=0 but still included.
    """
    segment_specs = (
        ("identity", identity, _PRIORITY.IDENTITY,
         config.identity_max_tokens, False),
        ("principles", principles, _PRIORITY.PRINCIPLES,
         config.principles_max_tokens, False),
        ("constraints", constraints, _PRIORITY.CONSTRAINTS,
         config.constraints_max_tokens, False),
        ("directives", directives, _PRIORITY.DIRECTIVES,
         config.directives_max_tokens, False),
        ("task_context", task_context, _PRIORITY.TASK_CONTEXT,
         config.task_context_max_tokens, False),
        ("contact_context", contact_context, _PRIORITY.CONTACT_CONTEXT,
         config.contact_context_max_tokens, True),
        ("learned_rules", learned_rules, _PRIORITY.LEARNED_RULES,
         config.learned_rules_max_tokens, True),
        ("goal_context", goal_context, _PRIORITY.GOAL_CONTEXT,
         config.goal_context_max_tokens, True),
        ("duty_context", duty_context, _PRIORITY.DUTY_CONTEXT,
         config.duty_context_max_tokens, True),
        ("episodic_memory", episodic_context, _PRIORITY.EPISODIC_MEMORY,
         config.episodic_memory_max_tokens, True),
    )

    segments: list[ContextSegment] = []
    for name, content, priority, max_tok, compressible in segment_specs:
        token_count = count_tokens(content) if content else 0
        segments.append(ContextSegment(
            name=name,
            content=content,
            token_count=token_count,
            priority=priority,
            max_tokens=max_tok,
            compressible=compressible,
        ))

    return tuple(segments)


def assemble_system_prompt(segments: tuple[ContextSegment, ...]) -> str:
    """
    Concatenate segments into a system prompt string.

    Skips segments with token_count=0 (empty content).
    Segments are joined with double newlines.
    """
    parts: list[str] = []
    for seg in segments:
        if seg.token_count > 0 and seg.content:
            parts.append(seg.content)
    return "\n\n".join(parts)


async def assemble_context(
    identity: str,
    principles: str,
    constraints: str,
    directives: str,
    contact_context: str,
    learned_rules: str,
    episodic_context: str,
    duty_context: str,
    goal_context: str,
    task_context: str,
    messages: tuple[dict[str, Any], ...],
    config: ContextWindowConfig,
    llm_client: Any | None = None,
    current_round: int = 0,
    memory_orchestrator: Any | None = None,
    memory_flush_callback: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    worker_dir: str = "",
    worker_id: str = "",
    skill_id: str = "",
    prefix_cache: StablePrefixCache | None = None,
) -> AssembledContext:
    """
    Main assembly entry point.

    Pipeline:
    1. build_segments() - wrap fields into ContextSegments
    2. allocate_budgets() - distribute token budgets
    3. trim_segment_to_budget() - truncate over-budget segments
    4. Compute utilization = total / effective_window
    5. If >= history_prune_threshold: Layer 1 + Layer 2
    6. If >= summarize_threshold and llm_client: Layer 3
    7. assemble_system_prompt() - join segments
    8. Return AssembledContext
    """
    # Step 1: Build segments
    segments = build_segments(
        identity, principles, constraints, directives,
        contact_context, learned_rules, episodic_context, duty_context,
        goal_context, task_context, config,
    )

    # Step 2: Allocate budgets
    segments = allocate_budgets(segments, config)

    # Step 3: Trim over-budget segments
    segments = tuple(trim_segment_to_budget(s) for s in segments)

    # Step 4: Compute utilization
    segment_tokens = sum(s.token_count for s in segments)
    message_tokens = count_messages_tokens(messages)
    total_tokens = segment_tokens + message_tokens
    effective = config.effective_window
    utilization = total_tokens / effective if effective > 0 else 0.0

    compactions: list[CompactionResult] = []
    warnings: list[str] = []
    current_messages = messages

    # Step 5: Layer 1 + Layer 2 if >= prune threshold
    if utilization >= config.history_prune_threshold:
        if memory_orchestrator is not None:
            try:
                await memory_orchestrator.on_pre_compress(tuple(current_messages))
            except Exception:
                pass
        current_messages, l1_result = trim_old_tool_results(
            current_messages, current_round, config,
        )
        compactions.append(l1_result)

        message_tokens = count_messages_tokens(current_messages)
        total_tokens = segment_tokens + message_tokens
        utilization = total_tokens / effective if effective > 0 else 0.0

        if utilization >= config.history_prune_threshold:
            target = int(effective * config.history_prune_threshold)
            current_messages, l2_result = prune_oldest_rounds(
                current_messages, target - segment_tokens, config,
            )
            compactions.append(l2_result)

            message_tokens = count_messages_tokens(current_messages)
            total_tokens = segment_tokens + message_tokens
            utilization = total_tokens / effective if effective > 0 else 0.0

        if memory_flush_callback is not None and llm_client is not None:
            try:
                from src.context.compaction.memory_flush import (
                    flush_memory_before_compaction,
                )
                flush_result = await flush_memory_before_compaction(
                    messages=current_messages,
                    llm_client=llm_client,
                )
                if flush_result:
                    payload = dict(flush_result)
                    payload["worker_dir"] = worker_dir
                    await memory_flush_callback(payload)
            except Exception:
                pass

        current_messages = trim_for_compression(current_messages)
        message_tokens = count_messages_tokens(current_messages)
        total_tokens = segment_tokens + message_tokens
        utilization = total_tokens / effective if effective > 0 else 0.0

    # Step 6: Layer 3 if >= summarize threshold
    if utilization >= config.summarize_threshold and llm_client is not None:
        preserved: list[dict[str, Any]] = []
        rest_start = 0
        if current_messages and current_messages[0].get("role") == "system":
            preserved.append(current_messages[0])
            rest_start = 1
        if rest_start < len(current_messages) and current_messages[rest_start].get("role") == "user":
            preserved.append(current_messages[rest_start])
            rest_start += 1
        history_to_summarize = tuple(current_messages[rest_start:])
        previous_summary = (
            load_previous_summary(Path(worker_dir))
            if worker_dir else None
        )
        summary = await summarize_structured(
            history_to_summarize,
            previous_summary,
            llm_client,
        )
        if worker_dir and summary.raw_text:
            save_summary(Path(worker_dir), summary)
        current_messages = tuple([*preserved, summary_to_message(summary)])
        message_tokens = count_messages_tokens(current_messages)
        l3_result = CompactionResult(
            layer="history_summarize",
            tokens_before=total_tokens - segment_tokens + count_messages_tokens(history_to_summarize),
            tokens_after=message_tokens,
            summary_generated=summary.raw_text,
            segments_affected=("conversation_history",),
            success=bool(summary.raw_text),
        )
        compactions.append(l3_result)

        message_tokens = count_messages_tokens(current_messages)
        total_tokens = segment_tokens + message_tokens
        utilization = total_tokens / effective if effective > 0 else 0.0

    structured_summary_context = ""
    identity_anchor_context = ""
    if compactions:
        latest_summary = load_previous_summary(Path(worker_dir)) if worker_dir else None
        if latest_summary is not None and latest_summary.raw_text:
            structured_summary_context = f"[Structured Summary]\n{latest_summary.raw_text}"
        anchor = build_identity_anchor(
            identity=identity,
            principles=principles,
            constraints=constraints,
        )
        identity_anchor_context = anchor_to_context(anchor)

    stable_segments = tuple(
        seg for seg in segments
        if seg.name in {"identity", "principles", "constraints", "directives"}
    )
    dynamic_segments = tuple(
        seg for seg in segments
        if seg.name not in {"identity", "principles", "constraints", "directives"}
    )
    dynamic_parts = [assemble_system_prompt(dynamic_segments)]
    if structured_summary_context:
        dynamic_parts.append(structured_summary_context)
    if identity_anchor_context:
        dynamic_parts.insert(0, identity_anchor_context)

    stable_prefix = assemble_system_prompt(stable_segments)
    if prefix_cache is not None:
        stable_prefix = prefix_cache.get_or_build(
            worker_id=worker_id,
            skill_id=skill_id,
            identity=identity,
            principles=principles,
            constraints=constraints,
            directives=directives,
            token_counter=count_tokens,
        ).text

    dynamic_context = "\n\n".join(part for part in dynamic_parts if part)
    system_prompt = "\n\n".join(
        part for part in (stable_prefix, dynamic_context) if part
    )

    # Step 8: Build result
    return AssembledContext(
        system_prompt=system_prompt,
        messages=current_messages,
        segments=segments,
        total_tokens=total_tokens,
        compactions_applied=tuple(compactions),
        budget_utilization=utilization,
        stable_prefix=stable_prefix,
        dynamic_context=dynamic_context,
        warnings=tuple(warnings),
    )
