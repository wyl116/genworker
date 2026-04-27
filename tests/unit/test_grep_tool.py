# edition: baseline
"""
Unit tests for the grep_search tool.

Tests cover regex matching, include filter, sandbox enforcement,
empty results, and max_results limiting.
"""
from __future__ import annotations

import pytest

from src.tools.builtin.grep_tool import _grep_handler, create_grep_tool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _create_files(base, file_contents: dict[str, str]) -> None:
    """Create files with given contents under base."""
    for path, content in file_contents.items():
        fp = base / path
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_find_regex_pattern(tmp_path):
    """Regex pattern matches lines and returns file:line:content format."""
    _create_files(tmp_path, {
        "src/app.py": "import os\ndef hello():\n    print('hello world')\n",
        "src/util.py": "def helper():\n    return 42\n",
    })

    result = await _grep_handler("def \\w+", "", "", 100, _workspace_root=str(tmp_path))

    assert "Found matches" in result
    # Should find 'def hello()' and 'def helper()'
    assert "hello" in result
    assert "helper" in result
    # Check file:line:content format (relative paths)
    lines = result.strip().split("\n")
    match_lines = [ln for ln in lines[1:] if ln.strip()]
    for line in match_lines:
        parts = line.split(":")
        assert len(parts) >= 3, f"Expected file:line:content format, got: {line}"


@pytest.mark.asyncio
async def test_include_filter(tmp_path):
    """Include filter limits search to matching file types."""
    _create_files(tmp_path, {
        "app.py": "TODO: fix this\n",
        "notes.txt": "TODO: read this\n",
        "data.json": '{"TODO": true}\n',
    })

    result = await _grep_handler(
        "TODO", "", "*.py", 100, _workspace_root=str(tmp_path),
    )

    assert "Found matches" in result
    assert "app.py" in result
    assert "notes.txt" not in result


@pytest.mark.asyncio
async def test_no_matches_returns_message(tmp_path):
    """When pattern has no matches, a descriptive message is returned."""
    _create_files(tmp_path, {"file.py": "nothing interesting\n"})

    result = await _grep_handler(
        "NONEXISTENT_PATTERN_XYZ", "", "", 100, _workspace_root=str(tmp_path),
    )

    assert "No matches found" in result


@pytest.mark.asyncio
async def test_path_outside_sandbox_returns_error(tmp_path):
    """A path outside the workspace returns an error."""
    result = await _grep_handler(
        "test", "/etc", "", 100, _workspace_root=str(tmp_path),
    )

    assert result.startswith("Error:")


@pytest.mark.asyncio
async def test_max_results_limits_output(tmp_path):
    """max_results parameter limits the number of returned matches."""
    # Create a file with many matching lines
    content = "\n".join(f"match_line_{i}" for i in range(50))
    _create_files(tmp_path, {"big.txt": content})

    result = await _grep_handler(
        "match_line", "", "", 5, _workspace_root=str(tmp_path),
    )

    assert "Found matches" in result
    lines = result.strip().split("\n")
    match_lines = [ln for ln in lines[1:] if ln.strip()]
    assert len(match_lines) <= 5


@pytest.mark.asyncio
async def test_subdirectory_search(tmp_path):
    """Search scoped to a subdirectory only finds files there."""
    _create_files(tmp_path, {
        "root.py": "target_word\n",
        "sub/inner.py": "target_word\n",
    })

    result = await _grep_handler(
        "target_word", "sub", "", 100, _workspace_root=str(tmp_path),
    )

    assert "Found matches" in result
    assert "inner.py" in result
    # root.py is outside 'sub', should not appear
    assert "root.py" not in result


@pytest.mark.asyncio
async def test_create_grep_tool_returns_tool(tmp_path):
    """create_grep_tool returns a Tool with correct metadata."""
    tool = create_grep_tool(str(tmp_path))

    assert tool.name == "grep_search"
    assert tool.is_concurrent_safe  # SEARCH type
