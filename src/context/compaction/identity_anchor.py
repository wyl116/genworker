"""Identity anchor helpers for post-compaction dynamic context."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class IdentityAnchor:
    identity_digest: str
    principles_digest: str
    constraints_digest: str


def build_identity_anchor(
    *,
    identity: str,
    principles: str,
    constraints: str,
) -> IdentityAnchor:
    return IdentityAnchor(
        identity_digest=_extract_digest(identity, max_lines=5),
        principles_digest=_extract_digest(principles, max_lines=5),
        constraints_digest=_extract_digest(constraints, max_lines=3),
    )


def anchor_to_context(anchor: IdentityAnchor) -> str:
    parts: list[str] = []
    if anchor.identity_digest:
        parts.append(f"[Identity Reminder]\n{anchor.identity_digest}")
    if anchor.principles_digest:
        parts.append(f"[Core Principles]\n{anchor.principles_digest}")
    if anchor.constraints_digest:
        parts.append(f"[Key Constraints]\n{anchor.constraints_digest}")
    return "\n\n".join(parts)


def _extract_digest(text: str, max_lines: int) -> str:
    if not text.strip():
        return ""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return "\n".join(lines[:max_lines])
