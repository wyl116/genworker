"""External-only compatibility exports for historical bootstrap imports."""
from __future__ import annotations

import warnings

from src.runtime.bootstrap_builders import (
    _RouterAdapter,
    build_unavailable_llm_client as _build_unavailable_llm_client,
    build_enhanced_planning_executor as _build_enhanced_planning_executor,
    build_planning_stack as _build_planning_stack,
    build_subagent_executor as _build_subagent_executor,
    build_tool_executor as _build_tool_executor,
    build_tool_pipeline as _build_tool_pipeline,
    parse_tool_args as _parse_tool_args,
)
from src.runtime.task_hooks import (
    build_error_feedback_handler as _build_error_feedback_handler,
    build_memory_flush_callback as _build_memory_flush_callback,
    build_post_run_handler as _build_post_run_handler,
)


def _build_direct_llm_client(*args, **kwargs):
    warnings.warn(
        "_build_direct_llm_client is deprecated; use _build_unavailable_llm_client",
        DeprecationWarning,
        stacklevel=2,
    )
    return _build_unavailable_llm_client(*args, **kwargs)

__all__ = [
    "_RouterAdapter",
    "_build_direct_llm_client",
    "_build_unavailable_llm_client",
    "_build_enhanced_planning_executor",
    "_build_error_feedback_handler",
    "_build_memory_flush_callback",
    "_build_planning_stack",
    "_build_post_run_handler",
    "_build_subagent_executor",
    "_build_tool_executor",
    "_build_tool_pipeline",
    "_parse_tool_args",
]
