# edition: baseline
"""
Tests for ContentParser - LLM-assisted content extraction.
"""
from __future__ import annotations

import json

import pytest

from src.worker.integrations.content_parser import ContentParser, _extract_json
from src.worker.integrations.domain_models import ParsedGoalInfo


# ---------------------------------------------------------------------------
# Mock LLM Client
# ---------------------------------------------------------------------------

class MockLLMClient:
    """Mock LLM client that returns a predetermined response."""

    def __init__(self, response: str) -> None:
        self._response = response
        self.call_count = 0
        self.last_messages: list | None = None

    async def invoke(self, messages, **kwargs):
        self.call_count += 1
        self.last_messages = messages
        return self._response


class MockLLMClientWithContent:
    """Mock LLM client that returns object with .content attr."""

    def __init__(self, content: str) -> None:
        self._content = content

    async def invoke(self, messages, **kwargs):
        class Resp:
            pass
        r = Resp()
        r.content = self._content
        return r


class FailingLLMClient:
    """Mock LLM client that raises an exception."""

    async def invoke(self, messages, **kwargs):
        raise RuntimeError("LLM unavailable")


# ---------------------------------------------------------------------------
# _extract_json helper
# ---------------------------------------------------------------------------

class TestExtractJson:
    def test_null_string(self):
        assert _extract_json("null") is None

    def test_none_string(self):
        assert _extract_json("none") is None

    def test_empty_string(self):
        assert _extract_json("") is None

    def test_plain_json(self):
        result = _extract_json('{"title": "test"}')
        assert result == '{"title": "test"}'

    def test_code_block_json(self):
        text = '```json\n{"title": "test"}\n```'
        result = _extract_json(text)
        assert result == '{"title": "test"}'

    def test_code_block_no_lang(self):
        text = '```\n{"title": "test"}\n```'
        result = _extract_json(text)
        assert result == '{"title": "test"}'

    def test_null_in_code_block(self):
        text = "```\nnull\n```"
        result = _extract_json(text)
        assert result is None


# ---------------------------------------------------------------------------
# ContentParser.parse
# ---------------------------------------------------------------------------

class TestContentParserParse:
    @pytest.mark.asyncio
    async def test_high_confidence_returns_parsed_info(self):
        response_data = {
            "title": "Q2 Data Migration",
            "description": "Migrate all data to new platform",
            "milestones": [
                {"title": "Design", "deadline": "2026-04-15", "tasks": []},
            ],
            "deadline": "2026-06-30",
            "priority": "high",
            "stakeholders": ["alice@company.com", "bob@company.com"],
            "confidence": 0.85,
        }
        llm = MockLLMClient(json.dumps(response_data))
        parser = ContentParser(llm, confidence_threshold=0.6)

        result = await parser.parse(
            content="We need to migrate Q2 data by June...",
            source_type="email",
            context={"source_uri": "email://inbox/123"},
        )

        assert result is not None
        assert isinstance(result, ParsedGoalInfo)
        assert result.title == "Q2 Data Migration"
        assert result.priority == "high"
        assert result.confidence == 0.85
        assert result.source_type == "email"
        assert result.source_uri == "email://inbox/123"
        assert len(result.stakeholders) == 2
        assert len(result.milestones) == 1

    @pytest.mark.asyncio
    async def test_low_confidence_returns_none(self):
        response_data = {
            "title": "Maybe a project",
            "description": "Unclear",
            "milestones": [],
            "confidence": 0.3,
        }
        llm = MockLLMClient(json.dumps(response_data))
        parser = ContentParser(llm, confidence_threshold=0.6)

        result = await parser.parse(
            content="Hey, let's grab lunch sometime",
            source_type="email",
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_null_response_returns_none(self):
        llm = MockLLMClient("null")
        parser = ContentParser(llm)

        result = await parser.parse(
            content="Random newsletter content",
            source_type="email",
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_empty_content_returns_none(self):
        llm = MockLLMClient("should not be called")
        parser = ContentParser(llm)

        result = await parser.parse(content="", source_type="email")

        assert result is None
        assert llm.call_count == 0

    @pytest.mark.asyncio
    async def test_llm_failure_returns_none(self):
        llm = FailingLLMClient()
        parser = ContentParser(llm)

        result = await parser.parse(
            content="Important project details",
            source_type="email",
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_invalid_json_returns_none(self):
        llm = MockLLMClient("not valid json at all")
        parser = ContentParser(llm)

        result = await parser.parse(
            content="Some content",
            source_type="email",
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_response_with_content_attribute(self):
        response_data = {
            "title": "Test Project",
            "description": "A test",
            "milestones": [],
            "confidence": 0.9,
        }
        llm = MockLLMClientWithContent(json.dumps(response_data))
        parser = ContentParser(llm, confidence_threshold=0.5)

        result = await parser.parse(
            content="project details here",
            source_type="feishu_doc",
        )

        assert result is not None
        assert result.title == "Test Project"
        assert result.source_type == "feishu_doc"

    @pytest.mark.asyncio
    async def test_code_block_response_parsed(self):
        response_data = {
            "title": "Wrapped Project",
            "description": "In code block",
            "milestones": [],
            "confidence": 0.75,
        }
        llm = MockLLMClient(f"```json\n{json.dumps(response_data)}\n```")
        parser = ContentParser(llm, confidence_threshold=0.6)

        result = await parser.parse(
            content="Some project email",
            source_type="email",
        )

        assert result is not None
        assert result.title == "Wrapped Project"

    @pytest.mark.asyncio
    async def test_default_threshold_is_0_6(self):
        response_data = {
            "title": "Edge Case",
            "description": "Exactly at threshold",
            "milestones": [],
            "confidence": 0.6,
        }
        llm = MockLLMClient(json.dumps(response_data))
        parser = ContentParser(llm)  # default threshold 0.6

        result = await parser.parse(
            content="project content",
            source_type="email",
        )

        assert result is not None
        assert result.confidence == 0.6

    @pytest.mark.asyncio
    async def test_just_below_threshold_returns_none(self):
        response_data = {
            "title": "Edge Case",
            "description": "Just below",
            "milestones": [],
            "confidence": 0.59,
        }
        llm = MockLLMClient(json.dumps(response_data))
        parser = ContentParser(llm)

        result = await parser.parse(
            content="vague content",
            source_type="email",
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_raw_content_preserved(self):
        response_data = {
            "title": "Content Test",
            "description": "Test",
            "milestones": [],
            "confidence": 0.8,
        }
        llm = MockLLMClient(json.dumps(response_data))
        parser = ContentParser(llm, confidence_threshold=0.5)
        original_content = "The original email body text"

        result = await parser.parse(
            content=original_content,
            source_type="email",
        )

        assert result is not None
        assert result.raw_content == original_content
