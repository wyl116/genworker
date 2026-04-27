# edition: baseline
"""
Unit tests for file read tool.

Tests:
- Read a text file and verify line numbers in output
- Test offset parameter (skip first N lines)
- Test limit parameter (read only N lines)
- Binary file detection returns error
- File not found returns error
- Path outside sandbox returns error
"""
import os
from pathlib import Path

import pytest

from src.tools.builtin.file_read_tool import create_file_read_tool


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """Provide a temporary workspace directory."""
    return tmp_path


@pytest.fixture
def sample_file(workspace: Path) -> Path:
    """Create a sample text file with known content."""
    f = workspace / "sample.txt"
    lines = [f"Line {i}" for i in range(1, 11)]
    f.write_text("\n".join(lines) + "\n")
    return f


@pytest.fixture
def read_handler(workspace: Path):
    """Return the file_read tool handler bound to workspace."""
    tool = create_file_read_tool(str(workspace))
    return tool.handler


class TestFileReadTool:
    """Tests for the file_read tool handler."""

    @pytest.mark.asyncio
    async def test_read_file_with_line_numbers(
        self, read_handler, sample_file: Path
    ) -> None:
        """Read a text file and verify line numbers appear in output."""
        result = await read_handler(file_path=sample_file.name)
        assert "1\tLine 1" in result
        assert "10\tLine 10" in result
        assert "Lines 1-10 of 10" in result

    @pytest.mark.asyncio
    async def test_offset_skips_lines(
        self, read_handler, sample_file: Path
    ) -> None:
        """Offset parameter skips the first N lines."""
        result = await read_handler(file_path=sample_file.name, offset=5)
        # Line numbering starts at offset+1 = 6
        assert "6\tLine 6" in result
        assert "1\tLine 1" not in result

    @pytest.mark.asyncio
    async def test_limit_restricts_lines(
        self, read_handler, sample_file: Path
    ) -> None:
        """Limit parameter restricts how many lines are returned."""
        result = await read_handler(file_path=sample_file.name, limit=3)
        assert "1\tLine 1" in result
        assert "3\tLine 3" in result
        assert "4\tLine 4" not in result
        assert "Lines 1-3 of 10" in result

    @pytest.mark.asyncio
    async def test_offset_and_limit_combined(
        self, read_handler, sample_file: Path
    ) -> None:
        """Offset and limit work together correctly."""
        result = await read_handler(
            file_path=sample_file.name, offset=2, limit=3
        )
        assert "3\tLine 3" in result
        assert "5\tLine 5" in result
        assert "1\tLine 1" not in result
        assert "6\tLine 6" not in result

    @pytest.mark.asyncio
    async def test_binary_file_returns_error(
        self, read_handler, workspace: Path
    ) -> None:
        """Binary file detection returns an error message."""
        binary_file = workspace / "binary.dat"
        binary_file.write_bytes(b"\x00\x01\x02\x03\xff\xfe")
        result = await read_handler(file_path="binary.dat")
        assert "Error" in result
        assert "Binary file" in result

    @pytest.mark.asyncio
    async def test_file_not_found_returns_error(
        self, read_handler
    ) -> None:
        """Non-existent file returns an error message."""
        result = await read_handler(file_path="nonexistent.txt")
        assert "Error" in result
        assert "not found" in result

    @pytest.mark.asyncio
    async def test_path_outside_sandbox_returns_error(
        self, workspace: Path
    ) -> None:
        """Path outside sandbox returns an error message."""
        # Create a nested workspace so traversal escapes
        inner = workspace / "inner"
        inner.mkdir()
        tool = create_file_read_tool(str(inner))
        result = await tool.handler(file_path="../outside.txt")
        assert "Error" in result
        assert "escapes workspace" in result
