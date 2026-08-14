from __future__ import annotations

from pathlib import Path

import pytest

from evopi.cli.main import main


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
