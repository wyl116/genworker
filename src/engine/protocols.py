"""
Engine protocols - type-safe abstractions for LLM and tool execution.

Engines depend on these Protocols, NOT concrete implementations.
This enables mock injection for testing and swapping backends.
"""
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from src.services.llm.intent import LLMCallIntent


@dataclass(frozen=True)
class ToolCall:
    """A single tool call requested by the LLM."""
    tool_name: str
    tool_input: dict[str, Any] = field(default_factory=dict)
    tool_call_id: str = ""


@dataclass(frozen=True)
class UsageInfo:
    """Token usage info from an LLM response."""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass(frozen=True)
class LLMResponse:
    """Immutable response from an LLM invocation."""
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    usage: UsageInfo = UsageInfo()


@dataclass(frozen=True)
class ToolResult:
    """Result from executing a single tool."""
    content: str = ""
    is_error: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class LLMClient(Protocol):
    """
    Protocol for LLM invocation.

    Engines call this to get LLM responses. Concrete implementations
    wrap LiteLLM, OpenAI, or any other LLM provider.
    """

    async def invoke(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        system_blocks: list[dict[str, Any]] | None = None,
        intent: LLMCallIntent | None = None,
    ) -> LLMResponse:
        """
        Invoke the LLM with messages and optional tool definitions.

        Args:
            messages: Chat messages in OpenAI format.
            tools: Tool definitions in OpenAI function calling format.
            tool_choice: Tool choice constraint ("auto", "none", or forced).
            system_blocks: Optional provider-specific system blocks.
            intent: Optional semantic routing intent for tier selection.

        Returns:
            LLMResponse with content and/or tool_calls.
        """
        ...


@runtime_checkable
class ToolExecutor(Protocol):
    """
    Protocol for tool execution.

    Engines call this to execute tool calls. Concrete implementation
    is ToolPipeline, but tests inject mocks.
    """

    async def execute(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
    ) -> ToolResult:
        """
        Execute a tool by name with given input.

        Args:
            tool_name: Name of the tool to execute.
            tool_input: Input parameters.

        Returns:
            ToolResult with output content.
        """
        ...
