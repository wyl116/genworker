# edition: baseline
"""
Unit tests for the glob_search tool.

Tests cover pattern matching, subdirectory search, sandbox enforcement,
empty results, and max-results truncation.
"""
from __future__ import annotations

import pytest

from src.tools.builtin.glob_tool import _glob_handler, create_glob_tool, MAX_RESULTS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _create_files(base, paths: list[str]) -> None:
    """Create files (with intermediate dirs) under base."""
    for p in paths:
        fp = base / p
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(f"# {p}\n")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_find_python_files(tmp_path):
    """**/*.py pattern finds Python files recursively."""
    _create_files(tmp_path, [
        "main.py",
        "src/foo.py",
        "src/bar.py",
        "data/notes.txt",
    ])

    result = await _glob_handler("**/*.py", "", _workspace_root=str(tmp_path))

    assert "Found 3 file(s)" in result
    assert "main.py" in result
    assert "foo.py" in result
    assert "bar.py" in result
    assert "notes.txt" not in result


@pytest.mark.asyncio
async def test_find_files_in_subdirectory(tmp_path):
    """Search limited to a subdirectory via the path parameter."""
    _create_files(tmp_path, [
        "root.py",
        "sub/a.py",
        "sub/b.py",
        "other/c.py",
    ])

    result = await _glob_handler("*.py", "sub", _workspace_root=str(tmp_path))

    assert "Found 2 file(s)" in result
    assert "a.py" in result
    assert "b.py" in result
    assert "root.py" not in result
    assert "c.py" not in result


@pytest.mark.asyncio
async def test_no_matches_returns_message(tmp_path):
    """When no files match, result contains 'no matches'."""
    _create_files(tmp_path, ["file.txt"])

    result = await _glob_handler("*.rs", "", _workspace_root=str(tmp_path))

    assert "Found 0 file(s)" in result
    assert "no matches" in result


@pytest.mark.asyncio
async def test_path_outside_sandbox_returns_error(tmp_path):
    """A path that escapes the workspace root returns an error."""
    result = await _glob_handler(
        "*.py", "/etc", _workspace_root=str(tmp_path),
    )

    assert result.startswith("Error:")


@pytest.mark.asyncio
async def test_traversal_path_returns_error(tmp_path):
    """Path traversal (../) returns an error."""
    result = await _glob_handler(
        "*.py", "../../../etc", _workspace_root=str(tmp_path),
    )

    assert result.startswith("Error:")


@pytest.mark.asyncio
async def test_max_results_truncation(tmp_path):
    """When matches exceed MAX_RESULTS, output is truncated."""
    count = MAX_RESULTS + 10
    _create_files(tmp_path, [f"file_{i:04d}.txt" for i in range(count)])

    result = await _glob_handler("*.txt", "", _workspace_root=str(tmp_path))

    assert f"Found {count} file(s)" in result
    assert f"showing first {MAX_RESULTS}" in result

    # Count listed files (one per line after the header)
    lines = result.strip().split("\n")
    file_lines = [ln for ln in lines[1:] if ln.strip()]
    assert len(file_lines) == MAX_RESULTS


@pytest.mark.asyncio
async def test_create_glob_tool_returns_tool(tmp_path):
    """create_glob_tool returns a Tool with the correct name and type."""
    tool = create_glob_tool(str(tmp_path))

    assert tool.name == "glob_search"
    assert tool.is_concurrent_safe  # SEARCH type


@pytest.mark.asyncio
async def test_create_glob_tool_handler_works(tmp_path):
    """The handler created by create_glob_tool can be called directly."""
    _create_files(tmp_path, ["hello.py"])

    tool = create_glob_tool(str(tmp_path))
    result = await tool.handler(pattern="*.py")

    assert "Found 1 file(s)" in result
    assert "hello.py" in result
