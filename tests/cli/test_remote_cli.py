from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from evopi.cli.main import main
from evopi.remote import RemoteHostConfig, RemoteHostStore


def test_remote_init_creates_host_profile_without_model_or_network(
    tmp_path: Path, capsys: object
) -> None:
    code = main(
        [
            "remote",
            "init",
            "home",
            "--workspace",
            str(tmp_path / "workspace"),
            "--remote-root",
            str(tmp_path / "remote"),
            "--profile",
            "default",
        ]
    )

    assert code == 0
    assert (tmp_path / "remote" / "hosts" / "home" / "config.json").exists()


def test_remote_pair_uses_authenticated_local_management_plane(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    called: list[tuple[str, dict[str, object]]] = []

    def fake_call(
        store: object, host_name: str, method: str, params: dict[str, object]
    ) -> dict[str, object]:
        del store
        assert host_name == "home"
        called.append((method, params))
        return {"code": "ABCD-EFGH-JKLM", "expires_at": "2026-01-01T00:10:00+00:00"}

    monkeypatch.setattr("evopi.cli.remote._call_admin", fake_call)
    code = main(
        [
            "remote",
            "pair",
            "home",
            "--remote-root",
            str(tmp_path / "remote"),
        ]
    )

    assert code == 0
    assert called == [("pair.issue", {})]
    assert "ABCD-EFGH-JKLM" in capsys.readouterr().out


def test_remote_serve_binds_initialized_workspace_and_security_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote_root = tmp_path / "remote"
    workspace = tmp_path / "workspace"
    RemoteHostStore(remote_root).initialize(
        RemoteHostConfig(name="home", workspace=workspace)
    )
    harness = object()
    captured: dict[str, object] = {}

    main_module = importlib.import_module("evopi.cli.main")
    def fake_build(args: object, **kwargs: object) -> object:
        del kwargs
        assert args.model_profile == "default"
        return harness

    monkeypatch.setattr(main_module, "_build_harness", fake_build)

    async def fake_serve(
        received_harness: object,
        broker: object,
        **kwargs: object,
    ) -> int:
        del broker
        captured.update(kwargs)
        assert received_harness is harness
        return 0

    monkeypatch.setattr("evopi.remote.serve_remote_gateway", fake_serve)
    code = main(
        [
            "remote",
            "serve",
            "home",
            "--remote-root",
            str(remote_root),
            "--console",
            "--request-rate",
            "77",
        ]
    )

    assert code == 0
    assert captured["host_name"] == "home"
    assert captured["gateway_config"].console_enabled is True
    assert captured["gateway_config"].request_rate_per_minute == 77


def test_remote_serve_refuses_public_plaintext_before_harness_creation(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    remote_root = tmp_path / "remote"
    RemoteHostStore(remote_root).initialize(
        RemoteHostConfig(name="home", workspace=tmp_path / "workspace")
    )

    code = main(
        [
            "remote",
            "serve",
            "home",
            "--remote-root",
            str(remote_root),
            "--bind",
            "0.0.0.0",
            "--allowed-host",
            "agent.example",
        ]
    )

    assert code == 1
    assert "TLS" in capsys.readouterr().err
