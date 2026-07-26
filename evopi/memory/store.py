"""Persistent key-value memory store with JSON file backing.

Memory is a long-term, queryable store of facts and learnings that the agent
can write to and read from.  It complements the Session (short-term conversation
history) and Trace (execution record).

Entries are versionless text blobs with metadata tags.  The store is thread-safe
and can optionally persist to a JSON file.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4


# ---------------------------------------------------------------------------
# MemoryEntry
# ---------------------------------------------------------------------------


@dataclass(slots=True, kw_only=True)
class MemoryEntry:
    content: str
    tags: list[str] = field(default_factory=list)
    id: str = field(default_factory=lambda: uuid4().hex)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    metadata: dict[str, Any] = field(default_factory=dict)

    def matches_tag(self, tag: str) -> bool:
        return any(t.lower() == tag.lower() for t in self.tags)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "content": self.content,
            "tags": self.tags,
            "created_at": self.created_at.isoformat(),
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> MemoryEntry:
        return cls(
            id=data["id"],
            content=data["content"],
            tags=list(data.get("tags", [])),
            created_at=datetime.fromisoformat(data["created_at"]),
            metadata=dict(data.get("metadata", {})),
        )


# ---------------------------------------------------------------------------
# MemoryStore
# ---------------------------------------------------------------------------


class MemoryStore:
    """Thread-safe, optionally persisted memory storage.

    Usage::

        store = MemoryStore(Path(".evopi/memory.json"))
        store.add(MemoryEntry(content="User prefers pytest", tags=["preference"]))
        results = store.search("pytest")  # simple keyword match
        store.remove(entry_id)
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = Path(path) if path is not None else None
        self._entries: dict[str, MemoryEntry] = {}
        self._lock = threading.Lock()
        if self._path is not None and self._path.exists():
            self._load()

    # -- CRUD -----------------------------------------------------------------

    def add(self, entry: MemoryEntry) -> MemoryEntry:
        with self._lock:
            self._entries[entry.id] = entry
            self._save()
        return entry

    def get(self, entry_id: str) -> MemoryEntry | None:
        with self._lock:
            return self._entries.get(entry_id)

    def remove(self, entry_id: str) -> bool:
        with self._lock:
            if entry_id in self._entries:
                del self._entries[entry_id]
                self._save()
                return True
            return False

    def search(
        self,
        query: str,
        *,
        tags: list[str] | None = None,
        limit: int = 10,
    ) -> list[MemoryEntry]:
        """Keyword-based search.

        Splits *query* into words and returns entries whose content or tags
        contain any query word (case-insensitive).  When *tags* is provided,
        only entries matching at least one tag are considered.
        """
        words = [w.lower() for w in query.split() if len(w) >= 2]
        results: list[MemoryEntry] = []
        with self._lock:
            candidates = list(self._entries.values())
        for entry in candidates:
            if tags and not any(entry.matches_tag(t) for t in tags):
                continue
            for word in words:
                if word in entry.content.lower() or any(
                    word in tag.lower() for tag in entry.tags
                ):
                    results.append(entry)
                    break
        return results[:limit]

    def all(self) -> list[MemoryEntry]:
        with self._lock:
            return list(self._entries.values())

    def tag(self, entry_id: str, *tags: str) -> MemoryEntry | None:
        with self._lock:
            entry = self._entries.get(entry_id)
            if entry is None:
                return None
            for tag in tags:
                if tag not in entry.tags:
                    entry.tags.append(tag)
            self._save()
            return entry

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._save()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    # -- persistence ----------------------------------------------------------

    def _save(self) -> None:
        if self._path is None:
            return
        try:
            data = [entry.to_dict() for entry in self._entries.values()]
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self._path)
        except OSError:
            pass  # best-effort persistence

    def _load(self) -> None:
        if self._path is None or not self._path.exists():
            return
        try:
            text = self._path.read_text(encoding="utf-8")
            raw = json.loads(text)
            for item in raw:
                entry = MemoryEntry.from_dict(item)
                self._entries[entry.id] = entry
        except (json.JSONDecodeError, KeyError, OSError):
            pass


__all__ = ["MemoryEntry", "MemoryStore"]
