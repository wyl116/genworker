"""Preference and decision extraction with JSONL persistence."""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from src.common.content_scanner import scan

_PREFERENCE_PATTERNS: tuple[str, ...] = (
    r"我(喜欢|偏好|习惯|总是|从不|不要|不喜欢|讨厌)(?P<content>[^。！？!\n]+)",
    r"(请|麻烦)?(以后|之后|每次|下次)(都|总是|不要)(?P<content>[^。！？!\n]+)",
    r"I\s+(prefer|like|always|never|don't\s+like|hate)\s+(?P<content>[^.!?\n]+)",
)
_DECISION_PATTERNS: tuple[str, ...] = (
    r"(决定|确定|选择|定下|敲定|就)(?P<content>[^。！？!\n]+)",
    r"(最终|那就|就)(?P<content>[^。！？!\n]+)",
    r"方案\s*(?P<content>[ABCD][^。！？!\n]*)",
    r"(decided\s+to|agreed\s+on|chose\s+to|we(?:'ll|\s+will)\s+go\s+with|let's\s+use)\s+(?P<content>[^.!?\n]+)",
)


@dataclass(frozen=True)
class UserPreference:
    """Persisted user preference."""

    preference_id: str
    category: str
    content: str
    confidence: float
    extracted_from: str
    extracted_at: str


@dataclass(frozen=True)
class UserDecision:
    """Persisted user decision."""

    decision_id: str
    topic: str
    decision: str
    confidence: float
    decided_at: str
    context: str
    superseded_by: str = ""


def extract_preferences(
    user_input: str,
    assistant_summary: str = "",
) -> tuple[UserPreference, ...]:
    """Extract explicit preference statements from user input."""
    text = user_input or ""
    if not text.strip():
        return ()
    now = _now_iso()
    results: list[UserPreference] = []
    for pattern in _PREFERENCE_PATTERNS:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            content = _clean_capture(match.groupdict().get("content", ""))
            if not content:
                continue
            results.append(UserPreference(
                preference_id=f"pref-{uuid4().hex[:12]}",
                category=_infer_preference_category(content),
                content=content,
                confidence=0.6,
                extracted_from=match.group(0).strip(),
                extracted_at=now,
            ))
    return merge_preferences((), tuple(results))


def extract_decisions(
    user_input: str,
    assistant_summary: str = "",
) -> tuple[UserDecision, ...]:
    """Extract explicit decisions from user input and summary text."""
    candidates: list[UserDecision] = []
    now = _now_iso()
    for text in (user_input or "", assistant_summary or ""):
        if not text.strip():
            continue
        for pattern in _DECISION_PATTERNS:
            for match in re.finditer(pattern, text, flags=re.IGNORECASE):
                decision = _clean_capture(match.groupdict().get("content", ""))
                if not decision:
                    continue
                candidates.append(UserDecision(
                    decision_id=f"dec-{uuid4().hex[:12]}",
                    topic=_infer_decision_topic(decision),
                    decision=decision,
                    confidence=0.7,
                    decided_at=now,
                    context=match.group(0).strip(),
                ))
    deduped: dict[tuple[str, str], UserDecision] = {}
    for item in candidates:
        deduped[(item.topic, item.decision.lower())] = item
    return tuple(deduped.values())


def merge_preferences(
    existing: tuple[UserPreference, ...],
    new: tuple[UserPreference, ...],
) -> tuple[UserPreference, ...]:
    """Merge duplicate preferences and boost confidence."""
    merged: dict[tuple[str, str], UserPreference] = {
        (item.category, item.content.lower()): item for item in existing
    }
    for item in new:
        key = (item.category, item.content.lower())
        current = merged.get(key)
        if current is None:
            merged[key] = item
            continue
        merged[key] = replace(
            current,
            confidence=min(1.0, round(max(current.confidence, item.confidence) + 0.1, 3)),
            extracted_at=item.extracted_at,
            extracted_from=item.extracted_from,
        )
    return tuple(sorted(merged.values(), key=lambda value: (-value.confidence, value.extracted_at)))


def supersede_decisions(
    existing: tuple[UserDecision, ...],
    new: tuple[UserDecision, ...],
) -> tuple[UserDecision, ...]:
    """Supersede previous decisions when a new decision shares the topic."""
    merged = list(existing)
    for item in new:
        if any(
            current.topic == item.topic
            and not current.superseded_by
            and current.decision.strip().lower() == item.decision.strip().lower()
            for current in merged
        ):
            continue
        updated_existing: list[UserDecision] = []
        for current in merged:
            if current.topic == item.topic and not current.superseded_by:
                updated_existing.append(replace(current, superseded_by=item.decision_id))
            else:
                updated_existing.append(current)
        merged = updated_existing
        merged.append(item)
    return tuple(sorted(merged, key=lambda value: value.decided_at, reverse=True))


