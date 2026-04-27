"""
ContentParser - LLM-assisted content parsing for goal extraction.

Uses an LLM to extract structured ParsedGoalInfo from unstructured
email/document content. Returns None when confidence is below threshold.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Protocol

from src.services.llm.intent import LLMCallIntent, Purpose

from .domain_models import ParsedGoalInfo

logger = logging.getLogger(__name__)

DEFAULT_CONFIDENCE_THRESHOLD = 0.6

GOAL_EXTRACTION_PROMPT = """
You are {worker_name}, {worker_role}.

The following is the content of a {source_type_desc}:
---
{content}
---

Determine if this content describes a trackable project or task.
If yes, extract the following:
1. Project title
2. Goal description (one sentence summary)
3. Milestone list (each with: title, deadline, subtasks)
4. Overall deadline
5. Priority (high/medium/low)
6. Stakeholder list (name, role, contact)

If the content does NOT contain a trackable project, return null.

Output JSON format:
{{"title": "...", "description": "...", "milestones": [...], "deadline": "...",
  "priority": "...", "stakeholders": [...], "confidence": 0.0}}
""".strip()

SOURCE_TYPE_DESCRIPTIONS = {
    "email": "email message",
    "feishu_doc": "Feishu document",
    "dingtalk": "DingTalk message",
    "wecom": "WeCom message",
    "webhook": "webhook payload",
}


class LLMClient(Protocol):
    """Protocol for LLM invocation."""
    async def invoke(
        self, messages: list[dict[str, str]], **kwargs: Any,
    ) -> Any: ...


class ContentParser:
    """Extract ParsedGoalInfo from unstructured text using LLM."""

    def __init__(
        self,
        llm_client: LLMClient,
        confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
    ) -> None:
        self._llm_client = llm_client
        self._confidence_threshold = confidence_threshold

    async def parse(
        self,
        content: str,
        source_type: str,
        context: dict[str, str] | None = None,
    ) -> ParsedGoalInfo | None:
        """
        Parse content into a ParsedGoalInfo using LLM.

        Returns None if:
        - Content does not describe a trackable goal
        - LLM confidence is below threshold
        - Parsing fails
        """
        if not content or not content.strip():
            logger.debug("[ContentParser] Empty content, skipping")
            return None

        ctx = context or {}
        prompt = self._build_prompt(content, source_type, ctx)

        try:
            raw_result = await self._invoke_llm(prompt)
            return self._parse_response(raw_result, content, source_type, ctx)
        except Exception as exc:
            logger.error(f"[ContentParser] LLM parsing failed: {exc}")
            return None

    def _build_prompt(
        self,
        content: str,
        source_type: str,
        context: dict[str, str],
    ) -> str:
        """Build the extraction prompt from template."""
        source_desc = SOURCE_TYPE_DESCRIPTIONS.get(source_type, source_type)
        return GOAL_EXTRACTION_PROMPT.format(
            worker_name=context.get("worker_name", "Assistant"),
            worker_role=context.get("worker_role", "project tracker"),
            source_type_desc=source_desc,
            content=content,
        )

    async def _invoke_llm(self, prompt: str) -> str:
        """Invoke the LLM and return the raw content string."""
        messages = [{"role": "user", "content": prompt}]
        response = await self._llm_client.invoke(
            messages=messages,
            intent=LLMCallIntent(purpose=Purpose.EXTRACT),
        )
        # Support both string responses and objects with .content
        if isinstance(response, str):
            return response
        return getattr(response, "content", str(response))

    def _parse_response(
        self,
        raw_result: str,
        content: str,
        source_type: str,
        context: dict[str, str],
    ) -> ParsedGoalInfo | None:
        """Parse JSON response into ParsedGoalInfo."""
        cleaned = _extract_json(raw_result)
        if cleaned is None:
            logger.debug("[ContentParser] LLM returned null/no-goal indicator")
            return None

        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            logger.warning(f"[ContentParser] Invalid JSON from LLM: {exc}")
            return None

        if data is None:
            return None

        confidence = float(data.get("confidence", 0.0))
        if confidence < self._confidence_threshold:
            logger.info(
                f"[ContentParser] Confidence {confidence:.2f} below "
                f"threshold {self._confidence_threshold:.2f}, skipping"
            )
            return None

        milestones_raw = data.get("milestones", [])
        milestones = tuple(milestones_raw) if milestones_raw else ()

        stakeholders_raw = data.get("stakeholders", [])
        stakeholders = tuple(
            str(s) for s in stakeholders_raw
        ) if stakeholders_raw else ()

        source_uri = context.get("source_uri", "")

        return ParsedGoalInfo(
            title=str(data.get("title", "")),
            description=str(data.get("description", "")),
            milestones=milestones,
            deadline=data.get("deadline"),
            priority=str(data.get("priority", "medium")),
            stakeholders=stakeholders,
            source_type=source_type,
            source_uri=source_uri,
            raw_content=content,
            confidence=confidence,
        )


def _extract_json(text: str) -> str | None:
    """Extract JSON from LLM response, handling code blocks and null."""
    text = text.strip()
    if text.lower() in ("null", "none", ""):
        return None

    # Strip markdown code blocks
    if text.startswith("```"):
        lines = text.split("\n")
        # Remove first and last line (``` markers)
        inner_lines = []
        for line in lines[1:]:
            if line.strip() == "```":
                break
            inner_lines.append(line)
        text = "\n".join(inner_lines).strip()

    if not text or text.lower() == "null":
        return None

    return text
