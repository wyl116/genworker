# edition: baseline
"""
Unit tests for file edit tool.

Tests:
- Replace a unique string
- old_string not found returns error
- Multiple matches without replace_all returns error
- replace_all=True replaces all occurrences
- old_string == new_string returns error
- File not found returns error
- Path outside sandbox returns error
"""
import os
from pathlib import Path

import pytest

from src.tools.builtin.file_edit_tool import create_file_edit_tool


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """Provide a temporary workspace directory."""
    return tmp_path


@pytest.fixture
def edit_handler(workspace: Path):
    """Return the file_edit tool handler bound to workspace."""
    tool = create_file_edit_tool(str(workspace))
    return tool.handler


def _write_file(workspace: Path, name: str, content: str) -> Path:
    """Helper to write a file in the workspace."""
    f = workspace / name
    f.write_text(content)
    return f


class TestFileEditTool:
    """Tests for the file_edit tool handler."""

    @pytest.mark.asyncio
    async def test_replace_unique_string(
        self, edit_handler, workspace: Path
    ) -> None:
        """Replace a unique string and verify file content."""
        _write_file(workspace, "code.py", "foo = 1\nbar = 2\n")
        result = await edit_handler(
            file_path="code.py",
            old_string="foo = 1",
            new_string="foo = 42",
        )
        assert "Edited" in result
        assert "Replacements: 1" in result
        assert (workspace / "code.py").read_text() == "foo = 42\nbar = 2\n"

    @pytest.mark.asyncio
    async def test_old_string_not_found_returns_error(
        self, edit_handler, workspace: Path
    ) -> None:
        """old_string not found returns an error message."""
        _write_file(workspace, "data.txt", "alpha\nbeta\n")
        result = await edit_handler(
            file_path="data.txt",
            old_string="gamma",
            new_string="delta",
        )
        assert "Error" in result
        assert "not found" in result

    @pytest.mark.asyncio
    async def test_multiple_matches_without_replace_all_returns_error(
        self, edit_handler, workspace: Path
    ) -> None:
        """Multiple matches without replace_all returns an error."""
        _write_file(workspace, "dup.txt", "aaa\nbbb\naaa\n")
        result = await edit_handler(
            file_path="dup.txt",
            old_string="aaa",
            new_string="ccc",
        )
        assert "Error" in result
        assert "2 times" in result

    @pytest.mark.asyncio
    async def test_replace_all_replaces_all_occurrences(
        self, edit_handler, workspace: Path
    ) -> None:
        """replace_all=True replaces every occurrence."""
        _write_file(workspace, "multi.txt", "x x x\n")
        result = await edit_handler(
            file_path="multi.txt",
            old_string="x",
            new_string="y",
            replace_all=True,
        )
        assert "Edited" in result
        assert "Replacements: 3" in result
        assert (workspace / "multi.txt").read_text() == "y y y\n"

    @pytest.mark.asyncio
    async def test_identical_strings_returns_error(
        self, edit_handler, workspace: Path
    ) -> None:
        """old_string == new_string returns an error."""
        _write_file(workspace, "same.txt", "hello\n")
        result = await edit_handler(
            file_path="same.txt",
            old_string="hello",
            new_string="hello",
        )
        assert "Error" in result
        assert "identical" in result

    @pytest.mark.asyncio
    async def test_file_not_found_returns_error(
        self, edit_handler
    ) -> None:
        """Non-existent file returns an error message."""
        result = await edit_handler(
            file_path="ghost.txt",
            old_string="a",
            new_string="b",
        )
        assert "Error" in result
        assert "not found" in result

    @pytest.mark.asyncio
    async def test_path_outside_sandbox_returns_error(
        self, workspace: Path
    ) -> None:
        """Path outside sandbox returns an error message."""
        inner = workspace / "inner"
        inner.mkdir()
        tool = create_file_edit_tool(str(inner))
        result = await tool.handler(
            file_path="../escape.txt",
            old_string="a",
            new_string="b",
        )
        assert "Error" in result
        assert "escapes workspace" in result

    @pytest.mark.asyncio
    async def test_multiline_replacement(
        self, edit_handler, workspace: Path
    ) -> None:
        """Multiline old_string replacement works correctly."""
        _write_file(workspace, "multi.txt", "line1\nline2\nline3\n")
        result = await edit_handler(
            file_path="multi.txt",
            old_string="line1\nline2",
            new_string="replaced",
        )
        assert "Edited" in result
        assert (workspace / "multi.txt").read_text() == "replaced\nline3\n"
