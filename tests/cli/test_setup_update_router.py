from __future__ import annotations

import json
from pathlib import Path

import pytest

from evopi.cli.main import main
from evopi.distribution import ReleaseInfo


def _clear_model_environment(monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
    monkeypatch.setenv("EVOPI_HOME", str(home))
    for name in (
        "EVOPI_PROVIDER",
        "ANTHROPIC_MODEL",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_API_KEY",
        "OPENAI_MODEL",
        "EVOPI_MODEL",
        "OPENAI_BASE_URL",
        "OPENAI_API_KEY",
    ):
        monkeypatch.setenv(name, "")


def test_setup_and_update_help_are_product_commands(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["setup", "--help"]) == 0
    assert "evopi setup" in capsys.readouterr().out
    assert main(["update", "--help"]) == 0
    assert "evopi update" in capsys.readouterr().out


def test_noninteractive_run_and_rpc_require_setup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _clear_model_environment(monkeypatch, tmp_path)

    assert main(["run", "hello"]) == 2
    assert "evopi setup" in capsys.readouterr().err
    assert main(["rpc"]) == 2
    assert "evopi setup" in capsys.readouterr().err


def test_update_check_works_for_external_install_without_downloading_wheel(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    info = ReleaseInfo(
        version="99.0.0",
        release_url="https://github.com/WeiSuanDi/EvoPi/releases/tag/v99.0.0",
        wheel_name="evopi-99.0.0-py3-none-any.whl",
        wheel_url="https://github.com/WeiSuanDi/EvoPi/releases/download/v99.0.0/x.whl",
        sha256="a" * 64,
        checksum_url="https://github.com/WeiSuanDi/EvoPi/releases/download/v99.0.0/SHA256SUMS",
    )

    class Client:
        def latest_info(self) -> ReleaseInfo:
            return info

        def download(self, release: ReleaseInfo) -> bytes:
            raise AssertionError("--check must not download the wheel")

        def close(self) -> None:
            return None

    monkeypatch.setenv("EVOPI_HOME", str(tmp_path))
    monkeypatch.setattr("evopi.cli.update.GitHubReleaseClient", Client)

    assert main(["update", "--check", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "update_available"
    assert payload["target_version"] == "99.0.0"
    assert "api_key" not in json.dumps(payload).lower()


def test_external_install_refuses_self_update(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    info = ReleaseInfo(
        version="99.0.0",
        release_url="https://github.com/WeiSuanDi/EvoPi/releases/tag/v99.0.0",
        wheel_name="evopi-99.0.0-py3-none-any.whl",
        wheel_url="https://github.com/WeiSuanDi/EvoPi/releases/download/v99.0.0/x.whl",
        sha256="a" * 64,
        checksum_url="https://github.com/WeiSuanDi/EvoPi/releases/download/v99.0.0/SHA256SUMS",
    )

    class Client:
        def latest_info(self) -> ReleaseInfo:
            return info

        def download(self, release: ReleaseInfo) -> bytes:
            raise AssertionError("unsupported install must fail before download")

        def close(self) -> None:
            return None

    monkeypatch.setenv("EVOPI_HOME", str(tmp_path))
    monkeypatch.delenv("EVOPI_MANAGED_ROOT", raising=False)
    monkeypatch.setattr("evopi.cli.update.GitHubReleaseClient", Client)

    assert main(["update", "--yes"]) == 2
    assert "externally managed" in capsys.readouterr().out
