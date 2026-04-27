# edition: baseline
"""
Unit tests for the web_fetch tool.

All HTTP interactions are mocked via monkeypatch on httpx.AsyncClient.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.tools.builtin.web_fetch_tool import (
    _strip_html,
    _validate_url,
    _web_fetch_handler,
    create_web_fetch_tool,
)


# ---------------------------------------------------------------------------
# _strip_html unit tests
# ---------------------------------------------------------------------------

def test_strip_html_removes_tags():
    """HTML tags are stripped, plain text preserved."""
    html = "<h1>Title</h1><p>Hello <b>world</b></p>"
    text = _strip_html(html)
    assert "Title" in text
    assert "Hello" in text
    assert "world" in text
    assert "<h1>" not in text
    assert "<b>" not in text


def test_strip_html_removes_script_and_style():
    """Script and style blocks are completely removed."""
    html = (
        "<html><head><style>body{color:red}</style></head>"
        "<body><script>alert(1)</script><p>content</p></body></html>"
    )
    text = _strip_html(html)
    assert "content" in text
    assert "alert" not in text
    assert "color:red" not in text


def test_strip_html_decodes_entities():
    """HTML entities are decoded to their character equivalents."""
    html = "<p>A &amp; B &lt; C &gt; D</p>"
    text = _strip_html(html)
    assert "A & B < C > D" in text


# ---------------------------------------------------------------------------
# _validate_url unit tests
# ---------------------------------------------------------------------------

def test_validate_url_accepts_http():
    assert _validate_url("http://example.com") is None


def test_validate_url_accepts_https():
    assert _validate_url("https://example.com/page") is None


def test_validate_url_rejects_ftp():
    error = _validate_url("ftp://files.example.com/file.txt")
    assert error is not None
    assert "Unsupported URL scheme" in error


def test_validate_url_rejects_missing_host():
    error = _validate_url("http://")
    assert error is not None


# ---------------------------------------------------------------------------
# _web_fetch_handler tests (mocked httpx)
# ---------------------------------------------------------------------------

def _make_mock_response(
    text: str = "Hello",
    status_code: int = 200,
    content_type: str = "text/html",
    content_bytes: bytes | None = None,
) -> MagicMock:
    """Create a mock httpx.Response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    resp.content = content_bytes if content_bytes is not None else text.encode()
    resp.headers = {"content-type": content_type}
    resp.raise_for_status = MagicMock()
    if status_code >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            f"HTTP {status_code}", request=MagicMock(), response=resp,
        )
    return resp


def _patch_httpx_client(mock_response):
    """Return a context manager that patches httpx.AsyncClient."""
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    return patch("httpx.AsyncClient", return_value=mock_client)


@pytest.mark.asyncio
async def test_fetch_html_strips_tags():
    """HTML content is stripped to plain text."""
    html = "<html><body><h1>Title</h1><p>Some content here</p></body></html>"
    mock_resp = _make_mock_response(text=html, content_type="text/html")

    with _patch_httpx_client(mock_resp):
        result = await _web_fetch_handler("https://example.com")

    assert "URL: https://example.com" in result
    assert "Status: 200" in result
    assert "Title" in result
    assert "Some content here" in result
    assert "<h1>" not in result


@pytest.mark.asyncio
async def test_fetch_plain_text_not_stripped():
    """Non-HTML content is returned as-is (not stripped)."""
    mock_resp = _make_mock_response(
        text="plain text body", content_type="text/plain",
    )

    with _patch_httpx_client(mock_resp):
        result = await _web_fetch_handler("https://example.com/data.txt")

    assert "plain text body" in result


@pytest.mark.asyncio
async def test_invalid_url_scheme_returns_error():
    """FTP URL returns an error without making any HTTP request."""
    result = await _web_fetch_handler("ftp://files.example.com/a.txt")

    assert result.startswith("Error:")
    assert "Unsupported URL scheme" in result


@pytest.mark.asyncio
async def test_timeout_returns_error():
    """httpx.TimeoutException is caught and returned as an error."""
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=httpx.TimeoutException("timed out"))
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("httpx.AsyncClient", return_value=mock_client):
        result = await _web_fetch_handler("https://slow.example.com")

    assert result.startswith("Error:")
    assert "timed out" in result.lower()


@pytest.mark.asyncio
async def test_http_error_status_returns_error():
    """HTTP 404 is caught and returned as an error."""
    mock_resp = _make_mock_response(text="Not found", status_code=404)

    with _patch_httpx_client(mock_resp):
        result = await _web_fetch_handler("https://example.com/missing")

    assert result.startswith("Error:")
    assert "404" in result


@pytest.mark.asyncio
async def test_content_truncation_with_max_length():
    """Content longer than max_length is truncated."""
    long_text = "A" * 5000
    mock_resp = _make_mock_response(
        text=long_text, content_type="text/plain",
    )

    with _patch_httpx_client(mock_resp):
        result = await _web_fetch_handler(
            "https://example.com", max_length=1000,
        )

    assert "content truncated" in result.lower()
    # The body section starts after "---\n"
    body_start = result.index("---\n") + 4
    body = result[body_start:]
    # 1000 chars of content + truncation message
    assert len(body) < 1100


@pytest.mark.asyncio
async def test_create_web_fetch_tool_metadata():
    """create_web_fetch_tool returns a Tool with correct metadata."""
    tool = create_web_fetch_tool()

    assert tool.name == "web_fetch"
    assert "url" in tool.parameters
