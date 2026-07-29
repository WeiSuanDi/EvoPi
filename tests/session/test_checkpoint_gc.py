"""Checkpoint GC planning and application tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
import hashlib
import os
from pathlib import Path
from uuid import uuid4

import pytest

from evopi.core.messages import UserMessage
from evopi.session import (
    CheckpointGCError,
    CheckpointGCSettings,
    SessionManager,
    build_runtime_fingerprint,
)


def _fingerprint():
    return build_runtime_fingerprint(
        harness="gc-test",
        model="gc-test",
        system_prompt="",
        tools=[],
        policies=[],
    )


def _append_checkpoint(session: SessionManager, content: str) -> Path:
    run_id = uuid4().hex
    session.append_run_start(run_id=run_id, runtime_fingerprint=_fingerprint())
    session.append_message(run_id=run_id, message=UserMessage(content=content))
    run_end = session.append_run_end(run_id=run_id, reason="completed")
    checkpoint = session.create_checkpoint(
        run_end=run_end,
        runtime_fingerprint=_fingerprint(),
    )
    assert checkpoint is not None
    assert session.session_path is not None
    return session.session_path.parent / "checkpoints" / f"{checkpoint.checkpoint_id}.json"


def _old(path: Path, *, days: int = 30) -> None:
    timestamp = (datetime.now(UTC) - timedelta(days=days)).timestamp()
    os.utime(path, (timestamp, timestamp))


def _branched_session(tmp_path: Path) -> tuple[SessionManager, list[Path]]:
    session = SessionManager.create(tmp_path / "workspace", root=tmp_path / "sessions")
    files = [
        _append_checkpoint(session, "common-1"),
        _append_checkpoint(session, "common-2"),
    ]
    common = session.leaf_id
    assert common is not None
    session.branch(from_entry_id=common, branch_name="source")
    files.extend(
        [
            _append_checkpoint(session, "source-1"),
            _append_checkpoint(session, "source-2"),
            _append_checkpoint(session, "source-3"),
        ]
    )
    session.switch_leaf(common)
    files.extend(
        [
            _append_checkpoint(session, "target-1"),
            _append_checkpoint(session, "target-2"),
            _append_checkpoint(session, "target-3"),
        ]
    )
    for path in files:
        _old(path)
    return session, files


def test_gc_plan_keeps_recent_valid_checkpoints_per_leaf_union(tmp_path: Path) -> None:
    session, files = _branched_session(tmp_path)

    plan = session.plan_checkpoint_gc(
        CheckpointGCSettings(keep_per_leaf=2, protect_days=0)
    )

    assert plan.session_id == session.session_id
    assert len(plan.log_sha256) == 64
    assert len(plan.items) == 8
    assert len(plan.candidates) == 4
    assert {item.relative_path for item in plan.candidates} == {
        path.relative_to(session.session_path.parent).as_posix()
        for path in files[:2] + [files[2], files[5]]
    }
    assert plan.estimated_bytes == sum(item.size_bytes for item in plan.candidates)
    assert all(item.reason == "retained_per_leaf" for item in plan.kept if not item.protected)


def test_gc_plan_protects_recent_files_even_when_not_retained(tmp_path: Path) -> None:
    session, _files = _branched_session(tmp_path)

    plan = session.plan_checkpoint_gc(
        CheckpointGCSettings(keep_per_leaf=1, protect_days=365)
    )

    assert plan.candidates == ()
    assert len(plan.protected) == 8
    assert all(item.reason == "protected_by_age" for item in plan.protected)


def test_gc_plan_classifies_old_corrupt_orphan_and_temporary_files(
    tmp_path: Path,
) -> None:
    session = SessionManager.create(tmp_path / "workspace", root=tmp_path / "sessions")
    referenced = _append_checkpoint(session, "one")
    checkpoint_dir = referenced.parent
    referenced.write_text("{broken", encoding="utf-8")
    orphan = checkpoint_dir / f"{uuid4().hex}.json"
    orphan.write_text("{}\n", encoding="utf-8")
    temporary = checkpoint_dir / f".{uuid4().hex}.json.deadbeef.tmp"
    temporary.write_text("partial", encoding="utf-8")
    ignored = checkpoint_dir / "notes.txt"
    ignored.write_text("keep me", encoding="utf-8")
    for path in (referenced, orphan, temporary, ignored):
        _old(path)

    plan = session.plan_checkpoint_gc(
        CheckpointGCSettings(keep_per_leaf=1, protect_days=0)
    )

    categories = {item.relative_path: item.category for item in plan.candidates}
    assert categories[referenced.relative_to(session.session_path.parent).as_posix()] == (
        "corrupt"
    )
    assert categories[orphan.relative_to(session.session_path.parent).as_posix()] == (
        "orphan"
    )
    assert categories[
        temporary.relative_to(session.session_path.parent).as_posix()
    ] == "temporary"
    assert ignored.exists()


def test_gc_plan_reports_missing_reference_without_making_it_a_candidate(
    tmp_path: Path,
) -> None:
    session = SessionManager.create(tmp_path / "workspace", root=tmp_path / "sessions")
    checkpoint = _append_checkpoint(session, "one")
    checkpoint.unlink()

    plan = session.plan_checkpoint_gc(
        CheckpointGCSettings(keep_per_leaf=1, protect_days=0)
    )

    assert len(plan.missing) == 1
    assert plan.missing[0].reason == "referenced_file_missing"
    assert plan.missing[0].eligible is False
    assert plan.candidates == ()


def test_gc_dry_run_does_not_change_files_or_session_log(tmp_path: Path) -> None:
    session, _files = _branched_session(tmp_path)
    assert session.session_path is not None
    before_log = session.session_path.read_bytes()
    before_files = sorted(path.name for path in session.session_path.parent.glob("checkpoints/*"))

    plan = session.plan_checkpoint_gc(
        CheckpointGCSettings(keep_per_leaf=1, protect_days=0)
    )

    assert plan.candidates
    assert session.session_path.read_bytes() == before_log
    assert sorted(path.name for path in session.session_path.parent.glob("checkpoints/*")) == (
        before_files
    )


def test_gc_apply_rejects_log_drift_before_deleting_any_file(tmp_path: Path) -> None:
    session, _files = _branched_session(tmp_path)
    plan = session.plan_checkpoint_gc(
        CheckpointGCSettings(keep_per_leaf=1, protect_days=0)
    )
    candidates = [
        session.session_path.parent / item.relative_path  # type: ignore[union-attr]
        for item in plan.candidates
    ]
    assert session.session_path is not None
    session.session_path.write_bytes(session.session_path.read_bytes() + b"\n")

    with pytest.raises(CheckpointGCError, match="Session Log changed"):
        session.apply_checkpoint_gc(plan)

    assert all(path.exists() for path in candidates)


def test_gc_apply_rejects_candidate_drift_before_any_deletion(tmp_path: Path) -> None:
    session, _files = _branched_session(tmp_path)
    plan = session.plan_checkpoint_gc(
        CheckpointGCSettings(keep_per_leaf=1, protect_days=0)
    )
    candidates = [
        session.session_path.parent / item.relative_path  # type: ignore[union-attr]
        for item in plan.candidates
    ]
    candidates[-1].write_text("changed", encoding="utf-8")

    with pytest.raises(CheckpointGCError, match="candidate changed"):
        session.apply_checkpoint_gc(plan)

    assert all(path.exists() for path in candidates)


def test_gc_apply_rejects_forged_non_checkpoint_target(tmp_path: Path) -> None:
    session, _files = _branched_session(tmp_path)
    plan = session.plan_checkpoint_gc(
        CheckpointGCSettings(keep_per_leaf=1, protect_days=0)
    )
    assert session.session_path is not None
    notes = session.session_path.parent / "checkpoints" / "notes.txt"
    notes.write_text("not a checkpoint", encoding="utf-8")
    forged_item = replace(
        plan.candidates[0],
        relative_path="checkpoints/notes.txt",
        size_bytes=notes.stat().st_size,
        sha256=hashlib.sha256(notes.read_bytes()).hexdigest(),
    )
    forged = replace(plan, items=(forged_item,))

    with pytest.raises(CheckpointGCError, match="invalid candidate"):
        session.apply_checkpoint_gc(forged)

    assert notes.read_text(encoding="utf-8") == "not a checkpoint"


def test_gc_apply_deletes_only_candidates_and_session_remains_recoverable(
    tmp_path: Path,
) -> None:
    session, _files = _branched_session(tmp_path)
    plan = session.plan_checkpoint_gc(
        CheckpointGCSettings(keep_per_leaf=1, protect_days=0)
    )
    candidates = [
        session.session_path.parent / item.relative_path  # type: ignore[union-attr]
        for item in plan.candidates
    ]
    kept = [
        session.session_path.parent / item.relative_path  # type: ignore[union-attr]
        for item in plan.kept
    ]
    session_path = session.session_path

    report = session.apply_checkpoint_gc(plan)

    assert report.applied is True
    assert report.deleted_count == len(candidates)
    assert report.errors == ()
    assert not any(path.exists() for path in candidates)
    assert all(path.exists() for path in kept)
    assert session.is_broken is False
    session.close()
    reopened = SessionManager.open(
        session_path,
        workspace=tmp_path / "workspace",
        root=tmp_path / "sessions",
    )
    assert reopened.messages[-1].content == "target-3"
    reopened.close()


def test_gc_report_records_partial_delete_failure_without_breaking_manager(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, _files = _branched_session(tmp_path)
    plan = session.plan_checkpoint_gc(
        CheckpointGCSettings(keep_per_leaf=1, protect_days=0)
    )
    failed_name = Path(plan.candidates[-1].relative_path).name
    original_unlink = Path.unlink

    def fail_one(path: Path, *args, **kwargs):
        if path.name == failed_name:
            raise PermissionError("locked")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_one)

    report = session.apply_checkpoint_gc(plan)

    assert report.deleted_count == len(plan.candidates) - 1
    assert len(report.errors) == 1
    assert report.errors[0].relative_path == plan.candidates[-1].relative_path
    assert report.passed is False
    assert session.is_broken is False
