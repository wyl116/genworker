"""
Rule generator - LLM-based rule extraction from feedback and reflection.

Extracts rule candidates from user feedback or worker self-reflection
using the LLM, then validates and creates them through the rule manager.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Protocol, runtime_checkable, Any

from src.common.content_scanner import scan
from src.services.llm.intent import LLMCallIntent, Purpose

from .models import Rule, RuleCandidate, RuleScope, RuleSource
from .rule_manager import create_rule

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# LLM protocol (matches src/engine/protocols.py LLMClient)
# ---------------------------------------------------------------------------

@runtime_checkable
class LLMClient(Protocol):
    """Protocol for LLM invocation, compatible with engine protocols."""

    async def invoke(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        intent: LLMCallIntent | None = None,
    ) -> Any:
        ...


# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------

_FEEDBACK_EXTRACTION_PROMPT = """\
You are an AI assistant that extracts reusable rules from user feedback.

Given the user's feedback and the task context, determine if there is a \
generalizable rule that should be remembered for future tasks.

If a rule can be extracted, respond with a JSON object:
{{
  "rule": "<concise rule statement>",
  "reason": "<why this rule is useful>",
  "category": "<preference|strategy|prohibition>"
}}

If no generalizable rule can be extracted, respond with:
{{"no_rule": true}}

User feedback: {feedback}

Task context: {task_context}
"""

_REFLECTION_EXTRACTION_PROMPT = """\
You are an AI assistant performing self-reflection after task completion.

Given the execution summary and outcome quality, determine if there is a \
reusable lesson or rule that should be remembered.

If a rule can be extracted, respond with a JSON object:
{{
  "rule": "<concise rule statement>",
  "reason": "<why this rule is useful>",
  "category": "<preference|strategy|prohibition>"
}}

If no useful rule can be extracted, respond with:
{{"no_rule": true}}

Execution summary: {execution_summary}

Outcome quality: {outcome_quality}
"""


# ---------------------------------------------------------------------------
# Rule extraction
# ---------------------------------------------------------------------------

async def extract_rule_from_feedback(
    feedback: str,
    task_context: str,
    llm_client: LLMClient,
) -> RuleCandidate | None:
    """
    Use LLM to extract a rule candidate from user feedback.

    Returns None if no generalizable rule is found.
    """
    prompt = _FEEDBACK_EXTRACTION_PROMPT.format(
        feedback=feedback,
        task_context=task_context,
    )
    return await _extract_candidate(
        prompt=prompt,
        llm_client=llm_client,
        source_type="user_feedback",
        evidence=feedback,
    )


async def extract_rule_from_reflection(
    execution_summary: str,
    outcome_quality: str,
    llm_client: LLMClient,
) -> RuleCandidate | None:
    """
    Use LLM self-reflection to extract a rule candidate after task completion.

    Returns None if no useful rule is found.
    """
    prompt = _REFLECTION_EXTRACTION_PROMPT.format(
        execution_summary=execution_summary,
        outcome_quality=outcome_quality,
    )
    return await _extract_candidate(
        prompt=prompt,
        llm_client=llm_client,
        source_type="self_reflection",
        evidence=execution_summary,
    )


async def _extract_candidate(
    prompt: str,
    llm_client: LLMClient,
    source_type: str,
    evidence: str,
) -> RuleCandidate | None:
    """Common extraction logic: send prompt, parse JSON response."""
    from datetime import datetime, timezone

    messages = [{"role": "user", "content": prompt}]
    try:
        response = await llm_client.invoke(
            messages=messages,
            intent=LLMCallIntent(
                purpose=Purpose.EXTRACT,
                quality_critical=True,
            ),
        )
        content = response.content if hasattr(response, "content") else str(response)
        parsed = _parse_json_response(content)
    except Exception as exc:
        logger.warning("LLM rule extraction failed: %s", exc)
        return None

    if parsed is None or parsed.get("no_rule"):
        return None

    rule_text = parsed.get("rule", "").strip()
    reason = parsed.get("reason", "").strip()
    category = parsed.get("category", "preference").strip()

    if not rule_text:
        return None

    valid_categories = ("preference", "strategy", "prohibition")
    if category not in valid_categories:
        category = "preference"

    now_iso = datetime.now(timezone.utc).isoformat()
    return RuleCandidate(
        rule=rule_text,
        reason=reason,
        category=category,
        scope=RuleScope(),
        source=RuleSource(
            type=source_type,
            evidence=evidence,
            created_at=now_iso,
        ),
    )


def _parse_json_response(text: str) -> dict | None:
    """Extract and parse JSON from LLM response text."""
    text = text.strip()

    # Try direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Try extracting JSON from markdown code block
    for marker in ("```json", "```"):
        if marker in text:
            start = text.index(marker) + len(marker)
            end = text.index("```", start) if "```" in text[start:] else len(text)
            try:
                return json.loads(text[start:end].strip())
            except (json.JSONDecodeError, ValueError):
                pass

    return None


# ---------------------------------------------------------------------------
# Combined validation + creation entry point
# ---------------------------------------------------------------------------

async def validate_and_create_rule(
    candidate: RuleCandidate,
    rules_dir: Path,
    principles: tuple[str, ...],
    existing_rules: tuple[Rule, ...],
    max_learned_rules: int = 30,
    memory_orchestrator: object | None = None,
    tenant_id: str = "",
    worker_id: str = "",
) -> Rule | str:
    """
    Combined entry point: conflict detection + rule creation.

    Returns the created Rule on success, or a conflict description string.
    This is an async wrapper to match the design spec interface, but the
    underlying create_rule is synchronous.
    """
    result = scan(f"{candidate.rule}\n{candidate.reason}")
    if not result.is_safe:
        return f"unsafe_content: {', '.join(result.violations)}"

    created = create_rule(
        rules_dir=rules_dir,
        candidate=candidate,
        principles=principles,
        existing_rules=existing_rules,
        max_learned_rules=max_learned_rules,
    )
    if (
        memory_orchestrator is not None
        and isinstance(created, Rule)
        and hasattr(memory_orchestrator, "on_memory_write")
    ):
        if not str(tenant_id or "").strip() or not str(worker_id or "").strip():
            logger.warning(
                "Rule %s created locally but skipped memory mirror due to missing tenant/worker context",
                created.rule_id,
            )
        else:
            from src.memory.orchestrator import MemoryWriteEvent
            from src.memory.write_models import SemanticFactWritePayload

            try:
                await memory_orchestrator.on_memory_write(MemoryWriteEvent(
                    action="create",
                    target="semantic_fact",
                    entity_id=created.rule_id,
                    content=SemanticFactWritePayload(
                        tenant_id=tenant_id,
                        worker_id=worker_id,
                        rule=created.rule,
                        reason=created.reason,
                    ),
                    source_subsystem="rule",
                    occurred_at=created.source.created_at,
                ))
            except Exception as exc:
                logger.warning(
                    "Rule %s created locally but memory mirror failed: %s",
                    created.rule_id,
                    exc,
                )
    return created
