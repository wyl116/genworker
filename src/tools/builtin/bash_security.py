"""Bash security - whitelist, path isolation, and pipeline hook."""
import os
import re
from typing import Any

from src.common.logger import get_logger
from src.tools.hooks import HookAction, HookResult

logger = get_logger()
DEFAULT_PIPELINE_SANDBOX_DIR = "/tmp/genworker-bash-sandbox"

# Allowed command prefixes (first word of command)
ALLOWED_COMMANDS: frozenset[str] = frozenset({
    # File operations
    "ls", "cat", "head", "tail", "wc", "find", "stat", "file", "mkdir", "cp",
    # Text processing
    "grep", "awk", "sed", "sort", "uniq", "cut", "tr", "jq", "csvtool",
    # Data processing
    "python3", "python",
    # System info
    "date", "df", "du", "uname", "whoami", "env",
    # Network diagnostics
    "ping", "curl", "wget",
    # Calculation
    "bc", "expr",
    # Echo for output
    "echo",
})

# Dangerous patterns (compiled regex)
BLOCKED_PATTERNS: tuple[re.Pattern, ...] = tuple(
    re.compile(p) for p in (
        r"rm\s+-rf",           # Recursive forced delete
        r">(>?)\s*/",          # Redirect to root path
        r"\bsudo\b",          # Privilege escalation
        r"\bchmod\b.*777",    # Insecure permissions
        r"\bmkfs\b",          # Filesystem format
        r"\bdd\b.*of=/dev",   # Device write
        r";\s*rm\b",          # Command injection delete
        r"\|.*rm\b",          # Pipe delete
        r"\$\(.*\)",          # Command substitution
    )
)

# Blocked filesystem paths
_RAW_BLOCKED_PATHS: tuple[str, ...] = ("/etc", "/var", "/root", "/proc", "/sys")
BLOCKED_PATHS: tuple[str, ...] = tuple(
    os.path.realpath(p) for p in _RAW_BLOCKED_PATHS
)


class BashSecurityError(Exception):
    """Raised when a command fails security validation."""
    pass


def validate_command(command: str, max_length: int = 2048) -> None:
    """
    Validate command against security rules.

    Raises BashSecurityError if command is not allowed.
    """
    if not command or not command.strip():
        raise BashSecurityError("Command must not be empty")

    if len(command) > max_length:
        raise BashSecurityError(
            f"Command length exceeds limit ({len(command)} > {max_length})"
        )

    # Check command whitelist
    first_cmd = command.strip().split()[0].split("/")[-1]
    if first_cmd not in ALLOWED_COMMANDS:
        raise BashSecurityError(
            f"Command '{first_cmd}' is not in the whitelist. "
            f"Allowed: {', '.join(sorted(ALLOWED_COMMANDS))}"
        )

    # Check blocked patterns
    for pattern in BLOCKED_PATTERNS:
        if pattern.search(command):
            raise BashSecurityError(
                "Command contains a dangerous pattern and was blocked"
            )


def validate_working_dir(path: str, sandbox_dir: str) -> str:
    """
    Validate and resolve working directory within sandbox.

    Returns resolved path. Raises BashSecurityError if path escapes sandbox.
    """
    if not path:
        return sandbox_dir

    resolved = os.path.realpath(os.path.join(sandbox_dir, path))

    if not resolved.startswith(os.path.realpath(sandbox_dir)):
        raise BashSecurityError(f"Working directory '{path}' escapes sandbox")

    for blocked in BLOCKED_PATHS:
        if resolved.startswith(blocked):
            raise BashSecurityError(f"Access to path denied: {blocked}")

    return resolved


def is_blocked_pattern(command: str) -> bool:
    """Check if command matches any blocked pattern."""
    return any(pattern.search(command) for pattern in BLOCKED_PATTERNS)


class BashSecurityHook:
    """Pipeline hook applying bash command validation before execution."""

    async def pre_execute(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
    ) -> HookResult:
        if tool_name != "bash_execute":
            return HookResult(action=HookAction.ALLOW)

        command = str(tool_input.get("command", ""))
        working_dir = str(tool_input.get("working_dir", ""))
        try:
            validate_command(command)
            if working_dir:
                validate_working_dir(
                    working_dir,
                    DEFAULT_PIPELINE_SANDBOX_DIR,
                )
        except BashSecurityError as exc:
            return HookResult(action=HookAction.DENY, message=str(exc))
        return HookResult(action=HookAction.ALLOW)

    async def post_execute(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        result: Any,
    ) -> None:
        return None
