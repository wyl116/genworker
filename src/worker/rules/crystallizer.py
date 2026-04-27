"""Rule crystallization into runtime skills or tools."""
from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import uuid4

from src.services.llm.intent import LLMCallIntent, Purpose
from src.tools.mcp.tool import Tool
from src.tools.mcp.types import MCPCategory, RiskLevel, ToolType
from src.worker.lifecycle.file_io import atomic_write_text
from src.worker.lifecycle.models import SuggestionRecord, add_days_iso, now_iso
from src.worker.lifecycle.skill_builder import (
    build_skill_from_payload,
    expand_instructions_with_llm,
    extract_keywords_from_text,
    stable_skill_id,
    write_skill_md,
)

from .models import Rule, rule_to_markdown
from .rule_manager import load_rules


CRYSTALLIZATION_CONFIDENCE = 0.9
CRYSTALLIZATION_APPLY_COUNT = 20


@dataclass(frozen=True)
class CrystallizationCandidate:
    rule: Rule
    target: str
    reason: str


@dataclass(frozen=True)
class CrystallizationResult:
    rule_id: str
    target: str
    success: bool
    artifact_path: str = ""
    artifact_ref: str = ""
    outcome: str = "applied"
    error: str = ""


def identify_crystallization_candidates(
    rules: tuple[Rule, ...],
) -> tuple[CrystallizationCandidate, ...]:
    """Find active learned rules ready to crystallize."""
    results: list[CrystallizationCandidate] = []
    for rule in rules:
        if (
            rule.type != "learned"
            or rule.status != "active"
            or rule.confidence < CRYSTALLIZATION_CONFIDENCE
            or rule.apply_count < CRYSTALLIZATION_APPLY_COUNT
        ):
            continue
        target = _classify_target(rule)
        results.append(
            CrystallizationCandidate(
                rule=rule,
                target=target,
                reason=f"confidence={rule.confidence:.2f}, apply_count={rule.apply_count}",
            )
        )
    return tuple(results)


async def crystallize_to_skill(
    candidate: CrystallizationCandidate,
    skills_dir: Path,
    llm_client: object | None,
) -> CrystallizationResult:
    """Expand one rule into a valid ``SKILL.md`` artifact."""
    payload = _build_rule_skill_payload(candidate.rule)
    skill = build_skill_from_payload(payload)
    if llm_client is not None:
        instructions = await expand_instructions_with_llm(
            candidate.rule.rule,
            candidate.rule.reason,
            llm_client,
        )
        skill = replace(skill, instructions={"general": instructions})
    path = write_skill_md(skill, skills_dir)
    return CrystallizationResult(
        candidate.rule.rule_id,
        "skill",
        True,
        artifact_path=str(path),
        artifact_ref=str(path),
    )


async def crystallize_to_tool(
    candidate: CrystallizationCandidate,
    mcp_server: object,
    llm_client: object | None,
) -> CrystallizationResult:
    """Register one crystallized tool backed by an LLM system instruction."""
    name = f"crystallized_{candidate.rule.rule_id}"

    async def _handler(task: str = "") -> dict[str, str]:
        if llm_client is None:
            return {"success": False, "error": "llm unavailable"}
        prompt = (
            f"遵循以下结晶规则完成任务：{candidate.rule.rule}\n"
            f"原因：{candidate.rule.reason}\n"
            f"任务：{task}"
        )
        response = await llm_client.invoke(
            messages=[{"role": "user", "content": prompt}],
            intent=LLMCallIntent(purpose=Purpose.GENERATE),
        )
        return {"success": True, "content": getattr(response, "content", "")}

    tool = Tool(
        name=name,
        description=f"Crystallized tool for rule {candidate.rule.rule_id}",
        handler=_handler,
        parameters={"task": {"type": "string", "description": "要执行的任务"}},
        required_params=("task",),
        tool_type=ToolType.WRITE,
        category=MCPCategory.SPECIALIZED,
        risk_level=RiskLevel.MEDIUM,
        tags=frozenset({"learning", "crystallized"}),
        enabled=True,
    )
    mcp_server.register_tool(tool)
    return CrystallizationResult(
        candidate.rule.rule_id,
        "tool",
        True,
        artifact_ref=name,
    )


