from __future__ import annotations

import json

from evopi.cli.diagnostics import (
    DoctorCheckStatus,
    DoctorStatus,
    build_config_snapshot,
    run_doctor,
)
from evopi.cli.main import main


def _environment(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("EVOPI_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("EVOPI_SESSION_DIR", str(tmp_path / "sessions"))
    monkeypatch.setenv("EVOPI_PROVIDER", "openai-compatible")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-test")
    monkeypatch.setenv("OPENAI_API_KEY", "top-secret-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.test/v1")
    monkeypatch.delenv("EVOPI_FALLBACKS", raising=False)


def test_config_snapshot_is_stable_and_never_contains_credentials(
    monkeypatch,
    tmp_path,
) -> None:
    _environment(monkeypatch, tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    snapshot = build_config_snapshot(workspace=workspace)
    payload = snapshot.to_dict()
    serialized = json.dumps(payload)

    assert payload["schema_version"] == 1
    assert payload["provider"] == "openai-compatible"
    assert payload["model"] == "gpt-test"
    assert payload["credential_configured"] is True
    assert payload["workspace_trusted"] is False
    assert payload["max_turns"] == 20
    assert "top-secret-key" not in serialized
    assert "api_key" not in serialized.lower()


def test_doctor_is_offline_and_reports_passed_checks(monkeypatch, tmp_path) -> None:
    _environment(monkeypatch, tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    report = run_doctor(workspace=workspace)

    assert report.schema_version == 1
    assert report.status is DoctorStatus.PASSED
    assert all(check.status is DoctorCheckStatus.PASSED for check in report.checks)
    assert report.to_dict()["status"] == "passed"


def test_doctor_warns_for_missing_credentials(monkeypatch, tmp_path) -> None:
    _environment(monkeypatch, tmp_path)
    monkeypatch.delenv("OPENAI_API_KEY")
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    report = run_doctor(workspace=workspace)

    assert report.status is DoctorStatus.WARNING
    credential = next(check for check in report.checks if check.name == "credential")
    assert credential.status is DoctorCheckStatus.WARNING


def test_doctor_fails_for_invalid_workspace_without_network(
    monkeypatch,
    tmp_path,
) -> None:
    _environment(monkeypatch, tmp_path)

    report = run_doctor(workspace=tmp_path / "missing")

    assert report.status is DoctorStatus.FAILED
    workspace = next(check for check in report.checks if check.name == "workspace")
    assert workspace.status is DoctorCheckStatus.FAILED


def test_doctor_does_not_import_unapproved_plugin_candidates(
    monkeypatch,
    tmp_path,
) -> None:
    _environment(monkeypatch, tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    marker = tmp_path / "candidate-imported.txt"
    candidate = workspace / ".evopi" / "plugin-candidates" / "unsafe"
    candidate.mkdir(parents=True)
    (candidate / "plugin.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('imported')\n",
        encoding="utf-8",
    )

    report = run_doctor(workspace=workspace)

    assert report.status is DoctorStatus.PASSED
    assert not marker.exists()


def test_config_and_doctor_cli_text_json_and_exit_codes(
    monkeypatch,
    tmp_path,
    capsys,
) -> None:
    _environment(monkeypatch, tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    assert main(["config", "show", "--workspace", str(workspace), "--json"]) == 0
    config_output = capsys.readouterr()
    assert json.loads(config_output.out)["schema_version"] == 1
    assert "top-secret-key" not in config_output.out

    assert main(["doctor", "--workspace", str(workspace), "--json"]) == 0
    doctor_output = capsys.readouterr()
    assert json.loads(doctor_output.out)["status"] == "passed"

    monkeypatch.delenv("OPENAI_API_KEY")
    assert main(["doctor", "--workspace", str(workspace)]) == 2
    assert "warning" in capsys.readouterr().out.lower()


def test_config_group_help_is_discoverable(capsys) -> None:
    assert main(["config", "--help"]) == 0
    output = capsys.readouterr().out
    assert "usage: evopi config" in output
    assert "show" in output
    assert "credentials" in output


def test_append_system_prompt_parser_is_compatible() -> None:
    from evopi.cli.main import build_parser

    args = build_parser().parse_args(
        [
            "--system-prompt",
            "replacement",
            "--append-system-prompt",
            "appendix",
        ]
    )
    assert args.system_prompt == "replacement"
    assert args.append_system_prompt == "appendix"


def test_config_snapshot_uses_max_turns_environment_and_explicit_override(
    monkeypatch,
    tmp_path,
) -> None:
    _environment(monkeypatch, tmp_path)
    monkeypatch.setenv("EVOPI_MAX_TURNS", "31")
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    assert build_config_snapshot(workspace=workspace).max_turns == 31
    assert build_config_snapshot(workspace=workspace, max_turns=8).max_turns == 8
