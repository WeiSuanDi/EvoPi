from __future__ import annotations

import io
from pathlib import Path

from evopi.cli.setup import SetupOptions, run_setup
from evopi.configuration import CredentialStore, UserConfigStore


def test_noninteractive_setup_from_stdin_saves_unverified_profile(tmp_path: Path) -> None:
    output = io.StringIO()
    result = run_setup(
        SetupOptions(
            provider="anthropic",
            model="claude-test",
            base_url=None,
            api_key_stdin=True,
            skip_test=True,
        ),
        home=tmp_path,
        stdin=io.StringIO("test-key\n"),
        stdout=output,
        stderr=io.StringIO(),
        permission_hardener=lambda path: None,
    )

    assert result == 0
    profile = UserConfigStore(tmp_path / "config.toml").load().active
    assert profile.provider == "anthropic"
    assert profile.model == "claude-test"
    assert profile.verified is False
    assert CredentialStore(
        tmp_path / "credentials.json", permission_hardener=lambda path: None
    ).load()[0].api_key == "test-key"
    assert "test-key" not in output.getvalue()


def test_failed_connection_test_preserves_existing_configuration(tmp_path: Path) -> None:
    first = run_setup(
        SetupOptions(
            provider="anthropic",
            model="old-model",
            api_key_stdin=True,
            skip_test=True,
        ),
        home=tmp_path,
        stdin=io.StringIO("old-key\n"),
        stdout=io.StringIO(),
        stderr=io.StringIO(),
        permission_hardener=lambda path: None,
    )
    assert first == 0

    result = run_setup(
        SetupOptions(
            provider="anthropic",
            model="new-model",
            api_key_stdin=True,
            skip_test=False,
        ),
        home=tmp_path,
        stdin=io.StringIO("new-key\n"),
        stdout=io.StringIO(),
        stderr=io.StringIO(),
        permission_hardener=lambda path: None,
        connection_tester=lambda resolved: (_ for _ in ()).throw(RuntimeError("offline")),
        interactive=False,
    )

    assert result == 1
    assert UserConfigStore(tmp_path / "config.toml").load().active.model == "old-model"


def test_connection_failure_never_echoes_api_key(tmp_path: Path) -> None:
    stderr = io.StringIO()
    secret = "do-not-print-this-key"

    result = run_setup(
        SetupOptions(
            provider="anthropic",
            model="model",
            api_key_stdin=True,
        ),
        home=tmp_path,
        stdin=io.StringIO(f"{secret}\n"),
        stdout=io.StringIO(),
        stderr=stderr,
        permission_hardener=lambda path: None,
        connection_tester=lambda resolved: (_ for _ in ()).throw(
            RuntimeError(f"bad credential {secret}")
        ),
        interactive=False,
    )

    assert result == 1
    assert secret not in stderr.getvalue()
    assert "[redacted]" in stderr.getvalue()
