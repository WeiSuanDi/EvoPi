from __future__ import annotations

import importlib
import json
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path

import pytest

from evopi.cli.policy_discovery import policy_discover_main

from tests.evolution.test_policy_pattern_discovery import (
    _confirmation_records,
    _write_trace,
)


def _repeated_denial_trace(path: Path) -> None:
    records: list[dict[str, object]] = []
    for index, run_id in enumerate(("run-one", "run-two", "run-two"), start=1):
        records.extend(
            _confirmation_records(
                run_id=run_id,
                index=index,
                decision="deny",
                command=f"private-command-{index}",
                created_at=datetime(2026, 1, index, tzinfo=UTC),
            )
        )
    _write_trace(path, records)


def test_policy_discover_json_saves_immutable_report_without_raw_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trace = tmp_path / "trace.jsonl"
    _repeated_denial_trace(trace)
    home = tmp_path / "home"
    monkeypatch.setenv("EVOPI_HOME", str(home))
    output = StringIO()

    with redirect_stdout(output):
        code = policy_discover_main([str(trace), "--json"])

    payload = json.loads(output.getvalue())
    assert code == 0
    assert payload["opportunities"][0]["theme"] == "repeated_denial"
    assert "private-command" not in output.getvalue()
    report_id = payload["report_id"]
    assert (
        home / "opportunities" / "policies" / "reports" / f"{report_id}.json"
    ).is_file()


def test_main_dispatches_policy_discover_without_building_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trace = tmp_path / "trace.jsonl"
    _repeated_denial_trace(trace)
    monkeypatch.setenv("EVOPI_HOME", str(tmp_path / "home"))

    def fail_if_runtime_is_built(*_args, **_kwargs):
        raise AssertionError("Policy discovery must not build a Harness or Model")

    cli_main = importlib.import_module("evopi.cli.main")
    monkeypatch.setattr(cli_main, "_build_harness", fail_if_runtime_is_built)
    output = StringIO()
    with redirect_stdout(output):
        code = cli_main.main(["policy", "discover", str(trace)])

    assert code == 0
    rendered = output.getvalue()
    assert "Policy opportunity discovery" in rendered
    assert "repeated_denial" in rendered
    assert "line=2" in rendered
    assert "run=run-one" in rendered
    assert "does not create or activate a Policy" in rendered


def test_policy_discover_invalid_trace_fails_without_saving_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trace = tmp_path / "trace.jsonl"
    trace.write_text("{broken}\n", encoding="utf-8")
    home = tmp_path / "home"
    monkeypatch.setenv("EVOPI_HOME", str(home))
    errors = StringIO()

    with redirect_stderr(errors):
        code = policy_discover_main([str(trace)])

    assert code == 1
    assert "trace.jsonl:1" in errors.getvalue()
    assert (home / "opportunities").exists() is False


def test_policy_discover_zero_opportunities_is_a_successful_saved_analysis(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trace = tmp_path / "trace.jsonl"
    _write_trace(
        trace,
        [{"schema_version": 2, "type": "agent_start", "run_id": "run", "data": {}}],
    )
    monkeypatch.setenv("EVOPI_HOME", str(tmp_path / "home"))
    output = StringIO()

    with redirect_stdout(output):
        code = policy_discover_main(
            [str(trace), "--min-occurrences", "4", "--min-runs", "3", "--json"]
        )

    payload = json.loads(output.getvalue())
    assert code == 0
    assert payload["settings"]["min_occurrences"] == 4
    assert payload["settings"]["min_runs"] == 3
    assert payload["opportunities"] == []
