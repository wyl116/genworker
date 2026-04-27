"""Shared security models for request-scoped tool execution."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class PolicyDecision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    NEEDS_APPROVAL = "needs_approval"


@dataclass(frozen=True)
class EnforcementConstraint:
    allowed_paths: frozenset[str] = field(default_factory=frozenset)
    blocked_paths: frozenset[str] = field(default_factory=frozenset)
    allowed_domains: frozenset[str] = field(default_factory=frozenset)
    blocked_domains: frozenset[str] = field(default_factory=frozenset)
    max_execution_time: float = 30.0


@dataclass(frozen=True)
class PolicyResult:
    decision: PolicyDecision
    reason: str = ""


@dataclass(frozen=True)
class AuditEntry:
    timestamp: str
    tenant_id: str
    worker_id: str
    tool_name: str
    policy_decision: str
    enforcement_result: str
    error_message: str = ""
    execution_time_ms: int = 0

