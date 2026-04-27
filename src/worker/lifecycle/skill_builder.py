"""Utilities for materializing lifecycle suggestions into SKILL.md files."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from uuid import uuid4

from src.common.content_scanner import scan
from src.services.llm.intent import LLMCallIntent, Purpose
from src.skills.models import Skill, SkillKeyword, SkillScope, SkillStrategy, StrategyMode
from src.skills.parser import SkillParser

_CN_STOPWORDS = frozenset(list("的了是在和与或但如果每天每周定期执行检查确保"))
_EN_STOPWORDS = frozenset(
    "a an the is are was were be been being have has had do does did "
    "will would shall should may might can could to for of in on at by with from".split()
)
_CJK_RANGE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]+")
_EN_TOKEN = re.compile(r"[a-z0-9_-]{2,}")
_SAFE_SKILL_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def stable_skill_id(seed: str, *, prefix: str = "skill") -> str:
    """Build a stable ASCII-only skill identifier from arbitrary input text."""
    raw = str(seed or "").strip().lower()
    slug = "".join(ch if ch.isascii() and ch.isalnum() else "-" for ch in raw)
    slug = "-".join(part for part in slug.split("-") if part)[:32]
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]
    if slug:
        return f"{prefix}-{slug}-{digest}"
    return f"{prefix}-{digest}"


def validate_skill_id(skill_id: str) -> str:
    """Reject unsafe skill identifiers before they reach the filesystem."""
    normalized = str(skill_id or "").strip().lower()
    if not normalized:
        raise ValueError("Skill payload is missing a usable skill_id.")
    if not _SAFE_SKILL_ID.fullmatch(normalized):
        raise ValueError(
            "Unsafe skill_id; expected lowercase ASCII letters, digits, '_' or '-'."
        )
    return normalized


def extract_keywords_from_text(text: str, *, max_keywords: int = 5) -> tuple[str, ...]:
    """Extract a compact mixed-language keyword set from free-form text."""
    combined = str(text or "").strip().lower()
    keywords: list[str] = []
    seen: set[str] = set()

    def _append(keyword: str) -> None:
        normalized = str(keyword).strip()
        if not normalized or normalized in seen:
            return
        seen.add(normalized)
        keywords.append(normalized)

    for span in _CJK_RANGE.findall(combined):
        compact = "".join(ch for ch in span if ch not in _CN_STOPWORDS)
        if len(compact) < 2:
            continue
        candidate = compact if len(compact) <= 4 else compact[-4:]
        _append(candidate)
        if len(keywords) >= max_keywords:
            return tuple(keywords)

    for token in _EN_TOKEN.findall(combined):
        if token in _EN_STOPWORDS:
            continue
        _append(token)
        if len(keywords) >= max_keywords:
            break

    return tuple(keywords) or ("auto-evolved",)


def render_quality_criteria_block(criteria: tuple[str, ...]) -> str:
    """Render the quality criteria appendix for evolved instructions."""
    if not criteria:
        return ""
    criteria_block = "\n".join(f"- {item}" for item in criteria)
    return f"\n\n## Quality Criteria\n{criteria_block}"


async def expand_instructions_with_llm(
    seed: str,
    reason: str,
    llm_client: object,
) -> str:
    """Optionally expand terse instructions using the configured LLM."""
    prompt = (
        "把下面的执行说明扩展为简洁清晰的操作指令，保留核心步骤，不要添加多余背景：\n"
        f"{seed}"
    )
    if reason:
        prompt += f"\n\n背景：{reason}"
    try:
        response = await llm_client.invoke(
            messages=[{"role": "user", "content": prompt}],
            intent=LLMCallIntent(purpose=Purpose.GENERATE),
        )
        expanded = getattr(response, "content", "") or ""
        return expanded.strip() or seed
    except Exception:
        return seed


def build_skill_from_payload(payload: dict) -> Skill:
    """Build a Skill model from a lifecycle suggestion payload."""
    if not isinstance(payload, dict) or not payload:
        raise ValueError("Skill payload must be a non-empty mapping.")
    seed_source = (
        str(payload.get("skill_id", "") or "").strip()
        or str(payload.get("name", "") or "").strip()
        or str(payload.get("instructions_seed", "") or "").strip()
    )
    if not seed_source:
        raise ValueError("Skill payload must include skill_id, name, or instructions_seed.")
    skill_id = validate_skill_id(
        str(payload.get("skill_id", "") or stable_skill_id(seed_source))
    )
    name = str(payload.get("name", "") or skill_id).strip() or skill_id
    description = str(payload.get("description", "") or f"自动演化技能: {name}")
    raw_keywords = payload.get("keywords", ())
    keywords = tuple(
        SkillKeyword(keyword=str(item).strip(), weight=0.5)
        for item in raw_keywords
        if str(item).strip()
    )
    if not keywords:
        fallback_keywords = extract_keywords_from_text(
            " ".join(part for part in (
                str(payload.get("instructions_seed", "") or "").strip(),
                str(payload.get("instructions_reason", "") or "").strip(),
                description,
            ) if part),
        )
        keywords = tuple(
            SkillKeyword(keyword=item, weight=0.5)
            for item in fallback_keywords
        )

    mode_str = str(payload.get("strategy_mode", "autonomous") or "autonomous")
    try:
        mode = StrategyMode(mode_str)
    except ValueError:
        mode = StrategyMode.AUTONOMOUS

    quality_criteria = tuple(
        str(item).strip()
        for item in payload.get("quality_criteria", ())
        if str(item).strip()
    )
    instructions_text = str(payload.get("instructions_seed", "") or name).strip() or name
    instructions_text += render_quality_criteria_block(quality_criteria)

    recommended_tools = tuple(
        str(item).strip()
        for item in payload.get("recommended_tools", ())
        if str(item).strip()
    )

    return Skill(
        skill_id=skill_id,
        name=name,
        description=description,
        version="1.0",
        scope=SkillScope.WORKER,
        strategy=SkillStrategy(mode=mode),
        keywords=keywords,
        recommended_tools=recommended_tools,
        gate_level="auto",
        instructions={"general": instructions_text},
        source_format="genworker_v2",
    )


def skill_to_markdown(skill: Skill) -> str:
    """Serialize a Skill into genworker_v2 ``SKILL.md`` format."""
    lines = [
        "---",
        f"name: {json.dumps(skill.skill_id, ensure_ascii=False)}",
        f"description: {json.dumps(skill.description, ensure_ascii=False)}",
        f"version: {json.dumps(skill.version, ensure_ascii=False)}",
        "metadata:",
        "  genworker:",
        f"    scope: {json.dumps(skill.scope.value, ensure_ascii=False)}",
        f"    gate_level: {json.dumps(skill.gate_level, ensure_ascii=False)}",
        "    strategy:",
        f"      mode: {json.dumps(skill.strategy.mode.value, ensure_ascii=False)}",
    ]
    if skill.keywords:
        lines.append("    keywords:")
        for keyword in skill.keywords:
            lines.append(
                f"      - {{ keyword: {json.dumps(keyword.keyword, ensure_ascii=False)}, "
                f"weight: {keyword.weight} }}"
            )
    if skill.recommended_tools:
        lines.append("    recommended_tools:")
        for tool_name in skill.recommended_tools:
            lines.append(f"      - {json.dumps(tool_name, ensure_ascii=False)}")
    lines.append("---")

    general = skill.instructions.get("general", "").strip()
    if general:
        lines.append("## instructions.general")
        lines.append(general)
    lines.append("")
    return "\n".join(lines)


def write_skill_md(skill: Skill, skills_dir: Path) -> Path:
    """Write one skill artifact with safety scan and roundtrip validation."""
    skill = Skill(
        skill_id=validate_skill_id(skill.skill_id),
        name=skill.name,
        description=skill.description,
        version=skill.version,
        scope=skill.scope,
        priority=skill.priority,
        strategy=skill.strategy,
        keywords=skill.keywords,
        recommended_tools=skill.recommended_tools,
        gate_level=skill.gate_level,
        default_skill=skill.default_skill,
        instructions=skill.instructions,
        source_format=skill.source_format,
        extra_metadata=skill.extra_metadata,
        source_path=skill.source_path,
    )
    skill_md = skill_to_markdown(skill)
    result = scan(skill_md)
    if not result.is_safe:
        raise ValueError(
            f"Content scanner rejected skill '{skill.skill_id}': "
            f"{', '.join(result.violations)}"
        )

    skill_dir = skills_dir / skill.skill_id
    skill_dir.mkdir(parents=True, exist_ok=True)
    path = skill_dir / "SKILL.md"
    tmp_path = skill_dir / f".skill-{uuid4().hex}.tmp"
    tmp_path.write_text(skill_md, encoding="utf-8")

    try:
        SkillParser.parse(tmp_path)
    except Exception as exc:
        tmp_path.unlink(missing_ok=True)
        _prune_empty_dirs(skill_dir, stop_dir=skills_dir)
        raise ValueError(
            f"Generated SKILL.md failed roundtrip parse for '{skill.skill_id}': {exc}"
        ) from exc
    tmp_path.replace(path)
    return path


def _prune_empty_dirs(directory: Path, *, stop_dir: Path) -> None:
    """Remove empty directories created during failed skill materialization."""
    current = directory
    stop = stop_dir.resolve()
    while True:
        try:
            current_resolved = current.resolve()
        except OSError:
            return
        if current_resolved == stop:
            return
        if not current.is_dir():
            current = current.parent
            continue
        try:
            next(current.iterdir())
            return
        except StopIteration:
            current.rmdir()
        except OSError:
            return
        current = current.parent
