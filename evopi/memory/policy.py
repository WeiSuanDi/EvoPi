"""Memory governance — what should and should not be preserved."""

from __future__ import annotations

from evopi.memory.store import MemoryEntry, MemoryStore


class MemoryWritePolicy:
    """Guard that prevents storing sensitive or excessive content in memory.

    This is a standalone validator used by ContextProviders and tools, not a
    Policy hooked into the Engine.  It can be composed with any Policy that
    governs ``before_tool_call`` for a hypothetical ``remember`` tool.
    """

    def __init__(self, *, max_entry_chars: int = 5000, max_total_entries: int = 1000) -> None:
        self._max_entry_chars = max_entry_chars
        self._max_total_entries = max_total_entries

    def validate(self, entry: MemoryEntry, store: MemoryStore) -> str | None:
        """Return an error message if the entry should be rejected, or None."""
        if len(entry.content) > self._max_entry_chars:
            return (
                f"Memory entry too long ({len(entry.content)} chars, "
                f"max {self._max_entry_chars})"
            )
        if len(store) >= self._max_total_entries:
            return (
                f"Memory store full ({len(store)} entries, "
                f"max {self._max_total_entries})"
            )
        return None

    def is_sensitive(self, entry: MemoryEntry) -> bool:
        """Heuristic check for content that should never be persisted."""
        sensitive_markers = [
            "API_KEY",
            "sk-ant-",
            "sk-",
            "password",
            "secret",
            "token",
        ]
        lower = entry.content.lower()
        return any(marker.lower() in lower for marker in sensitive_markers)


__all__ = ["MemoryWritePolicy"]
