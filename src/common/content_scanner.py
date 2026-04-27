"""Content safety scanner for learned artifacts."""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ScanResult:
    """Result of scanning generated or persisted content."""

    is_safe: bool
    violations: tuple[str, ...]


_INJECTION_PATTERNS: tuple[tuple[str, str], ...] = (
    ("prompt_injection", r"ignore\s+(all\s+)?previous\s+instructions"),
    ("prompt_injection", r"you\s+are\s+now"),
    ("prompt_injection", r"system\s+prompt\s+override"),
    ("prompt_injection", r"new\s+instructions?\s*:"),
    ("prompt_injection", r"forget\s+(everything|all|your\s+instructions)"),
    ("prompt_injection", r"disregard\s+(all|previous|above)"),
)

_COMMAND_EXFILTRATION_PATTERNS: tuple[tuple[str, str], ...] = (
    ("command_exfiltration", r"(curl|wget|fetch)\s+https?://"),
    ("command_exfiltration", r"ssh\s+-i"),
)

_SECRET_EXFILTRATION_PATTERNS: tuple[tuple[str, str], ...] = (
    ("secret_exfiltration", r"\b(API_KEY|SECRET|PASSWORD|TOKEN)\b"),
    ("secret_exfiltration", r"\.env\b"),
    ("secret_exfiltration", r"credentials?\.(json|yaml|yml)"),
)

_UNICODE_PATTERNS: tuple[tuple[str, str], ...] = (
    ("hidden_unicode", r"[\u200b-\u200f]"),
    ("hidden_unicode", r"[\u202a-\u202e]"),
    ("hidden_unicode", r"[\ufeff]"),
)


def scan(content: str) -> ScanResult:
    """Scan content for prompt injection, exfiltration, and hidden Unicode."""
    text = content or ""
    violations: list[str] = []

    for label, pattern in _INJECTION_PATTERNS:
        if re.search(pattern, text, flags=re.IGNORECASE):
            violations.append(label)

    command_context = any(
        re.search(pattern, text, flags=re.IGNORECASE)
        for _, pattern in _COMMAND_EXFILTRATION_PATTERNS
    )
    if command_context:
        violations.extend(
            label
            for label, pattern in _COMMAND_EXFILTRATION_PATTERNS
            if re.search(pattern, text, flags=re.IGNORECASE)
        )
        violations.extend(
            label
            for label, pattern in _SECRET_EXFILTRATION_PATTERNS
            if re.search(pattern, text, flags=re.IGNORECASE)
        )

    violations.extend(
        label
        for label, pattern in _UNICODE_PATTERNS
        if re.search(pattern, text)
    )

    return ScanResult(
        is_safe=not violations,
        violations=tuple(dict.fromkeys(violations)),
    )
