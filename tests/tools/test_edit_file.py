from __future__ import annotations

import asyncio
from pathlib import Path

from evopi.tools.builtins import create_edit_file_tool


def test_edit_file_replaces_one_exact_occurrence_atomically(tmp_path: Path) -> None:
    target = tmp_path / "demo.py"
    target.write_text("before\nold value\nafter\n", encoding="utf-8")
    tool = create_edit_file_tool(tmp_path)

    result = asyncio.run(
        tool.execute(
            {
                "path": "demo.py",
                "old_text": "old value",
                "new_text": "new value",
            }
        )
    )

    assert result.is_error is False
    assert target.read_text(encoding="utf-8") == "before\nnew value\nafter\n"
    assert tool.metadata["effects"] == ["write"]


def test_edit_file_rejects_zero_or_multiple_matches_without_writing(
    tmp_path: Path,
) -> None:
    target = tmp_path / "demo.txt"
    original = "repeat\nrepeat\n"
    target.write_text(original, encoding="utf-8")
    tool = create_edit_file_tool(tmp_path)

    multiple = asyncio.run(
        tool.execute(
            {"path": "demo.txt", "old_text": "repeat", "new_text": "changed"}
        )
    )
    missing = asyncio.run(
        tool.execute(
            {"path": "demo.txt", "old_text": "missing", "new_text": "changed"}
        )
    )

    assert multiple.is_error is True
    assert "2 occurrences" in multiple.content
    assert missing.is_error is True
    assert "not found" in missing.content
    assert target.read_text(encoding="utf-8") == original


def test_edit_file_rejects_workspace_escape(tmp_path: Path) -> None:
    tool = create_edit_file_tool(tmp_path)

    result = asyncio.run(
        tool.execute(
            {"path": "../outside.txt", "old_text": "a", "new_text": "b"}
        )
    )

    assert result.is_error is True
    assert "escapes workspace" in result.content


def test_edit_file_preserves_existing_crlf_newlines(tmp_path: Path) -> None:
    target = tmp_path / "windows.txt"
    target.write_bytes(b"before\r\nold\r\nafter\r\n")
    tool = create_edit_file_tool(tmp_path)

    result = asyncio.run(
        tool.execute(
            {"path": "windows.txt", "old_text": "old", "new_text": "new"}
        )
    )

    assert result.is_error is False
    assert target.read_bytes() == b"before\r\nnew\r\nafter\r\n"
