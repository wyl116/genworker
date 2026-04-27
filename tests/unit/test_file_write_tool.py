# edition: baseline
"""
Unit tests for file write tool.

Tests:
- Write a new file and verify contents
- Overwrite an existing file
- Auto-create parent directories
- Path outside sandbox returns error
- Verify "Created" vs "Updated" in result message
"""
import os
from pathlib import Path

import pytest

from src.tools.builtin.file_write_tool import create_file_write_tool


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """Provide a temporary workspace directory."""
    return tmp_path


@pytest.fixture
def write_handler(workspace: Path):
    """Return the file_write tool handler bound to workspace."""
    tool = create_file_write_tool(str(workspace))
    return tool.handler


class TestFileWriteTool:
    """Tests for the file_write tool handler."""

    @pytest.mark.asyncio
    async def test_write_new_file(
        self, write_handler, workspace: Path
    ) -> None:
        """Write a new file and verify its contents on disk."""
        result = await write_handler(
            file_path="hello.txt", content="Hello, world!\n"
        )
        assert "Created" in result
        written = (workspace / "hello.txt").read_text()
        assert written == "Hello, world!\n"

    @pytest.mark.asyncio
    async def test_overwrite_existing_file(
        self, write_handler, workspace: Path
    ) -> None:
        """Overwriting an existing file reports 'Updated'."""
        target = workspace / "existing.txt"
        target.write_text("old content")

        result = await write_handler(
            file_path="existing.txt", content="new content"
        )
        assert "Updated" in result
        assert target.read_text() == "new content"

    @pytest.mark.asyncio
    async def test_auto_create_parent_directories(
        self, write_handler, workspace: Path
    ) -> None:
        """Parent directories are created automatically."""
        result = await write_handler(
            file_path="a/b/c/deep.txt", content="deep\n"
        )
        assert "Created" in result
        assert (workspace / "a" / "b" / "c" / "deep.txt").exists()
        assert (workspace / "a" / "b" / "c" / "deep.txt").read_text() == "deep\n"

    @pytest.mark.asyncio
    async def test_path_outside_sandbox_returns_error(
        self, workspace: Path
    ) -> None:
        """Path outside sandbox returns an error message."""
        inner = workspace / "inner"
        inner.mkdir()
        tool = create_file_write_tool(str(inner))
        result = await tool.handler(
            file_path="../escape.txt", content="should not write"
        )
        assert "Error" in result
        assert "escapes workspace" in result
        assert not (workspace / "escape.txt").exists()

    @pytest.mark.asyncio
    async def test_created_vs_updated_message(
        self, write_handler, workspace: Path
    ) -> None:
        """First write says 'Created', second write says 'Updated'."""
        first = await write_handler(file_path="toggle.txt", content="v1")
        assert "Created" in first

        second = await write_handler(file_path="toggle.txt", content="v2")
        assert "Updated" in second

    @pytest.mark.asyncio
    async def test_result_includes_byte_count(
        self, write_handler, workspace: Path
    ) -> None:
        """Result message includes bytes written."""
        content = "abc"
        result = await write_handler(file_path="bytes.txt", content=content)
        assert "Bytes written: 3" in result

    @pytest.mark.asyncio
    async def test_result_includes_line_count(
        self, write_handler, workspace: Path
    ) -> None:
        """Result message includes line count."""
        content = "line1\nline2\nline3\n"
        result = await write_handler(file_path="lines.txt", content=content)
        assert "Lines: 3" in result
