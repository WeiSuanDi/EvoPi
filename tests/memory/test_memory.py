"""Tests for the Memory module."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from evopi.core.context import AgentContext
from evopi.core.messages import UserMessage
from evopi.memory import (
    MemoryEntry,
    MemoryRetriever,
    MemoryStore,
    MemoryWritePolicy,
    TagFilteredRetriever,
)


# ---------------------------------------------------------------------------
# MemoryEntry
# ---------------------------------------------------------------------------

def test_entry_defaults() -> None:
    e = MemoryEntry(content="hello")
    assert e.content == "hello"
    assert e.tags == []
    assert len(e.id) == 32  # hex uuid4
    assert e.metadata == {}


def test_entry_matches_tag_case_insensitive() -> None:
    e = MemoryEntry(content="x", tags=["Important"])
    assert e.matches_tag("important")
    assert e.matches_tag("IMPORTANT")
    assert not e.matches_tag("other")


def test_entry_to_dict_round_trip() -> None:
    e = MemoryEntry(content="test", tags=["a", "b"], metadata={"source": "user"})
    d = e.to_dict()
    restored = MemoryEntry.from_dict(d)
    assert restored.content == "test"
    assert restored.tags == ["a", "b"]
    assert restored.id == e.id
    assert restored.metadata == {"source": "user"}


# ---------------------------------------------------------------------------
# MemoryStore
# ---------------------------------------------------------------------------

@pytest.fixture
def store() -> MemoryStore:
    return MemoryStore()


def test_add_and_get(store: MemoryStore) -> None:
    e = store.add(MemoryEntry(content="hello"))
    assert store.get(e.id) is not None
    assert store.get(e.id).content == "hello"  # type: ignore[union-attr]


def test_remove(store: MemoryStore) -> None:
    e = store.add(MemoryEntry(content="temp"))
    assert store.remove(e.id)
    assert store.get(e.id) is None
    assert not store.remove("nonexistent")


def test_len(store: MemoryStore) -> None:
    assert len(store) == 0
    store.add(MemoryEntry(content="a"))
    assert len(store) == 1


def test_all(store: MemoryStore) -> None:
    store.add(MemoryEntry(content="a"))
    store.add(MemoryEntry(content="b"))
    assert len(store.all()) == 2


def test_tag(store: MemoryStore) -> None:
    e = store.add(MemoryEntry(content="tag me"))
    result = store.tag(e.id, "important", "urgent")
    assert result is not None
    assert result.tags == ["important", "urgent"]
    # Duplicate tag not added
    store.tag(e.id, "important")
    assert store.get(e.id).tags == ["important", "urgent"]  # type: ignore[union-attr]


def test_tag_nonexistent(store: MemoryStore) -> None:
    assert store.tag("nonexistent", "tag") is None


def test_search_keyword(store: MemoryStore) -> None:
    store.add(MemoryEntry(content="User prefers pytest", tags=["pref"]))
    store.add(MemoryEntry(content="Project requires Python 3.12", tags=["env"]))
    # Single word
    assert len(store.search("pytest")) == 1
    # Multi-word query - "test" in "pytest"
    assert len(store.search("test framework")) == 1
    # No match
    assert len(store.search("javascript")) == 0


def test_search_with_tags_filter(store: MemoryStore) -> None:
    store.add(MemoryEntry(content="use pytest", tags=["testing"]))
    store.add(MemoryEntry(content="use python 3.12", tags=["env"]))
    results = store.search("use", tags=["testing"])
    assert len(results) == 1
    assert results[0].content == "use pytest"


def test_clear(store: MemoryStore) -> None:
    store.add(MemoryEntry(content="a"))
    store.clear()
    assert len(store) == 0


def test_persistence() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "memory.json"
        s1 = MemoryStore(path)
        s1.add(MemoryEntry(content="persist me", tags=["keep"]))
        # Re-open
        s2 = MemoryStore(path)
        assert len(s2) == 1
        e = s2.all()[0]
        assert e.content == "persist me"
        assert e.tags == ["keep"]


def test_thread_safety(store: MemoryStore) -> None:
    import threading

    def add_entry(i: int) -> None:
        store.add(MemoryEntry(content=f"entry-{i}"))

    threads = [threading.Thread(target=add_entry, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(store) == 20


# ---------------------------------------------------------------------------
# MemoryRetriever
# ---------------------------------------------------------------------------

def test_retriever_uses_last_user_message() -> None:
    store = MemoryStore()
    store.add(MemoryEntry(content="Use pytest for testing"))
    store.add(MemoryEntry(content="Deploy on Fridays"))

    ctx = AgentContext(
        messages=[
            UserMessage(content="hello"),
            UserMessage(content="What testing framework?"),
        ],
        tools=[],
    )
    retriever = MemoryRetriever(store)
    import asyncio
    results = asyncio.run(retriever.retrieve(ctx))
    assert len(results) == 1
    assert "pytest" in results[0].content


def test_retriever_no_user_message() -> None:
    store = MemoryStore()
    store.add(MemoryEntry(content="data"))
    ctx = AgentContext(messages=[], tools=[])
    retriever = MemoryRetriever(store)
    import asyncio
    results = asyncio.run(retriever.retrieve(ctx))
    assert results == []


def test_tag_filtered_retriever() -> None:
    store = MemoryStore()
    store.add(MemoryEntry(content="pytest config", tags=["testing"]))
    store.add(MemoryEntry(content="python version", tags=["env"]))

    ctx = AgentContext(
        messages=[UserMessage(content="what version")],
        tools=[],
    )
    retriever = TagFilteredRetriever(store, tags=["env"])
    import asyncio
    results = asyncio.run(retriever.retrieve(ctx))
    assert len(results) == 1
    assert "python" in results[0].content


# ---------------------------------------------------------------------------
# MemoryWritePolicy
# ---------------------------------------------------------------------------

def test_policy_validates_normal_entry() -> None:
    policy = MemoryWritePolicy()
    store = MemoryStore()
    assert policy.validate(MemoryEntry(content="ok"), store) is None


def test_policy_rejects_overlong_entry() -> None:
    policy = MemoryWritePolicy(max_entry_chars=100)
    store = MemoryStore()
    err = policy.validate(MemoryEntry(content="x" * 101), store)
    assert err is not None
    assert "too long" in err


def test_policy_rejects_full_store() -> None:
    policy = MemoryWritePolicy(max_total_entries=2)
    store = MemoryStore()
    store.add(MemoryEntry(content="a"))
    store.add(MemoryEntry(content="b"))
    err = policy.validate(MemoryEntry(content="c"), store)
    assert err is not None
    assert "full" in err


def test_policy_detects_sensitive_content() -> None:
    policy = MemoryWritePolicy()
    assert policy.is_sensitive(MemoryEntry(content="sk-ant-secret-key"))
    assert policy.is_sensitive(MemoryEntry(content="my_password = '123'"))
    assert not policy.is_sensitive(MemoryEntry(content="hello world"))
