"""Audit logger for scoped tool execution."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone

from src.common.logger import get_logger
from src.tools.security.models import AuditEntry

logger = get_logger()


class AuditLogger:
    """Log tool execution decisions in a structured way."""

    def log(
        self,
        *,
        tenant_id: str,
        worker_id: str,
        tool_name: str,
        policy_decision: str,
        enforcement_result: str,
        error_message: str = "",
        execution_time_ms: int = 0,
    ) -> None:
        entry = AuditEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            tenant_id=tenant_id,
            worker_id=worker_id,
            tool_name=tool_name,
            policy_decision=policy_decision,
            enforcement_result=enforcement_result,
            error_message=error_message,
            execution_time_ms=execution_time_ms,
        )
        logger.info("[ToolAudit] %s", asdict(entry))

