"""Configuration helpers for the execute_code tool."""
from __future__ import annotations

from dataclasses import dataclass

from src.common.settings import get_settings

from .bash_sandbox import ProcessSandbox, SandboxConfig

DEFAULT_CODE_TIMEOUT_SECONDS = 300
DEFAULT_CODE_MAX_TIMEOUT_SECONDS = 600
DEFAULT_CODE_MAX_TOOL_CALLS = 50
DEFAULT_INLINE_SCRIPT_SIZE_LIMIT = 8 * 1024
DEFAULT_CODE_OUTPUT_BYTES = 50 * 1024
DEFAULT_CODE_CAPTURE_BYTES = 128 * 1024


@dataclass(frozen=True)
class CodeExecutionLimits:
    """Global execution limits applied to code execution paths."""

    default_timeout_seconds: int = DEFAULT_CODE_TIMEOUT_SECONDS
    max_timeout_seconds: int = DEFAULT_CODE_MAX_TIMEOUT_SECONDS
    max_tool_calls: int = DEFAULT_CODE_MAX_TOOL_CALLS
    inline_script_size_limit_bytes: int = DEFAULT_INLINE_SCRIPT_SIZE_LIMIT
    output_bytes: int = DEFAULT_CODE_OUTPUT_BYTES
    capture_bytes: int = DEFAULT_CODE_CAPTURE_BYTES


def get_code_execution_limits() -> CodeExecutionLimits:
    """Load effective limits from settings with safe fallbacks."""
    settings = get_settings()
    default_timeout = max(
        int(getattr(settings, "code_exec_timeout_seconds", DEFAULT_CODE_TIMEOUT_SECONDS)),
        1,
    )
    max_timeout = max(
        int(getattr(settings, "code_exec_max_timeout_seconds", DEFAULT_CODE_MAX_TIMEOUT_SECONDS)),
        default_timeout,
    )
    return CodeExecutionLimits(
        default_timeout_seconds=default_timeout,
        max_timeout_seconds=max_timeout,
        max_tool_calls=max(
            int(getattr(settings, "code_exec_max_tool_calls", DEFAULT_CODE_MAX_TOOL_CALLS)),
            1,
        ),
        inline_script_size_limit_bytes=max(
            int(
                getattr(
                    settings,
                    "code_exec_inline_size_limit_bytes",
                    DEFAULT_INLINE_SCRIPT_SIZE_LIMIT,
                )
            ),
            1,
        ),
    )


def build_code_sandbox(timeout_seconds: int) -> ProcessSandbox:
    """Create the dedicated process sandbox used by execute_code."""
    settings = get_settings()
    config = SandboxConfig(
        mode=str(getattr(settings, "bash_sandbox_mode", "subprocess")),
        timeout_seconds=max(timeout_seconds, 1),
        max_output_bytes=DEFAULT_CODE_CAPTURE_BYTES,
        max_concurrent=3,
        memory_limit_mb=512,
    )
    return ProcessSandbox(config)
