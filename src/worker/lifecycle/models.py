"""Lifecycle data models."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4


def now_iso() -> str:
    """Return the current UTC timestamp."""
    return datetime.now(timezone.utc).isoformat()


def add_days_iso(timestamp: str, days: int) -> str:
    """Return an ISO timestamp days after the given ISO timestamp."""
    base = parse_iso(timestamp) or datetime.now(timezone.utc)
    return (base + timedelta(days=days)).isoformat()


def parse_iso(value: str) -> datetime | None:
    """Parse an ISO timestamp, tolerating a trailing Z."""
    raw = str(value or "").strip()
    if not raw:
        return None
    normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class SuggestionRecord:
    """Pending or resolved lifecycle suggestion."""

    suggestion_id: str
    type: str
    source_entity_type: str
    source_entity_id: str
    title: str
    reason: str
    evidence: tuple[str, ...] = ()
    confidence: float = 0.0
    candidate_payload: str = ""
    status: str = "pending"
    created_at: str = field(default_factory=now_iso)
    claim_token: str = ""
    claimed_at: str = ""
    approval_stage: str = ""
    approval_summary: str = ""
    approval_artifact_ref: str = ""
    approval_applied_at: str = ""
    expires_at: str = ""
    resolved_at: str = ""
    resolved_by: str = ""
    resolution_note: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["evidence"] = list(self.evidence)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SuggestionRecord":
        created_at = str(data.get("created_at", "") or now_iso())
        expires_at = str(data.get("expires_at", "") or add_days_iso(created_at, 30))
        return cls(
            suggestion_id=str(data.get("suggestion_id", "") or uuid4().hex),
            type=str(data.get("type", "")),
            source_entity_type=str(data.get("source_entity_type", "")),
            source_entity_id=str(data.get("source_entity_id", "")),
            title=str(data.get("title", "")),
            reason=str(data.get("reason", "")),
            evidence=tuple(str(item) for item in data.get("evidence", ())),
            confidence=float(data.get("confidence", 0.0) or 0.0),
            candidate_payload=str(data.get("candidate_payload", "") or ""),
            status=str(data.get("status", "pending") or "pending"),
            created_at=created_at,
            claim_token=str(data.get("claim_token", "") or ""),
            claimed_at=str(data.get("claimed_at", "") or ""),
            approval_stage=str(data.get("approval_stage", "") or ""),
            approval_summary=str(data.get("approval_summary", "") or ""),
            approval_artifact_ref=str(data.get("approval_artifact_ref", "") or ""),
            approval_applied_at=str(data.get("approval_applied_at", "") or ""),
            expires_at=expires_at,
            resolved_at=str(data.get("resolved_at", "") or ""),
            resolved_by=str(data.get("resolved_by", "") or ""),
            resolution_note=str(data.get("resolution_note", "") or ""),
        )

    @property
    def payload_dict(self) -> dict[str, Any]:
        if not self.candidate_payload.strip():
            return {}
        try:
            parsed = json.loads(self.candidate_payload)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}


@dataclass(frozen=True)
class FeedbackRecord:
    """Structured user feedback for lifecycle targets."""

    feedback_id: str
    target_type: str
    target_id: str
    verdict: str
    reason: str = ""
    created_by: str = ""
    created_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FeedbackRecord":
        return cls(
            feedback_id=str(data.get("feedback_id", "") or uuid4().hex),
            target_type=str(data.get("target_type", "")),
            target_id=str(data.get("target_id", "")),
            verdict=str(data.get("verdict", "")),
            reason=str(data.get("reason", "") or ""),
            created_by=str(data.get("created_by", "") or ""),
            created_at=str(data.get("created_at", "") or now_iso()),
        )
