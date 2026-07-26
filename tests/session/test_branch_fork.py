"""Tests for Session branch, fork, compact, and multi-leaf operations."""

import tempfile
from pathlib import Path

import pytest

from evopi.session import (
    BranchEntry,
    CompactEntry,
    LeafSelectedEntry,
    SessionError,
    SessionManager,
)
from evopi.core.messages import UserMessage


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
    session.branch(from_entry_id=session.leaf_id, branch_name="alt")

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

    selected_a = session.switch_leaf(leaf_a)
    assert isinstance(selected_a, LeafSelectedEntry)
    assert selected_a.parent_id == leaf_a

    selected_b = session.switch_leaf(leaf_b)
    assert selected_b.parent_id == leaf_b


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
    selected = session.switch_leaf(inner_id)
    assert selected.parent_id == inner_id


def test_switch_leaf_rebuilds_messages_from_only_the_selected_path() -> None:
    session = SessionManager.in_memory()
    root_run = "00000000-0000-0000-0000-000000000001"
    session.append_run_start(run_id=root_run, runtime_fingerprint=_dummy_fingerprint())
    session.append_message(
        run_id=root_run,
        message=UserMessage(content="root"),
    )
    root_end = session.append_run_end(run_id=root_run, reason="completed")

    session.branch(from_entry_id=session.leaf_id, branch_name="a")
    run_a = "00000000-0000-0000-0000-000000000002"
    session.append_run_start(run_id=run_a, runtime_fingerprint=_dummy_fingerprint())
    session.append_message(run_id=run_a, message=UserMessage(content="branch-a"))
    session.append_run_end(run_id=run_a, reason="completed")
    leaf_a = session.leaf_id

    session.switch_leaf(root_end.entry_id)
    run_b = "00000000-0000-0000-0000-000000000003"
    session.append_run_start(run_id=run_b, runtime_fingerprint=_dummy_fingerprint())
    session.append_message(run_id=run_b, message=UserMessage(content="branch-b"))
    session.append_run_end(run_id=run_b, reason="completed")

    session.switch_leaf(leaf_a)

    assert [message.content for message in session.messages] == ["root", "branch-a"]


def test_selected_leaf_persists_without_appending_a_new_run(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    session = SessionManager.create(tmp_path, root=root)
    run_id = "00000000-0000-0000-0000-000000000001"
    session.append_run_start(run_id=run_id, runtime_fingerprint=_dummy_fingerprint())
    session.append_message(run_id=run_id, message=UserMessage(content="first"))
    first_end = session.append_run_end(run_id=run_id, reason="completed")
    session.branch(from_entry_id=session.leaf_id, branch_name="other")

    selected = session.switch_leaf(first_end.entry_id)
    path = session.session_path
    session.close()

    reopened = SessionManager.open(path, workspace=tmp_path, root=root)
    try:
        assert isinstance(reopened.get_entry(reopened.leaf_id), LeafSelectedEntry)
        assert reopened.leaf_id == selected.entry_id
        assert [message.content for message in reopened.messages] == ["first"]
    finally:
        reopened.close()


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
    selected = session.switch_leaf(leaf_before)
    assert selected.parent_id == leaf_before


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


def test_compact_replaces_only_selected_projection_prefix() -> None:
    session = SessionManager.in_memory()
    run_id = "00000000-0000-0000-0000-000000000001"
    session.append_run_start(run_id=run_id, runtime_fingerprint=_dummy_fingerprint())
    session.append_message(run_id=run_id, message=UserMessage(content="old"))
    session.append_message(run_id=run_id, message=UserMessage(content="recent"))
    session.append_run_end(run_id=run_id, reason="completed")

    session.compact(
        up_to_entry_id=session.leaf_id,
        summary="old summary",
        compacted_ids=[session.message_source_ids[0]],
    )

    assert [message.content for message in session.messages] == [
        (
            "<summary>\nold summary\n</summary>\n\n"
            "The above is a summary of the earlier conversation. "
            "Continue helping based on this context."
        ),
        "recent",
    ]


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
