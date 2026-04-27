# edition: baseline
"""
Unit tests for the web_search tool.

All HTTP interactions are mocked. Tests cover API key validation,
successful search, empty query, and result formatting.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.tools.builtin.web_search_tool import (
    SearchAPIConfig,
    _format_results,
    _web_search_handler,
    create_web_search_tool,
)


# ---------------------------------------------------------------------------
# _format_results unit tests
# ---------------------------------------------------------------------------

def test_format_results_empty():
    """Empty results list returns 'No results found' message."""
    output = _format_results("test query", [])
    assert "No results found" in output
    assert "test query" in output


def test_format_results_with_items():
    """Results are numbered and include title, URL, and snippet."""
    results = [
        {"title": "First", "url": "https://a.com", "snippet": "snippet A"},
        {"title": "Second", "url": "https://b.com", "snippet": "snippet B"},
    ]
    output = _format_results("my query", results)

    assert "Search results for: my query" in output
    assert "1. First" in output
    assert "URL: https://a.com" in output
    assert "snippet A" in output
    assert "2. Second" in output


def test_format_results_without_url():
    """Result with empty URL omits the URL line."""
    results = [{"title": "Summary", "url": "", "snippet": "answer text"}]
    output = _format_results("q", results)

    assert "1. Summary" in output
    assert "URL:" not in output
    assert "answer text" in output


# ---------------------------------------------------------------------------
# _web_search_handler tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_api_key_returns_error():
    """Missing API key returns an error for non-custom providers."""
    config = SearchAPIConfig(provider="tavily", api_key="")

    result = await _web_search_handler(
        "test query", 5, _config=config,
    )

    assert result.startswith("Error:")
    assert "No API key" in result


@pytest.mark.asyncio
async def test_empty_query_returns_error():
    """Empty or whitespace-only query returns an error."""
    config = SearchAPIConfig(provider="tavily", api_key="key123")

    result_empty = await _web_search_handler("", 5, _config=config)
    assert result_empty.startswith("Error:")
    assert "empty" in result_empty.lower()

    result_spaces = await _web_search_handler("   ", 5, _config=config)
    assert result_spaces.startswith("Error:")


@pytest.mark.asyncio
async def test_successful_tavily_search():
    """Mock a successful Tavily API response and verify formatting."""
    config = SearchAPIConfig(provider="tavily", api_key="test-key")

    tavily_response = {
        "answer": "Python is a programming language.",
        "results": [
            {
                "title": "Python Official",
                "url": "https://python.org",
                "content": "Welcome to Python",
            },
            {
                "title": "Python Wikipedia",
                "url": "https://en.wikipedia.org/wiki/Python",
                "content": "Python is an interpreted language",
            },
        ],
    }

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = tavily_response
    mock_resp.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_resp)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("httpx.AsyncClient", return_value=mock_client):
        result = await _web_search_handler("Python", 5, _config=config)

    assert "Search results for: Python" in result
    # Summary from answer field
    assert "Summary" in result
    assert "programming language" in result
    # Regular results
    assert "Python Official" in result
    assert "python.org" in result


@pytest.mark.asyncio
async def test_unknown_provider_returns_error():
    """An unknown provider name returns an error."""
    config = SearchAPIConfig(provider="unknown_provider", api_key="key")

    result = await _web_search_handler("query", 5, _config=config)

    assert result.startswith("Error:")
    assert "Unknown search provider" in result


@pytest.mark.asyncio
async def test_api_error_returns_error():
    """HTTP error from the search API returns a descriptive error."""
    config = SearchAPIConfig(provider="tavily", api_key="bad-key")

    mock_resp = MagicMock()
    mock_resp.status_code = 401
    mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
        "401 Unauthorized", request=MagicMock(), response=mock_resp,
    )

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_resp)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("httpx.AsyncClient", return_value=mock_client):
        result = await _web_search_handler("query", 5, _config=config)

    assert result.startswith("Error:")


@pytest.mark.asyncio
async def test_create_web_search_tool_metadata():
    """create_web_search_tool returns a Tool with correct metadata."""
    tool = create_web_search_tool()

    assert tool.name == "web_search"
    assert "query" in tool.parameters


@pytest.mark.asyncio
async def test_create_web_search_tool_with_config():
    """create_web_search_tool accepts a custom config."""
    config = SearchAPIConfig(provider="serpapi", api_key="my-key")
    tool = create_web_search_tool(config=config)

    assert tool.name == "web_search"
