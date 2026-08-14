from __future__ import annotations

import argparse
from importlib import import_module
import json
from io import StringIO
from types import SimpleNamespace

import pytest

from evopi.core.messages import AssistantMessage
from evopi.core.model_errors import ModelErrorInfo
from evopi.core.run import AgentRunState

cli_main = import_module("evopi.cli.main")


def test_root_help_exposes_the_two_product_layers(capsys) -> None:
    assert cli_main.main(["--help"]) == 0

    output = capsys.readouterr().out
    assert "evopi chat" in output
    assert "evopi run" in output
    assert "session" in output
    assert "policy" in output
    assert "plugin" in output
    assert "config" in output
    assert "doctor" in output


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("session", ("list", "gc")),
        ("policy", ("init", "review", "approve", "activate", "discover")),
        ("plugin", ("init", "review", "approve", "reload")),
    ],
)
def test_management_group_help_lists_supported_actions(
    command: str,
    expected: tuple[str, ...],
    capsys,
) -> None:
    assert cli_main.main([command, "--help"]) == 0

    output = capsys.readouterr().out
    for action in expected:
        assert action in output


def test_version_uses_package_version(capsys) -> None:
    assert cli_main.main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == "EvoPi 0.3.0"


def test_run_prompt_combines_pipe_and_argument() -> None:
    assert cli_main.compose_run_prompt("from stdin\n", "from argv") == (
        "from stdin\n\nfrom argv"
    )
    assert cli_main.compose_run_prompt("", "from argv") == "from argv"
    assert cli_main.compose_run_prompt("from stdin", None) == "from stdin"


def test_run_requires_prompt_or_stdin(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli_main.sys, "stdin", StringIO(""))

    assert cli_main.main(["run"]) == 2
    assert "requires a prompt or piped stdin" in capsys.readouterr().err


def test_chat_routes_initial_prompt_to_repl(
    monkeypatch,
    configured_anthropic_environment,
) -> None:
    observed: list[tuple[str | None, str | None]] = []

    async def fake_repl(args, *, initial_prompt=None):
        observed.append((args.prompt, initial_prompt))
        return 0

    monkeypatch.setattr(cli_main, "_run_repl", fake_repl)

    assert cli_main.main(["chat", "hello"]) == 0
    assert observed == [("hello", "hello")]


def test_run_routes_combined_prompt_to_one_shot(monkeypatch) -> None:
    observed: list[tuple[str, bool]] = []

    async def fake_run(args, *, json_output=False):
        observed.append((args.prompt, json_output))
        return 0

    monkeypatch.setattr(cli_main.sys, "stdin", StringIO("pipe"))
    monkeypatch.setattr(cli_main, "_run_one_shot", fake_run)

    assert cli_main.main(["run", "arg", "--json"]) == 0
    assert observed == [("pipe\n\narg", True)]


def test_run_json_payload_is_stable_and_does_not_copy_sensitive_metadata() -> None:
    answer = AssistantMessage(
        id="assistant-1",
        content="done",
        stop_reason="stop",
        metadata={
            "provider_state": {"secret": "must-not-leak"},
            "prompt": "must-not-leak",
        },
    )
    state = AgentRunState(
        run_id="run-1",
        end_reason="error",
        turns_used=7,
        max_turns=20,
        error="unsafe raw exception",
        error_info=ModelErrorInfo(
            kind="server",
            message="safe summary",
            provider="test",
            retryable=True,
            status_code=503,
            code="busy",
            request_id="request-1",
            metadata={"raw_request": "must-not-leak"},
        ),
    )
    harness = SimpleNamespace(
        session=SimpleNamespace(session_id="session-1"),
        agent=SimpleNamespace(last_run=state),
    )

    payload = cli_main.build_run_result(harness, answer)

    assert payload == {
        "schema_version": 1,
        "session_id": "session-1",
        "run_id": "run-1",
        "end_reason": "error",
        "turns_used": 7,
        "max_turns": 20,
        "assistant": {
            "id": "assistant-1",
            "content": "done",
            "stop_reason": "stop",
        },
        "error_info": {
            "kind": "server",
            "message": "safe summary",
            "provider": "test",
            "retryable": True,
            "status_code": 503,
            "code": "busy",
            "retry_after": None,
            "request_id": "request-1",
        },
    }
    serialized = json.dumps(payload)
    assert "must-not-leak" not in serialized
    assert "unsafe raw exception" not in serialized


def test_max_turns_cli_overrides_environment_and_rejects_invalid_values(
    monkeypatch,
) -> None:
    monkeypatch.setenv("EVOPI_MAX_TURNS", "37")

    assert cli_main.build_parser().parse_args([]).max_turns == 37
    assert cli_main.build_parser().parse_args(["--max-turns", "9"]).max_turns == 9
    with pytest.raises(SystemExit) as error:
        cli_main.build_parser().parse_args(["--max-turns", "0"])
    assert error.value.code == 2


def test_invalid_max_turns_environment_fails_during_argument_parsing(
    monkeypatch,
) -> None:
    monkeypatch.setenv("EVOPI_MAX_TURNS", "not-an-integer")

    with pytest.raises(SystemExit) as error:
        cli_main.build_parser().parse_args([])

    assert error.value.code == 2


def test_shell_cli_overrides_environment_and_rejects_unknown_mode(
    monkeypatch,
) -> None:
    monkeypatch.setenv("EVOPI_SHELL", "powershell")

    assert cli_main.build_parser().parse_args([]).shell == "powershell"
    assert cli_main.build_parser().parse_args(["--shell", "cmd"]).shell == "cmd"
    with pytest.raises(SystemExit) as error:
        cli_main.build_parser().parse_args(["--shell", "fish"])
    assert error.value.code == 2

    monkeypatch.setenv("EVOPI_SHELL", "fish")
    with pytest.raises(SystemExit) as environment_error:
        cli_main.build_parser().parse_args([])
    assert environment_error.value.code == 2


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("completed", 0),
        ("terminated", 0),
        ("error", 1),
        ("deadline_exceeded", 1),
        ("turn_limit", 1),
        ("aborted", 130),
    ],
)
def test_run_exit_code_follows_end_reason(reason: str, expected: int) -> None:
    assert cli_main.run_exit_code(reason) == expected


def test_explicit_plugin_path_emits_unreviewed_warning(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(
        "evopi.plugins.approved_plugin_entrypoints",
        lambda workspace: (),
    )
    args = argparse.Namespace(workspace=tmp_path, plugin=[tmp_path / "plugin.py"])

    cli_main._plugin_paths_from_args(args)

    warning = capsys.readouterr().err
    assert "deprecated" in warning.lower()
    assert "unreviewed" in warning.lower()
