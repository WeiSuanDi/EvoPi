"""Tests for Session branch, fork, compact, and multi-leaf operations."""

import tempfile
from pathlib import Path

import pytest

from evopi.session import (
    BranchEntry,
    CompactEntry,
    SessionError,
    SessionManager,
)


# ---------------------------------------------------------------------------
# Multi-leaf — basic
# ---------------------------------------------------------------------------

def test_single_leaf_session_has_one_leaf() -> None:
    session = SessionManager.in_memory()
    session.append_run_start(
        run_id="00000000-0000-0000-0000-000000000001",
        runtime_fingerprint=_dummy_fingerprint(),
    )
    session.append_run_end(
        run_id="00000000-0000-0000-0000-000000000001",
        reason="completed",
    )
    assert session.leaf_id is not None
    assert len(session.leaves()) == 1


def test_branch_creates_new_leaf() -> None:
    session = SessionManager.in_memory()
    session.append_run_start(
        run_id="00000000-0000-0000-0000-000000000001",
        runtime_fingerprint=_dummy_fingerprint(),
    )
    run_end = session.append_run_end(
        run_id="00000000-0000-0000-0000-000000000001",
        reason="completed",
    )
    session.create_checkpoint(
        run_end=run_end,
        runtime_fingerprint=_dummy_fingerprint(),
    )

    # Branch from the checkpoint
    branch = session.branch(from_entry_id=session.leaf_id)
    assert isinstance(branch, BranchEntry)
    assert branch.parent_id is not None
    assert len(session.leaves()) == 1  # old leaf replaced by branch
    assert session.leaf_id == branch.entry_id


def test_multiple_branches_create_multiple_leaves() -> None:
    session = SessionManager.in_memory()
    session.append_run_start(
        run_id="00000000-0000-0000-0000-000000000001",
        runtime_fingerprint=_dummy_fingerprint(),
    )
    session.append_run_end(
        run_id="00000000-0000-0000-0000-000000000001",
        reason="completed",
    )
    first_leaf = session.leaf_id

    # First branch
    session.branch(from_entry_id=first_leaf)
    branch1_id = session.leaf_id

    # Switch back and create second branch
    session.switch_leaf(first_leaf)
    session.branch(from_entry_id=first_leaf, branch_name="alt")

    leaves = session.leaves()
    assert len(leaves) == 2
    assert branch1_id in leaves
    assert session.leaf_id in leaves


def test_switch_leaf_changes_active() -> None:
    session = SessionManager.in_memory()
    session.append_run_start(
        run_id="00000000-0000-0000-0000-000000000001",
        runtime_fingerprint=_dummy_fingerprint(),
    )
    session.append_run_end(
        run_id="00000000-0000-0000-0000-000000000001",
        reason="completed",
    )
    leaf_a = session.leaf_id

    session.branch(from_entry_id=leaf_a)
    leaf_b = session.leaf_id

    session.switch_leaf(leaf_a)
    assert session.leaf_id == leaf_a

    session.switch_leaf(leaf_b)
    assert session.leaf_id == leaf_b


def test_switch_to_nonexistent_leaf_fails() -> None:
    session = SessionManager.in_memory()
    with pytest.raises(SessionError):
        session.switch_leaf("nonexistent-leaf-id")


def test_switch_to_inner_node_is_allowed() -> None:
    """Switching to an inner node is allowed — appending creates a branch."""
    session = SessionManager.in_memory()
    session.append_run_start(
        run_id="00000000-0000-0000-0000-000000000001",
        runtime_fingerprint=_dummy_fingerprint(),
    )
    inner_id = session.leaf_id
    session.append_run_end(
        run_id="00000000-0000-0000-0000-000000000001",
        reason="completed",
    )
    # inner_id is now an inner node (has child), but switching is allowed
    session.switch_leaf(inner_id)
    assert session.leaf_id == inner_id


# ---------------------------------------------------------------------------
# Branch — edge cases
# ---------------------------------------------------------------------------

def test_branch_from_nonexistent_entry_fails() -> None:
    session = SessionManager.in_memory()
    with pytest.raises(SessionError):
        session.branch(from_entry_id="nonexistent")


def test_branch_preserves_ancestor_entries() -> None:
    session = SessionManager.in_memory()
    session.append_run_start(
        run_id="00000000-0000-0000-0000-000000000001",
        runtime_fingerprint=_dummy_fingerprint(),
    )
    session.append_run_end(
        run_id="00000000-0000-0000-0000-000000000001",
        reason="completed",
    )
    before_count = len(session.entries)

    session.branch(from_entry_id=session.leaf_id)
    # Branch adds one entry, other entries preserved
    assert len(session.entries) == before_count + 1


