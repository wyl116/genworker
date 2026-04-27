# edition: baseline
"""
Tests for ToolDiscovery - tool categorization and tool_search meta-tool.
"""
import pytest

from src.tools.discovery import ToolDiscovery, DiscoveryResult
from src.tools.mcp.tool import Tool
from src.tools.mcp.types import MCPCategory, RiskLevel, ToolType


# --- Helpers ---

def _make_tool(name: str, description: str = "", tags: frozenset[str] = frozenset()) -> Tool:
    """Create a minimal tool for testing."""
    async def noop(**kw):
        return f"executed {name}"

    return Tool(
        name=name,
        description=description or f"Test tool {name}",
        handler=noop,
        tool_type=ToolType.CUSTOM,
        category=MCPCategory.GLOBAL,
        risk_level=RiskLevel.LOW,
        tags=tags,
    )


@pytest.fixture
def all_tools() -> tuple[Tool, ...]:
    return (
        _make_tool("parse_time", "Parse fuzzy time expressions", frozenset({"time", "datetime"})),
        _make_tool("bash_execute", "Execute shell commands in sandbox", frozenset({"bash", "shell"})),
        _make_tool("sql_executor", "Execute SQL queries on database", frozenset({"sql", "database"})),
        _make_tool("file_reader", "Read file contents", frozenset({"file", "read"})),
        _make_tool("http_client", "Make HTTP requests", frozenset({"http", "network"})),
    )


# --- Tests ---

class TestToolDiscovery:

    def test_core_tools_always_include_tool_search(self, all_tools):
        """tool_search meta-tool is always in core_tools."""
        discovery = ToolDiscovery(all_tools=all_tools)
        core_names = {t.name for t in discovery.core_tools}
        assert "tool_search" in core_names

    def test_recommended_tools_are_core(self, all_tools):
        """Tools listed in recommended_tool_names should be in core_tools."""
        discovery = ToolDiscovery(
            all_tools=all_tools,
            recommended_tool_names=frozenset({"parse_time", "bash_execute"}),
        )
        core_names = {t.name for t in discovery.core_tools}
        assert "parse_time" in core_names
        assert "bash_execute" in core_names
        assert "tool_search" in core_names

    def test_non_core_tools_are_deferred(self, all_tools):
        """Tools not in core_tool_names end up in deferred_tools."""
        discovery = ToolDiscovery(
            all_tools=all_tools,
            core_tool_names=frozenset({"tool_search"}),
        )
        deferred_names = {t.name for t in discovery.deferred_tools}
        assert "sql_executor" in deferred_names
        assert "file_reader" in deferred_names
        assert "http_client" in deferred_names
        # tool_search should NOT be in deferred
        assert "tool_search" not in deferred_names

    def test_search_matches_deferred_tools_by_keyword(self, all_tools):
        """search() should find deferred tools by keyword in name/description/tags."""
        discovery = ToolDiscovery(all_tools=all_tools)
        matches = discovery.search("database")
        match_names = {t.name for t in matches}
        assert "sql_executor" in match_names

    def test_search_matches_by_tag(self, all_tools):
        """search() should match tags."""
        discovery = ToolDiscovery(all_tools=all_tools)
        matches = discovery.search("http")
        match_names = {t.name for t in matches}
        assert "http_client" in match_names

    def test_search_empty_keyword_returns_nothing(self, all_tools):
        """Empty keyword returns no matches."""
        discovery = ToolDiscovery(all_tools=all_tools)
        assert discovery.search("") == ()
        assert discovery.search("   ") == ()

    def test_search_no_match_returns_empty(self, all_tools):
        """Non-matching keyword returns empty tuple."""
        discovery = ToolDiscovery(all_tools=all_tools)
        assert discovery.search("nonexistent_xyz_tool") == ()

    @pytest.mark.asyncio
    async def test_tool_search_returns_matching_tools(self, all_tools):
        """The tool_search handler should return text describing matching tools."""
        discovery = ToolDiscovery(all_tools=all_tools)

        # Find the tool_search tool
        tool_search = None
        for t in discovery.core_tools:
            if t.name == "tool_search":
                tool_search = t
                break
        assert tool_search is not None

        # Call the handler
        result = await tool_search.handler(keyword="database")
        assert "sql_executor" in result
        assert "Found" in result

    @pytest.mark.asyncio
    async def test_tool_search_no_match_message(self, all_tools):
        """tool_search handler returns 'No tools found' for non-matching keyword."""
        discovery = ToolDiscovery(all_tools=all_tools)

        tool_search = next(
            t for t in discovery.core_tools if t.name == "tool_search"
        )

        result = await tool_search.handler(keyword="nonexistent")
        assert "No tools found" in result

    def test_get_result_returns_discovery_result(self, all_tools):
        """get_result() returns a DiscoveryResult with correct structure."""
        discovery = ToolDiscovery(all_tools=all_tools)
        result = discovery.get_result()
        assert isinstance(result, DiscoveryResult)
        assert len(result.core_tools) > 0
        assert len(result.deferred_tools) > 0

    def test_all_original_tools_accounted_for(self, all_tools):
        """Every original tool should be in either core or deferred (tool_search is extra)."""
        discovery = ToolDiscovery(all_tools=all_tools)
        all_names = {t.name for t in all_tools}
        core_names = {t.name for t in discovery.core_tools}
        deferred_names = {t.name for t in discovery.deferred_tools}

        # All original tools should appear in core or deferred
        for name in all_names:
            assert name in core_names or name in deferred_names, (
                f"Tool '{name}' not in core or deferred"
            )

    def test_no_duplicates_between_core_and_deferred(self, all_tools):
        """No tool should appear in both core and deferred."""
        discovery = ToolDiscovery(all_tools=all_tools)
        core_names = {t.name for t in discovery.core_tools}
        deferred_names = {t.name for t in discovery.deferred_tools}
        overlap = core_names & deferred_names
        assert overlap == set(), f"Duplicate tools in core and deferred: {overlap}"

    def test_hidden_from_llm_tools_are_excluded_from_discovery(self):
        hidden_tool = _make_tool(
            "fetch_metrics",
            "Reusable hidden script",
            frozenset({"hidden_from_llm", "script"}),
        )
        visible_tool = _make_tool("visible_tool", "Visible helper")

        discovery = ToolDiscovery(all_tools=(hidden_tool, visible_tool))

        discovered_names = {tool.name for tool in discovery.core_tools + discovery.deferred_tools}
        assert "fetch_metrics" not in discovered_names
        assert "visible_tool" in discovered_names
