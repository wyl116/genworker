"""Runtime enforcement fences for scoped tool execution."""
from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlparse

from src.tools.builtin.bash_security import BashSecurityError, validate_working_dir
from src.tools.builtin.workspace_sandbox import (
    WorkspaceSandboxError,
    resolve_workspace_path,
)


def normalize_workspace_path(raw_path: str) -> str:
    """Normalize a relative workspace path for conflict detection."""
    cleaned = raw_path.strip().replace("\\", "/")
    if not cleaned:
        return ""
    return os.path.normpath(f"/{cleaned}").lstrip("/")


@dataclass(frozen=True)
class ResourceFence:
    """Validate filesystem access against request-scoped constraints."""

    workspace_root: str = ""

    def check(self, ctx) -> tuple[bool, str]:
        if ctx.tool is None:
            return False, f"Tool '{ctx.tool_name}' is unavailable"

        constraint = getattr(ctx, "constraint", None)
        if constraint is None:
            return True, ""

        tool_name = ctx.tool_name
        tool_input = ctx.tool_input
        if tool_name in {"file_read", "file_write", "file_edit", "glob_search", "grep_search"}:
            raw_path = str(tool_input.get("file_path") or tool_input.get("path") or "").strip()
            if not raw_path:
                return True, ""
            try:
                resolved = resolve_workspace_path(raw_path, self.workspace_root)
            except WorkspaceSandboxError as exc:
                return False, str(exc)
            return _check_path_constraint(resolved, constraint)

        if tool_name == "bash_execute":
            working_dir = str(tool_input.get("working_dir", "")).strip()
            if not working_dir:
                return True, ""
            try:
                validate_working_dir(working_dir, "/tmp/genworker-bash-sandbox")
            except BashSecurityError as exc:
                return False, str(exc)
        return True, ""


@dataclass(frozen=True)
class NetworkFence:
    """Validate URL-based tools against domain constraints."""

    def check(self, ctx) -> tuple[bool, str]:
        constraint = getattr(ctx, "constraint", None)
        if constraint is None:
            return True, ""
        url = str(ctx.tool_input.get("url", "")).strip()
        if not url:
            return True, ""
        parsed = urlparse(url)
        host = parsed.netloc.lower()
        if not host:
            return False, f"Invalid URL for tool '{ctx.tool_name}'"

        if constraint.blocked_domains and any(host == item or host.endswith(f".{item}") for item in constraint.blocked_domains):
            return False, f"Domain '{host}' is blocked for tool '{ctx.tool_name}'"
        if constraint.allowed_domains and not any(
            host == item or host.endswith(f".{item}") for item in constraint.allowed_domains
        ):
            return False, f"Domain '{host}' is outside the allowed scope"
        return True, ""


def _check_path_constraint(path: str, constraint) -> tuple[bool, str]:
    normalized = os.path.realpath(path)
    if constraint.blocked_paths and any(
        normalized == blocked or normalized.startswith(f"{blocked}{os.sep}")
        for blocked in constraint.blocked_paths
    ):
        return False, f"Path '{path}' is blocked for this run"
    if constraint.allowed_paths and not any(
        normalized == allowed or normalized.startswith(f"{allowed}{os.sep}")
        for allowed in constraint.allowed_paths
    ):
        return False, f"Path '{path}' is outside the allowed scope"
    return True, ""

