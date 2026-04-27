"""
Integration adapters - bridges context management with Engine/Worker layers.

Provides high-level interfaces for Engine consumption and
prompt_too_long error recovery.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Any

from src.context.assembler import assemble_context
from src.context.compaction.reactive_recovery import recover_from_prompt_too_long
from src.context.models import AssembledContext, ContextWindowConfig
from src.context.prefix_cache import StablePrefixCache
from src.context.token_counter import count_messages_tokens
from src.engine.state import GraphState, WorkerContext


def context_config_from_worker(
    worker_context: WorkerContext,
    model_context_window: int = 128_000,
) -> ContextWindowConfig:
    """
    Build a ContextWindowConfig from WorkerContext.

    Currently uses default values with the specified model window size.
    Future: reads overrides from PERSONA.md context_window configuration.
    """
    return ContextWindowConfig(model_context_window=model_context_window)


async def build_managed_context(
    worker_context: WorkerContext,
    messages: tuple[dict[str, Any], ...],
    config: ContextWindowConfig,
    llm_client: Any | None = None,
    current_round: int = 0,
    episodic_context: str = "",
    duty_context: str = "",
    goal_context: str = "",
    contact_context: str = "",
    memory_orchestrator: Any | None = None,
    memory_flush_callback: Any | None = None,
    worker_dir: str = "",
    prefix_cache: StablePrefixCache | None = None,
) -> AssembledContext:
    """
    High-level interface for Engine layer.

    Extracts fields from WorkerContext + Phase 7 context additions,
    delegates to assemble_context(), and returns AssembledContext.

    Engines use AssembledContext.system_prompt to replace original
    system prompt and AssembledContext.messages to replace message list.
    """
    return await assemble_context(
        identity=worker_context.identity,
        principles=worker_context.principles,
        constraints=worker_context.constraints,
        directives=worker_context.directives,
        contact_context=contact_context,
        learned_rules=worker_context.learned_rules,
        episodic_context=episodic_context or worker_context.historical_context,
        duty_context=duty_context,
        goal_context=goal_context,
        task_context=worker_context.task_context,
        messages=messages,
        config=config,
        llm_client=llm_client,
        current_round=current_round,
        memory_orchestrator=memory_orchestrator,
        memory_flush_callback=memory_flush_callback,
        worker_dir=worker_dir,
        worker_id=worker_context.worker_id,
        skill_id=worker_context.skill_id,
        prefix_cache=prefix_cache,
    )


async def handle_prompt_too_long(
    state: GraphState,
    llm_client: Any,
    config: ContextWindowConfig,
) -> GraphState:
    """
    Called by ReactEngine after catching a prompt_too_long error.

    Executes Layer 4 reactive recovery on the messages, then returns
    a new GraphState with compressed messages. Caller retries LLM call.
    """
    compressed_messages, result = await recover_from_prompt_too_long(
        messages=state.messages,
        llm_client=llm_client,
        config=config,
    )

    return GraphState(
        messages=compressed_messages,
        worker_context=state.worker_context,
        budget=state.budget,
        thread_id=state.thread_id,
    )
