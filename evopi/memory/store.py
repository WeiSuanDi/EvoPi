"""Persistent key-value memory store with JSON file backing.

Memory is a long-term, queryable store of facts and learnings that the agent
can write to and read from.  It complements the Session (short-term conversation
history) and Trace (execution record).

Entries are versionless text blobs with metadata tags.  The store is thread-safe
and can optionally persist to a JSON file.
"""

from __future__ import annotations

import json
import os
import re
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from collections.abc import Iterator
from typing import Any, BinaryIO, Mapping
from uuid import uuid4

MEMORY_SCHEMA_VERSION = 1


class MemoryPersistenceError(RuntimeError):
    """Raised when Memory cannot be loaded or durably persisted."""

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
        entry_id = data.get("id")
        content = data.get("content")
        tags = data.get("tags", [])
        created_at = data.get("created_at")
        metadata = data.get("metadata", {})
        if not isinstance(entry_id, str) or not entry_id:
            raise ValueError("Memory entry id must be a non-empty string")
        if not isinstance(content, str):
            raise ValueError("Memory entry content must be a string")
        if not isinstance(tags, list) or any(
            not isinstance(tag, str) for tag in tags
        ):
            raise ValueError("Memory entry tags must be an array of strings")
        if not isinstance(created_at, str):
            raise ValueError("Memory entry created_at must be a string")
        if not isinstance(metadata, dict):
            raise ValueError("Memory entry metadata must be an object")
        parsed_at = datetime.fromisoformat(created_at)
        if parsed_at.tzinfo is None:
            raise ValueError("Memory entry created_at must include timezone")
        return cls(
            id=entry_id,
            content=content,
            tags=list(tags),
            created_at=parsed_at,
            metadata=dict(metadata),
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
            previous = self._entries.get(entry.id)
            self._entries[entry.id] = entry
            try:
                self._save()
            except Exception:
                if previous is None:
                    self._entries.pop(entry.id, None)
                else:
                    self._entries[entry.id] = previous
                raise
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
        words = _query_terms(query)
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
        temporary = self._path.with_name(f".{self._path.name}.{uuid4().hex}.tmp")
        try:
            data = {
                "schema_version": MEMORY_SCHEMA_VERSION,
                "entries": [entry.to_dict() for entry in self._entries.values()],
            }
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with temporary.open("x", encoding="utf-8", newline="\n") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            with _memory_file_lock(self._path.with_suffix(self._path.suffix + ".lock")):
                os.replace(temporary, self._path)
        except MemoryPersistenceError:
            temporary.unlink(missing_ok=True)
            raise
        except (OSError, TypeError, ValueError) as exc:
            temporary.unlink(missing_ok=True)
            raise MemoryPersistenceError(f"memory could not be persisted: {exc}") from exc

    def _load(self) -> None:
        if self._path is None or not self._path.exists():
            return
        try:
            text = self._path.read_text(encoding="utf-8")
            raw = json.loads(text)
            if isinstance(raw, list):
                items = raw
            elif (
                isinstance(raw, dict)
                and raw.get("schema_version") == MEMORY_SCHEMA_VERSION
                and isinstance(raw.get("entries"), list)
            ):
                items = raw["entries"]
            else:
                raise MemoryPersistenceError("invalid memory file schema")
            for item in items:
                entry = MemoryEntry.from_dict(item)
                self._entries[entry.id] = entry
        except MemoryPersistenceError:
            raise
        except (json.JSONDecodeError, KeyError, TypeError, ValueError, OSError) as exc:
            raise MemoryPersistenceError(f"invalid memory file: {exc}") from exc


def _query_terms(query: str) -> list[str]:
    lowered = query.casefold()
    terms = [word for word in re.findall(r"[\w.-]+", lowered) if len(word) >= 2]
    cjk_runs = re.findall(r"[\u3400-\u9fff]+", lowered)
    for run in cjk_runs:
        terms.extend(run[index : index + 2] for index in range(len(run) - 1))
    return list(dict.fromkeys(terms))


@contextmanager
def _memory_file_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle: BinaryIO = path.open("a+b")
    try:
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import importlib

            fcntl: Any = importlib.import_module("fcntl")
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    except OSError as exc:
        raise MemoryPersistenceError(
            f"Memory is locked by another process: {path}"
        ) from exc
    finally:
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import importlib

                fcntl = importlib.import_module("fcntl")
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        handle.close()


__all__ = [
    "MEMORY_SCHEMA_VERSION",
    "MemoryEntry",
    "MemoryPersistenceError",
    "MemoryStore",
]
