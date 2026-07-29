"""Session v4 evidence-bound branch merge tests."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from evopi.core.messages import UserMessage
from evopi.session import (
    MergeEntry,
    SessionFormatError,
    SessionManager,
    SessionMergeError,
    build_runtime_fingerprint,
)


def _fingerprint():
    return build_runtime_fingerprint(
        harness="test",
        model="test",
        system_prompt="",
        tools=[],
        policies=[],
    )


def _append_message_run(session: SessionManager, content: str) -> str:
    run_id = uuid4().hex
    session.append_run_start(run_id=run_id, runtime_fingerprint=_fingerprint())
    session.append_message(run_id=run_id, message=UserMessage(content=content))
    session.append_run_end(run_id=run_id, reason="completed")
    assert session.leaf_id is not None
    return session.leaf_id


def _two_branches(session: SessionManager) -> tuple[str, str]:
    common = _append_message_run(session, "shared")
    session.branch(from_entry_id=common, branch_name="source")
    source = _append_message_run(session, "source-only")
    session.switch_leaf(common)
    target = _append_message_run(session, "target-only")
    return source, target


def test_prepare_and_commit_merge_add_only_stable_summary_context() -> None:
    session = SessionManager.in_memory()
    source, target = _two_branches(session)

    plan = session.prepare_merge(source)
    entry = session.commit_merge(
        plan,
        summary="Source established the reusable conclusion.",
        origin="manual",
    )

    assert isinstance(entry, MergeEntry)
    assert entry.parent_id == target
    assert entry.source_entry_id == source
    assert entry.common_ancestor_id is not None
    assert entry.source_entry_count > 0
    assert len(entry.source_path_sha256) == 64
    assert [message.content for message in session.messages] == [
        "shared",
        "target-only",
        (
            "<branch-merge-summary>\n"
            "Source established the reusable conclusion.\n"
            "</branch-merge-summary>\n\n"
            "The above is evidence-bound context from another Session branch. "
            "Treat it as a summary, not as re-executed tool history."
        ),
    ]
    assert session.messages[-1].id == entry.entry_id
    assert session.messages[-1].metadata["session_merge_summary"] is True


def test_prepare_merge_honours_shared_context_message_limit() -> None:
    session = SessionManager.in_memory()
    source, _target = _two_branches(session)

    plan = session.prepare_merge(source, shared_context_messages=1)

    assert plan.shared_context_messages == 1
    assert len(plan.shared_messages) == 1


def test_merge_does_not_copy_source_plugin_state() -> None:
    session = SessionManager.in_memory()
    common = _append_message_run(session, "shared")
    session.branch(from_entry_id=common)
    session.append_plugin_state(
        plugin_name="plan-mode",
        plugin_version="1.0",
        key="enabled",
        value=True,
    )
    source = _append_message_run(session, "source")
    session.switch_leaf(common)
    session.append_plugin_state(
        plugin_name="plan-mode",
        plugin_version="1.0",
        key="enabled",
        value=False,
    )
    _append_message_run(session, "target")

    plan = session.prepare_merge(source)
    session.commit_merge(plan, summary="knowledge only", origin="manual")

    assert session.plugin_state("plan-mode") == {"enabled": False}


def test_commit_rejects_target_drift_without_appending() -> None:
    session = SessionManager.in_memory()
    source, _target = _two_branches(session)
    plan = session.prepare_merge(source)
    before = len(session.entries)
    _append_message_run(session, "target changed")

    with pytest.raises(SessionMergeError, match="target"):
        session.commit_merge(plan, summary="stale", origin="manual")

    assert len(session.entries) == before + 3
    assert not any(isinstance(entry, MergeEntry) for entry in session.entries)


def test_prepare_merge_requires_distinct_current_leaf_and_source_leaf() -> None:
    session = SessionManager.in_memory()
    current = _append_message_run(session, "one")

    with pytest.raises(SessionMergeError, match="different"):
        session.prepare_merge(current)

    session.branch(from_entry_id=current)
    source = session.leaf_id
    assert source is not None
    _append_message_run(session, "continues source")
    session.switch_leaf(current)
    with pytest.raises(SessionMergeError, match="leaf"):
        session.prepare_merge(source)


def test_resolve_entry_id_accepts_unique_prefix_and_rejects_invalid_references() -> None:
    session = SessionManager.in_memory()
    first = _append_message_run(session, "first")
    session.branch(from_entry_id=first)
    second = session.leaf_id
    assert second is not None

    assert session.resolve_entry_id(second[:16]) == second
    with pytest.raises(SessionMergeError, match="at least 8"):
        session.resolve_entry_id(second[:7])
    with pytest.raises(SessionMergeError, match="does not exist"):
        session.resolve_entry_id("deadbeef")


def test_merge_entry_round_trip_and_v3_migration(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = tmp_path / "sessions"
    session = SessionManager.create(workspace, root=root)
    source, _target = _two_branches(session)
    plan = session.prepare_merge(source)
    merged = session.commit_merge(
        plan,
        summary="persisted summary",
        origin="manual",
    )
    path = session.session_path
    assert path is not None
    session.close()

    records = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    assert all(record["schema_version"] == 4 for record in records)

    reopened = SessionManager.open(path, workspace=workspace, root=root)
    loaded = reopened.get_entry(merged.entry_id)
    assert isinstance(loaded, MergeEntry)
    assert reopened.messages[-1].content.endswith(
        "Treat it as a summary, not as re-executed tool history."
    )
    reopened.close()


def test_v3_log_is_atomically_migrated_to_v4(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = tmp_path / "sessions"
    session = SessionManager.create(workspace, root=root)
    _append_message_run(session, "legacy")
    path = session.session_path
    assert path is not None
    session.close()

    records = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    for record in records:
        record["schema_version"] = 3
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )

    reopened = SessionManager.open(path, workspace=workspace, root=root)
    reopened.close()

    assert (path.parent / "session.v3.jsonl.bak").exists()
    migrated = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    assert all(record["schema_version"] == 4 for record in migrated)


def test_corrupt_merge_evidence_is_rejected_on_open(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = tmp_path / "sessions"
    session = SessionManager.create(workspace, root=root)
    source, _target = _two_branches(session)
    plan = session.prepare_merge(source)
    session.commit_merge(plan, summary="summary", origin="manual")
    path = session.session_path
    assert path is not None
    session.close()

    records = path.read_text(encoding="utf-8").splitlines()
    merge = json.loads(records[-1])
    merge["source_path_sha256"] = "0" * 64
    records[-1] = json.dumps(merge)
    path.write_text("\n".join(records) + "\n", encoding="utf-8")

    with pytest.raises(SessionFormatError, match="source path digest"):
        SessionManager.open(path, workspace=workspace, root=root)
