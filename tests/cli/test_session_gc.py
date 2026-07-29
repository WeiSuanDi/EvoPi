"""Checkpoint GC command tests."""

from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

from evopi.cli.session import session_gc_main
from evopi.cli.main import main
from evopi.core.messages import UserMessage
from evopi.session import SessionManager, build_runtime_fingerprint


def _fingerprint():
    return build_runtime_fingerprint(
        harness="cli-gc",
        model="cli-gc",
        system_prompt="",
        tools=[],
        policies=[],
    )


def _session_with_old_checkpoints(tmp_path: Path) -> SessionManager:
    session = SessionManager.create(tmp_path / "workspace", root=tmp_path / "sessions")
    for index in range(3):
        run_id = uuid4().hex
        session.append_run_start(run_id=run_id, runtime_fingerprint=_fingerprint())
        session.append_message(
            run_id=run_id,
            message=UserMessage(content=f"message-{index}"),
        )
        run_end = session.append_run_end(run_id=run_id, reason="completed")
        session.create_checkpoint(
            run_end=run_end,
            runtime_fingerprint=_fingerprint(),
        )
    assert session.session_path is not None
    for path in session.session_path.parent.glob("checkpoints/*.json"):
        os.utime(path, (1, 1))
    return session


def test_session_gc_cli_json_dry_run_and_apply(tmp_path: Path) -> None:
    session = _session_with_old_checkpoints(tmp_path)
    session_id = session.session_id
    assert session.session_path is not None
    session_log = session.session_path
    before_log = session_log.read_bytes()
    session.close()
    arguments = [
        session_id,
        "--session-root",
        str(tmp_path / "sessions"),
        "--keep-per-leaf",
        "1",
        "--protect-days",
        "0",
        "--json",
    ]
    output = StringIO()

    with redirect_stdout(output):
        code = session_gc_main(arguments)

    payload = json.loads(output.getvalue())
    assert code == 0
    assert payload["applied"] is False
    assert payload["candidate_count"] == 2
    assert session_log.read_bytes() == before_log
    assert len(list((Path(payload["session_path"]).parent / "checkpoints").glob("*.json"))) == 3

    output = StringIO()
    with redirect_stdout(output):
        code = session_gc_main([*arguments, "--apply"])

    payload = json.loads(output.getvalue())
    assert code == 0
    assert payload["applied"] is True
    assert payload["deleted_count"] == 2
    assert len(list((Path(payload["session_path"]).parent / "checkpoints").glob("*.json"))) == 1


def test_session_gc_cli_text_preview_and_argument_validation(
    tmp_path: Path,
) -> None:
    session = _session_with_old_checkpoints(tmp_path)
    session_path = session.session_path
    session.close()
    assert session_path is not None
    output = StringIO()

    with redirect_stdout(output):
        code = session_gc_main(
            [
                str(session_path),
                "--keep-per-leaf",
                "1",
                "--protect-days",
                "0",
            ]
        )

    assert code == 0
    assert "Dry run" in output.getvalue()
    assert "Candidates: 2" in output.getvalue()
    with pytest.raises(SystemExit):
        session_gc_main([str(session_path), "--keep-per-leaf", "0"])
    with pytest.raises(SystemExit):
        session_gc_main([str(session_path), "--protect-days", "-1"])


def test_main_dispatches_session_gc_without_building_a_model(
    tmp_path: Path,
) -> None:
    session = _session_with_old_checkpoints(tmp_path)
    session_id = session.session_id
    session.close()
    output = StringIO()

    with redirect_stdout(output):
        code = main(
            [
                "session",
                "gc",
                session_id,
                "--session-root",
                str(tmp_path / "sessions"),
                "--keep-per-leaf",
                "1",
                "--protect-days",
                "0",
                "--json",
            ]
        )

    assert code == 0
    assert json.loads(output.getvalue())["candidate_count"] == 2
