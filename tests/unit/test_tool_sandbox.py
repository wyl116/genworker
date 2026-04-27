# edition: baseline
"""
Tests for tool sandbox - filter_tools pure function and ScopedToolExecutor.
"""
import pytest

from src.tools.mcp.tool import Tool
from src.tools.mcp.types import MCPCategory, RiskLevel, ToolType
from src.tools.sandbox import (
    PermissionDenial,
    ScopedToolExecutor,
    TenantPolicy,
    ToolPolicy,
    filter_tools,
)
from src.tools.formatters import ToolResult


# --- Fixtures ---

def _make_tool(name: str, **kwargs) -> Tool:
    """Create a minimal tool for testing."""
    async def noop(**kw):
        return f"executed {name}"

    return Tool(
        name=name,
        description=f"Test tool {name}",
        handler=kwargs.get("handler", noop),
        tool_type=ToolType.CUSTOM,
        category=MCPCategory.GLOBAL,
        risk_level=RiskLevel.LOW,
        **{k: v for k, v in kwargs.items() if k != "handler"},
    )


@pytest.fixture
def sample_tools() -> tuple[Tool, ...]:
    return (
        _make_tool("tool_a"),
        _make_tool("tool_b"),
        _make_tool("tool_c"),
        _make_tool("tool_d"),
        _make_tool("tool_e"),
    )


# --- filter_tools tests ---

class TestFilterTools:

    def test_blacklist_filters_denied_tools(self, sample_tools):
        """Blacklist mode removes denied tools, keeps the rest."""
        policy = ToolPolicy(
            mode="blacklist",
            denied_tools=frozenset({"tool_b", "tool_d"}),
        )
        result = filter_tools(sample_tools, policy)
        names = {t.name for t in result}
        assert names == {"tool_a", "tool_c", "tool_e"}

    def test_blacklist_empty_denied_keeps_all(self, sample_tools):
        """Blacklist with empty denied list keeps all tools."""
        policy = ToolPolicy(mode="blacklist", denied_tools=frozenset())
        result = filter_tools(sample_tools, policy)
        assert len(result) == 5

    def test_whitelist_only_allows_declared_tools(self, sample_tools):
        """Whitelist mode only keeps explicitly allowed tools."""
        policy = ToolPolicy(
            mode="whitelist",
            allowed_tools=frozenset({"tool_a", "tool_c"}),
        )
        result = filter_tools(sample_tools, policy)
        names = {t.name for t in result}
        assert names == {"tool_a", "tool_c"}

    def test_whitelist_empty_allowed_returns_nothing(self, sample_tools):
        """Whitelist with empty allowed list returns no tools."""
        policy = ToolPolicy(mode="whitelist", allowed_tools=frozenset())
        result = filter_tools(sample_tools, policy)
        assert len(result) == 0

    def test_tenant_denied_tools_overlay(self, sample_tools):
        """Tenant overlay further removes tools from blacklist result."""
        policy = ToolPolicy(
            mode="blacklist",
            denied_tools=frozenset({"tool_b"}),
        )
        tenant = TenantPolicy(denied_tools=frozenset({"tool_d", "tool_e"}))
        result = filter_tools(sample_tools, policy, tenant)
        names = {t.name for t in result}
        assert names == {"tool_a", "tool_c"}

    def test_tenant_overlay_with_whitelist(self, sample_tools):
        """Tenant overlay works on whitelist mode too."""
        policy = ToolPolicy(
            mode="whitelist",
            allowed_tools=frozenset({"tool_a", "tool_b", "tool_c"}),
        )
        tenant = TenantPolicy(denied_tools=frozenset({"tool_b"}))
        result = filter_tools(sample_tools, policy, tenant)
        names = {t.name for t in result}
        assert names == {"tool_a", "tool_c"}

    def test_tenant_none_has_no_effect(self, sample_tools):
        """None tenant policy has no effect."""
        policy = ToolPolicy(mode="blacklist", denied_tools=frozenset())
        result = filter_tools(sample_tools, policy, None)
        assert len(result) == 5

    def test_pure_function_no_mutation(self, sample_tools):
        """filter_tools does not mutate inputs."""
        original_len = len(sample_tools)
        policy = ToolPolicy(mode="blacklist", denied_tools=frozenset({"tool_a"}))
        filter_tools(sample_tools, policy)
        assert len(sample_tools) == original_len

    def test_returns_tuple(self, sample_tools):
        """filter_tools returns a tuple (immutable)."""
        policy = ToolPolicy(mode="blacklist")
        result = filter_tools(sample_tools, policy)
        assert isinstance(result, tuple)


# --- ScopedToolExecutor tests ---

class TestScopedToolExecutor:

    @pytest.mark.asyncio
    async def test_execute_allowed_tool(self):
        """Allowed tool executes successfully."""
        tool = _make_tool("my_tool")
        executor = ScopedToolExecutor(allowed_tools={"my_tool": tool})
        result = await executor.execute("my_tool", {})
        assert not result.is_error
        assert "executed my_tool" in result.content

    @pytest.mark.asyncio
    async def test_permission_denial_for_disallowed_tool(self):
        """Disallowed tool returns PermissionDenial as ToolResult, not exception."""
        tool = _make_tool("my_tool")
        executor = ScopedToolExecutor(allowed_tools={"my_tool": tool})
        result = await executor.execute("other_tool", {})
        assert result.is_error
        assert "not in the allowed" in result.content

    @pytest.mark.asyncio
    async def test_permission_denial_is_data_object_not_exception(self):
        """Verify PermissionDenial is a frozen dataclass, not an exception."""
        denial = PermissionDenial(
            tool_name="blocked_tool",
            reason="Not allowed",
            context="sandbox",
        )
        assert not isinstance(denial, Exception)
        assert denial.tool_name == "blocked_tool"
        # Verify frozen
        with pytest.raises(AttributeError):
            denial.tool_name = "other"

    @pytest.mark.asyncio
    async def test_handler_exception_returns_error_result(self):
        """Handler exception is caught and returned as error ToolResult."""
        async def failing_handler(**kw):
            raise ValueError("test error")

        tool = _make_tool("fail_tool", handler=failing_handler)
        executor = ScopedToolExecutor(allowed_tools={"fail_tool": tool})
        result = await executor.execute("fail_tool", {})
        assert result.is_error
        assert "ValueError" in result.content

    @pytest.mark.asyncio
    async def test_sync_handler_supported(self):
        """Synchronous handler works correctly."""
        def sync_handler(**kw):
            return "sync result"

        tool = _make_tool("sync_tool", handler=sync_handler)
        executor = ScopedToolExecutor(allowed_tools={"sync_tool": tool})
        result = await executor.execute("sync_tool", {})
        assert not result.is_error
        assert "sync result" in result.content

    @pytest.mark.asyncio
    async def test_empty_executor_denies_all(self):
        """Executor with no allowed tools denies everything."""
        executor = ScopedToolExecutor(allowed_tools={})
        result = await executor.execute("any_tool", {})
        assert result.is_error