def store_preference(
    preferences_path: Path,
    preference: UserPreference,
) -> UserPreference:
    """Append a preference to JSONL storage after safety scan."""
    _store_json_line(preferences_path, asdict(preference), preference.content)
    return preference


def store_decision(
    decisions_path: Path,
    decision: UserDecision,
) -> UserDecision:
    """Append a decision to JSONL storage after safety scan."""
    _store_json_line(decisions_path, asdict(decision), decision.decision)
    return decision


def save_preferences(
    preferences_path: Path,
    preferences: tuple[UserPreference, ...],
) -> None:
    """Rewrite all preferences to JSONL storage."""
    _rewrite_json_lines(
        preferences_path,
        tuple((asdict(item), item.content) for item in preferences),
    )


def save_decisions(
    decisions_path: Path,
    decisions: tuple[UserDecision, ...],
) -> None:
    """Rewrite all decisions to JSONL storage."""
    _rewrite_json_lines(
        decisions_path,
        tuple((asdict(item), item.decision) for item in decisions),
    )


def load_preferences(
    preferences_path: Path,
) -> tuple[UserPreference, ...]:
    """Load stored preferences ordered by confidence descending."""
    if not preferences_path.exists():
        return ()
    items = [
        UserPreference(**payload)
        for payload in _load_json_lines(preferences_path)
        if payload.get("preference_id")
    ]
    return tuple(sorted(items, key=lambda value: (-value.confidence, value.extracted_at)))


def load_active_decisions(
    decisions_path: Path,
) -> tuple[UserDecision, ...]:
    """Load non-superseded decisions ordered by newest first."""
    if not decisions_path.exists():
        return ()
    items = [
        UserDecision(**payload)
        for payload in _load_json_lines(decisions_path)
        if payload.get("decision_id")
    ]
    active = [item for item in items if not item.superseded_by]
    return tuple(sorted(active, key=lambda value: value.decided_at, reverse=True))


def load_decisions(
    decisions_path: Path,
) -> tuple[UserDecision, ...]:
    """Load all decisions including superseded history."""
    if not decisions_path.exists():
        return ()
    items = [
        UserDecision(**payload)
        for payload in _load_json_lines(decisions_path)
        if payload.get("decision_id")
    ]
    return tuple(sorted(items, key=lambda value: value.decided_at, reverse=True))


def format_preferences_for_prompt(
    preferences: tuple[UserPreference, ...],
    max_items: int = 10,
) -> str:
    """Format preferences into prompt-friendly text."""
    if not preferences:
        return ""
    lines = ["[User Preferences]"]
    for item in preferences[:max_items]:
        lines.append(f"- ({item.category}) {item.content}")
    return "\n".join(lines)


def format_decisions_for_prompt(
    decisions: tuple[UserDecision, ...],
    max_items: int = 5,
) -> str:
    """Format active decisions into prompt-friendly text."""
    if not decisions:
        return ""
    lines = ["[User Decisions]"]
    for item in decisions[:max_items]:
        lines.append(f"- ({item.topic}, {item.decided_at}) {item.decision}")
    return "\n".join(lines)


def _store_json_line(path: Path, payload: dict[str, object], safety_text: str) -> None:
    result = scan(safety_text)
    if not result.is_safe:
        raise ValueError(f"unsafe preference content: {', '.join(result.violations)}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _rewrite_json_lines(
    path: Path,
    rows: tuple[tuple[dict[str, object], str], ...],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized: list[str] = []
    for payload, safety_text in rows:
        result = scan(safety_text)
        if not result.is_safe:
            raise ValueError(f"unsafe preference content: {', '.join(result.violations)}")
        serialized.append(json.dumps(payload, ensure_ascii=False))
    path.write_text(
        "\n".join(serialized) + ("\n" if serialized else ""),
        encoding="utf-8",
    )


def _load_json_lines(path: Path) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            items.append(payload)
    return items


def _infer_preference_category(content: str) -> str:
    lowered = content.lower()
    if "markdown" in lowered or "表格" in content or "格式" in content:
        return "format"
    if "简洁" in content or "风格" in content or "style" in lowered:
        return "style"
    if "总是" in content or "不要" in content or "always" in lowered or "never" in lowered:
        return "behavior"
    return "domain"


def _infer_decision_topic(content: str) -> str:
    lowered = content.lower()
    if "redis" in lowered or "mysql" in lowered or "postgres" in lowered or "存储" in content:
        return "storage"
    if "方案" in content or "plan" in lowered or "strategy" in lowered:
        return "strategy"
    if "流程" in content or "process" in lowered:
        return "process"
    if "工具" in content or "tool" in lowered:
        return "tooling"
    return "general"


def _clean_capture(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip(" ：:,.。！!？?")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
