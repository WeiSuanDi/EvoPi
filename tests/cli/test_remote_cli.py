from __future__ import annotations

from pathlib import Path

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
