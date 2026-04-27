"""
Web search tool - Search the web via configurable search API.

Supports multiple search backends (SerpAPI, Tavily, or custom HTTP).
Results are formatted as an LLM-friendly summary list.
"""
import json
from dataclasses import dataclass, field
from typing import Any, Mapping

from src.common.logger import get_logger

from .registry import builtin_tool
from ..mcp.tool import Tool
from ..mcp.types import ConcurrencyLevel, MCPCategory, RiskLevel, ToolType

logger = get_logger()

DEFAULT_MAX_RESULTS = 5
REQUEST_TIMEOUT = 10.0


@dataclass(frozen=True)
class SearchAPIConfig:
    """Configuration for the search API backend."""
    provider: str = "tavily"  # "tavily" | "serpapi" | "custom"
    api_key: str = ""
    base_url: str = ""
    extra_params: Mapping[str, Any] = field(default_factory=dict)


async def _search_tavily(
    query: str,
    max_results: int,
    config: SearchAPIConfig,
) -> list[dict[str, str]]:
    """Execute search via Tavily API."""
    import httpx

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        response = await client.post(
            "https://api.tavily.com/search",
            json={
                "api_key": config.api_key,
                "query": query,
                "max_results": max_results,
                "include_answer": True,
            },
        )
        response.raise_for_status()
        data = response.json()

    results = []
    if data.get("answer"):
        results.append({"title": "Summary", "url": "", "snippet": data["answer"]})

    for item in data.get("results", [])[:max_results]:
        results.append({
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "snippet": item.get("content", ""),
        })
    return results


async def _search_serpapi(
    query: str,
    max_results: int,
    config: SearchAPIConfig,
) -> list[dict[str, str]]:
    """Execute search via SerpAPI."""
    import httpx

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        response = await client.get(
            "https://serpapi.com/search",
            params={
                "api_key": config.api_key,
                "q": query,
                "num": max_results,
                "engine": "google",
            },
        )
        response.raise_for_status()
        data = response.json()

    results = []
    for item in data.get("organic_results", [])[:max_results]:
        results.append({
            "title": item.get("title", ""),
            "url": item.get("link", ""),
            "snippet": item.get("snippet", ""),
        })
    return results


async def _search_custom(
    query: str,
    max_results: int,
    config: SearchAPIConfig,
) -> list[dict[str, str]]:
    """Execute search via custom HTTP endpoint."""
    import httpx

    if not config.base_url:
        return [{"title": "Error", "url": "", "snippet": "No base_url configured for custom search"}]

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        response = await client.post(
            config.base_url,
            json={
                "query": query,
                "max_results": max_results,
                **dict(config.extra_params),
            },
        )
        response.raise_for_status()
        data = response.json()

    # Expect {"results": [{"title": ..., "url": ..., "snippet": ...}]}
    return [
        {
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "snippet": item.get("snippet", ""),
        }
        for item in data.get("results", [])[:max_results]
    ]


_SEARCH_BACKENDS = {
    "tavily": _search_tavily,
    "serpapi": _search_serpapi,
    "custom": _search_custom,
}


def _format_results(query: str, results: list[dict[str, str]]) -> str:
    """Format search results for LLM consumption."""
    if not results:
        return f"No results found for: {query}"

    lines = [f"Search results for: {query}\n"]
    for i, r in enumerate(results, 1):
        title = r.get("title", "Untitled")
        url = r.get("url", "")
        snippet = r.get("snippet", "")

        lines.append(f"{i}. {title}")
        if url:
            lines.append(f"   URL: {url}")
        if snippet:
            lines.append(f"   {snippet}")
        lines.append("")

    return "\n".join(lines).strip()


async def _web_search_handler(
    query: str,
    max_results: int = DEFAULT_MAX_RESULTS,
    *,
    _config: SearchAPIConfig,
) -> str:
    """Execute web search and return formatted results."""
    if not query or not query.strip():
        return "Error: Search query must not be empty"

    if not _config.api_key and _config.provider != "custom":
        return (
            f"Error: No API key configured for {_config.provider}. "
            f"Set search_api_key in settings."
        )

    max_results = min(max(1, max_results), 10)
    backend = _SEARCH_BACKENDS.get(_config.provider)
    if not backend:
        return f"Error: Unknown search provider: {_config.provider}"

    try:
        import httpx
    except ImportError:
        return "Error: httpx not installed. Run: pip install httpx"

    try:
        results = await backend(query, max_results, _config)
        formatted = _format_results(query, results)
        logger.info(
            f"[WebSearch] '{query}': {len(results)} results "
            f"via {_config.provider}"
        )
        return formatted

    except Exception as e:
        logger.error(f"[WebSearch] Failed for '{query}': {e}")
        return f"Error: Search failed: {e}"


@builtin_tool()
def create_web_search_tool(
    config: SearchAPIConfig | None = None,
) -> Tool:
    """Create the web_search Tool instance."""
    search_config = config or SearchAPIConfig()

    async def handler(
        query: str,
        max_results: int = DEFAULT_MAX_RESULTS,
    ) -> str:
        return await _web_search_handler(
            query, max_results, _config=search_config,
        )

    return Tool(
        name="web_search",
        description=(
            "Search the web for information. Returns a list of results "
            "with title, URL, and snippet. Use for finding current "
            "information, documentation, or research."
        ),
        handler=handler,
        parameters={
            "query": {
                "type": "string",
                "description": "Search query",
            },
            "max_results": {
                "type": "integer",
                "description": f"Maximum results to return (default: {DEFAULT_MAX_RESULTS}, max: 10)",
            },
        },
        required_params=("query",),
        tool_type=ToolType.READ,
        category=MCPCategory.SPECIALIZED,
        risk_level=RiskLevel.MEDIUM,
        concurrency=ConcurrencyLevel.SAFE,
        tags=frozenset({"web", "search", "internet", "query"}),
    )