# ---------------------------------------------------------------------------
# Fork
# ---------------------------------------------------------------------------

def test_fork_creates_independent_session() -> None:
    session = SessionManager.in_memory()
    session.append_run_start(
        run_id="00000000-0000-0000-0000-000000000001",
        runtime_fingerprint=_dummy_fingerprint(),
    )
    session.append_run_end(
        run_id="00000000-0000-0000-0000-000000000001",
        reason="completed",
    )

    # Fork requires persistent session, so use temp dir
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "sessions"
        persistent = SessionManager.create(".", root=root)
        persistent.append_run_start(
            run_id="00000000-0000-0000-0000-000000000001",
            runtime_fingerprint=_dummy_fingerprint(),
        )
        persistent.append_run_end(
            run_id="00000000-0000-0000-0000-000000000001",
            reason="completed",
        )

        forked = persistent.fork()
        assert forked.session_id != persistent.session_id
        assert len(forked.messages) == 0  # No messages in original
        assert forked.is_persistent
        forked.close()
        persistent.close()


# ---------------------------------------------------------------------------
# Compact
# ---------------------------------------------------------------------------

def test_compact_creates_compact_entry() -> None:
    session = SessionManager.in_memory()
    session.append_run_start(
        run_id="00000000-0000-0000-0000-000000000001",
        runtime_fingerprint=_dummy_fingerprint(),
    )
    session.append_run_end(
        run_id="00000000-0000-0000-0000-000000000001",
        reason="completed",
    )
    leaf_before = session.leaf_id

    entry = session.compact(
        up_to_entry_id=leaf_before,
        summary="This session created a project skeleton.",
    )
    assert isinstance(entry, CompactEntry)
    assert entry.summary == "This session created a project skeleton."
    assert session.leaf_id == entry.entry_id
    # Can still switch back to the original leaf
    session.switch_leaf(leaf_before)
    assert session.leaf_id == leaf_before


def test_compact_collects_message_ids() -> None:
    session = SessionManager.in_memory()
    session.append_run_start(
        run_id="00000000-0000-0000-0000-000000000001",
        runtime_fingerprint=_dummy_fingerprint(),
    )
    session.append_run_end(
        run_id="00000000-0000-0000-0000-000000000001",
        reason="completed",
    )
    leaf_before = session.leaf_id
    before_count = len(session.entries)

    entry = session.compact(
        up_to_entry_id=leaf_before,
        summary="Compacted.",
    )
    # Compact adds one new entry; old entries preserved
    assert len(session.entries) == before_count + 1
    assert isinstance(entry, CompactEntry)


def test_compact_to_nonexistent_fails() -> None:
    session = SessionManager.in_memory()
    with pytest.raises(SessionError):
        session.compact(up_to_entry_id="nonexistent", summary="test")


# ---------------------------------------------------------------------------
# Persistence round-trip with new entry types
# ---------------------------------------------------------------------------

def test_branch_entry_round_trips_through_jsonl() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "sessions"
        session = SessionManager.create(".", root=root)
        session.append_run_start(
            run_id="00000000-0000-0000-0000-000000000001",
            runtime_fingerprint=_dummy_fingerprint(),
        )
        session.append_run_end(
            run_id="00000000-0000-0000-0000-000000000001",
            reason="completed",
        )
        session.branch(from_entry_id=session.leaf_id, branch_name="test-branch")
        session.close()

        reopened = SessionManager.open(session.session_path, workspace=".", root=root)
        leaves = reopened.leaves()
        assert len(leaves) == 1
        # Active path should contain the BranchEntry
        path = reopened.get_active_path()
        branch_entries = [e for e in path if isinstance(e, BranchEntry)]
        assert len(branch_entries) == 1
        assert branch_entries[0].branch_name == "test-branch"
        reopened.close()


def test_compact_entry_round_trips_through_jsonl() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "sessions"
        session = SessionManager.create(".", root=root)
        session.append_run_start(
            run_id="00000000-0000-0000-0000-000000000001",
            runtime_fingerprint=_dummy_fingerprint(),
        )
        session.append_run_end(
            run_id="00000000-0000-0000-0000-000000000001",
            reason="completed",
        )
        leaf = session.leaf_id
        session.compact(up_to_entry_id=leaf, summary="summary text")
        session.close()

        reopened = SessionManager.open(session.session_path, workspace=".", root=root)
        path = reopened.get_active_path()
        compact_entries = [e for e in path if isinstance(e, CompactEntry)]
        assert len(compact_entries) == 1
        assert compact_entries[0].summary == "summary text"
        reopened.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _dummy_fingerprint():
    from evopi.session import RuntimeFingerprint
    return RuntimeFingerprint(
        harness="test",
        model="test-model",
        system_prompt_sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        tools_sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        policies_sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    )
