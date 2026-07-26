from __future__ import annotations

import json
from pathlib import Path

from evopi.cli.plugin import plugin_main
from evopi.plugins import (
    available_plugin_templates,
    initialize_plugin_candidate,
    plugin_sdk_guide,
    review_plugin,
)


def test_packaged_plugin_templates_are_discoverable() -> None:
    templates = available_plugin_templates()

    assert set(templates) == {"basic", "plan-mode"}
    assert all(template.description for template in templates.values())
    assert "Policy" in plugin_sdk_guide()


def test_plugin_init_creates_reviewable_candidate_without_activation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "home"
    monkeypatch.setenv("EVOPI_HOME", str(home))

    code = plugin_main(
        "init",
        [
            "my-helper",
            "--workspace",
            str(workspace),
            "--template",
            "basic",
            "--json",
        ],
    )

    candidate = workspace / ".evopi" / "plugin-candidates" / "my-helper"
    assert code == 0
    assert review_plugin(candidate).passed is True
    assert (candidate / "README.md").is_file()
    assert (candidate / "tests" / "test_plugin.py").is_file()
    assert not (home / "activations.json").exists()


def test_plugin_init_refuses_existing_nonempty_target(tmp_path: Path) -> None:
    target = tmp_path / "candidate"
    target.mkdir()
    (target / "keep.txt").write_text("user data", encoding="utf-8")

    try:
        initialize_plugin_candidate("demo", template="basic", path=target)
    except FileExistsError as exc:
        assert "not empty" in str(exc)
    else:
        raise AssertionError("expected FileExistsError")

    assert (target / "keep.txt").read_text(encoding="utf-8") == "user data"


def test_plugin_examples_json_lists_packaged_templates(capsys) -> None:
    code = plugin_main("examples", ["--json"])

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert [item["name"] for item in payload["templates"]] == [
        "basic",
        "plan-mode",
    ]
