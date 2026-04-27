"""
Bash sandbox execution tool.

Provides a sandboxed bash execution handler that can be registered
as an MCP Tool. Includes security validation, timeout control,
output truncation, and concurrency limiting.
"""
import asyncio
import os
from typing import Any, Optional

from src.common.logger import get_logger
from src.common.settings import get_settings

from .registry import builtin_tool
from .bash_security import BashSecurityError, validate_command, validate_working_dir
from .bash_sandbox import ProcessSandbox, SandboxConfig, SandboxResult
from ..mcp.tool import Tool
from ..mcp.types import ConcurrencyLevel, MCPCategory, RiskLevel, ToolType

logger = get_logger()

# Default configuration
DEFAULT_TIMEOUT = 30
DEFAULT_MAX_OUTPUT_SIZE = 10240  # 10KB
DEFAULT_MAX_CONCURRENT = 5
DEFAULT_SANDBOX_DIR = "/tmp/genworker-bash-sandbox"

# Concurrency semaphore (module-level, lazy init)
_semaphore: Optional[asyncio.Semaphore] = None
_process_sandbox: ProcessSandbox | None = None


def _get_semaphore(max_concurrent: int = DEFAULT_MAX_CONCURRENT) -> asyncio.Semaphore:
    """Get or create the concurrency-limiting semaphore."""
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(max_concurrent)
    return _semaphore


async def bash_execute(
    command: str,
    working_dir: str = "",
    timeout: int = DEFAULT_TIMEOUT,
) -> str:
    """
    Execute a shell command in a sandboxed environment.

    Steps:
    1. Security validation (whitelist + dangerous pattern detection)
    2. Working directory resolution (sandbox isolation)
    3. Subprocess execution with timeout
    4. Output truncation and JSON formatting

    Returns:
        JSON string with exit_code, stdout, stderr, truncated fields.
    """
    sandbox_dir = DEFAULT_SANDBOX_DIR
    # Security validation
    try:
        validate_command(command)
        resolved_dir = validate_working_dir(working_dir, sandbox_dir)
    except BashSecurityError as e:
        logger.warning(f"[BASH] Security blocked: {e}")
        return _build_error_result(str(e))

    os.makedirs(resolved_dir, exist_ok=True)

    semaphore = _get_semaphore()
    async with semaphore:
        try:
            sandbox = _get_process_sandbox(timeout=timeout)
            result = await sandbox.execute(command, resolved_dir)

            logger.info(
                f"[BASH] Executed: {command[:80]}... | "
                f"exit_code={result.exit_code} | "
                f"stdout_len={len(result.stdout)}"
            )

            return result.to_json()

        except Exception as e:
            logger.error(f"[BASH] Execution error: {e}", exc_info=True)
            return _build_error_result(f"Execution error: {e}")


def _build_error_result(stderr: str) -> str:
    """Build a JSON error result string."""
    return SandboxResult(
        exit_code=-1,
        stdout="",
        stderr=stderr,
        truncated=False,
    ).to_json()


def _get_process_sandbox(timeout: int) -> ProcessSandbox:
    """Get or create the process sandbox with current settings."""
    global _process_sandbox
    settings = get_settings()
    config = SandboxConfig(
        mode=str(getattr(settings, "bash_sandbox_mode", "subprocess")),
        timeout_seconds=max(timeout, 1),
        max_output_bytes=DEFAULT_MAX_OUTPUT_SIZE,
        max_concurrent=DEFAULT_MAX_CONCURRENT,
        writable_paths=(DEFAULT_SANDBOX_DIR,),
    )
    if _process_sandbox is None or _process_sandbox._config != config:  # type: ignore[attr-defined]
        _process_sandbox = ProcessSandbox(config)
    return _process_sandbox


@builtin_tool()
def create_bash_tool() -> Tool:
    """Create the bash execution Tool instance."""
    return Tool(
        name="bash_execute",
        description=(
            "Execute a shell command in a sandboxed environment. "
            "Supported: ls, cat, grep, awk, sed, python3, curl, etc. "
            "Dangerous operations (rm -rf, sudo) are blocked."
        ),
        handler=bash_execute,
        parameters={
            "command": {
                "type": "string",
                "description": "Shell command to execute",
            },
            "working_dir": {
                "type": "string",
                "description": "Working directory (relative to sandbox, default: sandbox root)",
            },
            "timeout": {
                "type": "integer",
                "description": "Timeout in seconds (default: 30, max: 300)",
            },
        },
        required_params=("command",),
        tool_type=ToolType.EXECUTE,
        category=MCPCategory.GLOBAL,
        risk_level=RiskLevel.MEDIUM,
        concurrency=ConcurrencyLevel.EXCLUSIVE,
        tags=frozenset({"bash", "shell", "execute", "sandbox"}),
    )