async def run_crystallization_cycle(
    rules_dir: Path,
    skills_dir: Path,
    mcp_server: object | None,
    llm_client: object | None,
    *,
    suggestion_store: object | None = None,
    tenant_id: str = "",
    worker_id: str = "",
) -> tuple[CrystallizationResult, ...]:
    """Identify and crystallize all eligible rules."""
    if suggestion_store is not None and (not tenant_id.strip() or not worker_id.strip()):
        raise ValueError("tenant_id and worker_id are required when suggestion_store is enabled.")
    rules = load_rules(rules_dir)
    results: list[CrystallizationResult] = []
    for candidate in identify_crystallization_candidates(rules):
        if candidate.rule.status == "crystallized":
            continue
        try:
            if candidate.target == "skill":
                if suggestion_store is not None:
                    result = _create_skill_suggestion(
                        candidate,
                        suggestion_store,
                        tenant_id=tenant_id,
                        worker_id=worker_id,
                    )
                else:
                    result = await crystallize_to_skill(candidate, skills_dir, llm_client)
            elif candidate.target == "tool" and mcp_server is not None:
                result = await crystallize_to_tool(candidate, mcp_server, llm_client)
            else:
                result = CrystallizationResult(
                    candidate.rule.rule_id,
                    candidate.target,
                    False,
                    error="target not crystallized",
                )
            if result.success and candidate.target == "tool":
                _mark_rule_crystallized(rules_dir, candidate.rule)
            elif result.success and candidate.target == "skill" and suggestion_store is None:
                _mark_rule_crystallized(rules_dir, candidate.rule)
            results.append(result)
        except Exception as exc:
            results.append(
                CrystallizationResult(
                    candidate.rule.rule_id,
                    candidate.target,
                    False,
                    error=str(exc),
                )
            )
    return tuple(results)


def _mark_rule_crystallized(rules_dir: Path, rule: Rule) -> None:
    updated = replace(rule, status="crystallized")
    path = rules_dir / "learned" / f"{rule.rule_id}.md"
    atomic_write_text(path, rule_to_markdown(updated))


def _classify_target(rule: Rule) -> str:
    text = rule.rule.lower()
    if rule.category in {"preference", "prohibition"}:
        return "none"
    if "step 1" in text or "步骤1" in text or any(token in text for token in ("step 2", "step 3", "1.", "2.", "3.")):
        return "skill"
    if "when " in text and ", do " in text:
        return "tool"
    return "none"


def _create_skill_suggestion(
    candidate: CrystallizationCandidate,
    suggestion_store,
    *,
    tenant_id: str,
    worker_id: str,
) -> CrystallizationResult:
    """Create a rule_to_skill suggestion instead of writing directly."""
    rule = candidate.rule
    if not tenant_id.strip() or not worker_id.strip():
        raise ValueError("tenant_id and worker_id are required for rule_to_skill suggestions.")
    block_reason = suggestion_store.creation_block_reason(
        tenant_id,
        worker_id,
        suggestion_type="rule_to_skill",
        source_entity_id=rule.rule_id,
    )
    if block_reason == "already approved":
        return CrystallizationResult(
            rule.rule_id,
            "skill",
            True,
            outcome="skipped",
            error="already approved",
        )

    payload = _build_rule_skill_payload(rule)
    suggestion = SuggestionRecord(
        suggestion_id=f"sugg-{uuid4().hex[:8]}",
        type="rule_to_skill",
        source_entity_type="rule",
        source_entity_id=rule.rule_id,
        title=f"建议将规则结晶为 Skill: {rule.rule[:40]}",
        reason=candidate.reason,
        evidence=(rule.rule_id,),
        confidence=min(0.99, rule.confidence),
        candidate_payload=json.dumps(payload, ensure_ascii=False),
        expires_at=add_days_iso(now_iso(), 30),
    )
    created = suggestion_store.create(tenant_id, worker_id, suggestion)
    if created is None:
        return CrystallizationResult(
            rule.rule_id,
            "skill",
            True,
            outcome="skipped",
            error=block_reason or "suggestion not created",
        )
    return CrystallizationResult(
        rule.rule_id,
        "skill",
        True,
        artifact_ref=created.suggestion_id,
        outcome="queued",
    )


def _build_rule_skill_payload(rule: Rule) -> dict:
    skill_id = stable_skill_id(
        f"{rule.rule_id}:{rule.rule}:{rule.reason}",
        prefix="crystallized",
    )
    return {
        "skill_id": skill_id,
        "name": skill_id,
        "description": f"自动结晶规则技能：{rule.rule[:40]}",
        "keywords": list(extract_keywords_from_text(rule.rule)),
        "strategy_mode": "autonomous",
        "instructions_seed": rule.rule,
        "instructions_reason": rule.reason,
        "recommended_tools": [],
        "source_type": "rule",
        "source_rule_id": rule.rule_id,
    }
