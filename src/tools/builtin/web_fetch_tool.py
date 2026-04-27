"""
Web fetch tool - Fetch URL content and convert to readable text.

Uses httpx for HTTP requests. Strips HTML tags with regex
(no external dependency). Includes URL validation, timeout,
and response size limits.
"""
import re
from urllib.parse import urlparse

from src.common.logger import get_logger

from .registry import builtin_tool
from ..mcp.tool import Tool
from ..mcp.types import ConcurrencyLevel, MCPCategory, RiskLevel, ToolType

logger = get_logger()

DEFAULT_MAX_LENGTH = 30000
MAX_RESPONSE_BYTES = 1_048_576  # 1MB
REQUEST_TIMEOUT = 15.0

# Simple HTML tag stripping patterns
_TAG_RE = re.compile(r"<[^>]+>")
_MULTI_NEWLINE_RE = re.compile(r"\n{3,}")
_MULTI_SPACE_RE = re.compile(r"[ \t]{2,}")

# HTML entities
_ENTITIES = {
    "&amp;": "&", "&lt;": "<", "&gt;": ">",
    "&quot;": '"', "&apos;": "'", "&nbsp;": " ",
}


def _strip_html(html: str) -> str:
    """Convert HTML to readable plain text."""
    # Remove script and style blocks
    text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)

    # Convert block elements to newlines
    for tag in ("br", "p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "tr"):
        text = re.sub(rf"</?{tag}[^>]*>", "\n", text, flags=re.IGNORECASE)

    # Strip remaining tags
    text = _TAG_RE.sub("", text)

    # Decode entities
    for entity, char in _ENTITIES.items():
        text = text.replace(entity, char)

    # Clean up whitespace
    text = _MULTI_SPACE_RE.sub(" ", text)
    text = _MULTI_NEWLINE_RE.sub("\n\n", text)

    return text.strip()


def _validate_url(url: str) -> str | None:
    """Validate URL scheme and structure. Returns error message or None."""
    try:
        parsed = urlparse(url)
    except Exception:
        return "Invalid URL format"

    if parsed.scheme not in ("http", "https"):
        return f"Unsupported URL scheme: {parsed.scheme}. Only http/https allowed."

    if not parsed.netloc:
        return "URL must have a valid host"

    return None


async def _web_fetch_handler(
    url: str,
    max_length: int = DEFAULT_MAX_LENGTH,
) -> str:
    """Fetch a URL and return its text content."""
    error = _validate_url(url)
    if error:
        return f"Error: {error}"

    max_length = min(max(1000, max_length), 100000)

    try:
        import httpx
    except ImportError:
        return "Error: httpx not installed. Run: pip install httpx"

    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=REQUEST_TIMEOUT,
        ) as client:
            response = await client.get(
                url,
                headers={"User-Agent": "genworker-agent/1.0"},
            )
            response.raise_for_status()

            content_length = len(response.content)
            if content_length > MAX_RESPONSE_BYTES:
                return (
                    f"Error: Response too large ({content_length} bytes). "
                    f"Max: {MAX_RESPONSE_BYTES} bytes."
                )

            text = response.text
            content_type = response.headers.get("content-type", "")

            if "html" in content_type.lower():
                text = _strip_html(text)

            if len(text) > max_length:
                text = text[:max_length] + "\n\n... [content truncated]"

            logger.info(
                f"[WebFetch] {url}: {response.status_code}, "
                f"{content_length} bytes"
            )

            return (
                f"URL: {url}\n"
                f"Status: {response.status_code}\n"
                f"Content-Length: {content_length}\n"
                f"---\n{text}"
            )

    except httpx.TimeoutException:
        return f"Error: Request timed out ({REQUEST_TIMEOUT}s): {url}"
    except httpx.HTTPStatusError as e:
        return f"Error: HTTP {e.response.status_code}: {url}"
    except Exception as e:
        logger.error(f"[WebFetch] Failed for {url}: {e}")
        return f"Error fetching URL: {e}"


@builtin_tool()
def create_web_fetch_tool() -> Tool:
    """Create the web_fetch Tool instance."""
    return Tool(
        name="web_fetch",
        description=(
            "Fetch content from a URL and convert to readable text. "
            "HTML pages are automatically stripped to plain text. "
            "Supports http/https URLs with timeout and size limits."
        ),
        handler=_web_fetch_handler,
        parameters={
            "url": {
                "type": "string",
                "description": "URL to fetch (http or https)",
            },
            "max_length": {
                "type": "integer",
                "description": f"Max characters to return (default: {DEFAULT_MAX_LENGTH})",
            },
        },
        required_params=("url",),
        tool_type=ToolType.READ,
        category=MCPCategory.SPECIALIZED,
        risk_level=RiskLevel.MEDIUM,
        concurrency=ConcurrencyLevel.SAFE,
        tags=frozenset({"web", "fetch", "url", "http", "scrape"}),
    )
