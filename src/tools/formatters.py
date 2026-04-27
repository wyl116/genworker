"""
ToolResult - Immutable result container for tool execution output.

Used by ScopedToolExecutor to wrap raw tool output into a
structured, LLM-friendly format.
"""
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass(frozen=True)
class ToolResult:
    """
    Immutable tool execution result.

    content: LLM-friendly text representation of the result.
    is_error: Whether this result represents an error.
    truncated: Whether the content was truncated.
    original_length: Original length before truncation (if applicable).
    """
    content: str
    is_error: bool = False
    truncated: bool = False
    original_length: Optional[int] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_sql(
        cls,
        rows: list[dict],
        total: int,
        max_rows: int = 50,
    ) -> "ToolResult":
        """
        Format SQL query results with truncation for large result sets.

        Args:
            rows: Query result rows.
            total: Total row count from the query.
            max_rows: Maximum rows to include in output.
        """
        display_rows = rows[:max_rows]
        formatted = _format_rows(display_rows)

        if total > max_rows:
            return cls(
                content=f"{formatted}\n... total {total} rows, showing first {max_rows}",
                truncated=True,
                original_length=total,
            )
        return cls(content=formatted)

    @classmethod
    def from_error(
        cls,
        error: Exception,
        suggestion: str = "",
    ) -> "ToolResult":
        """
        Format an error into a structured result with optional suggestion.

        Args:
            error: The exception that occurred.
            suggestion: Optional fix suggestion for the LLM.
        """
        msg = f"Error: {type(error).__name__}: {error}"
        if suggestion:
            msg += f"\nSuggestion: {suggestion}"
        return cls(content=msg, is_error=True)

    @classmethod
    def from_text(cls, text: str, max_length: int = 50000) -> "ToolResult":
        """
        Format a text result with length truncation.

        Args:
            text: Raw text output.
            max_length: Maximum character length.
        """
        if len(text) > max_length:
            return cls(
                content=text[:max_length] + "\n... [output truncated]",
                truncated=True,
                original_length=len(text),
            )
        return cls(content=text)


def _format_rows(rows: list[dict]) -> str:
    """Format a list of dict rows into a readable table string."""
    if not rows:
        return "(no results)"

    headers = list(rows[0].keys())
    col_widths = {
        h: max(len(str(h)), *(len(str(r.get(h, ""))) for r in rows))
        for h in headers
    }

    header_line = " | ".join(str(h).ljust(col_widths[h]) for h in headers)
    separator = "-+-".join("-" * col_widths[h] for h in headers)

    lines = [header_line, separator]
    for row in rows:
        line = " | ".join(
            str(row.get(h, "")).ljust(col_widths[h]) for h in headers
        )
        lines.append(line)

    return "\n".join(lines)
