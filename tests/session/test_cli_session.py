from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import AsyncIterator
from importlib import import_module
from pathlib import Path

import pytest

from evopi.cli.session import session_list_main
from evopi.core.context import AgentContext
from evopi.core.messages import AssistantMessage
from evopi.core.stream import ModelComplete, ModelStreamEvent
from evopi.session import SessionManager

cli_main = import_module("evopi.cli.main")


class ContextRecordingModel:
    name = "cli-session-model"

    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.contexts: list[AgentContext] = []

    async def stream(
        self, context: AgentContext
    ) -> AsyncIterator[ModelStreamEvent]:
        self.contexts.append(context)
        yield ModelComplete(
            message=AssistantMessage(content=self.answer, stop_reason="stop")
        )


def prompt_args(
    *,
    prompt: str,
    workspace: Path,
    root: Path,
    new_session: bool = False,
    session: str | None = None,
    no_session: bool = False,
) -> argparse.Namespace:
    return argparse.Namespace(
        prompt=prompt,
        provider=None,
        workspace=workspace,
        trace=workspace / "trace.jsonl",
        no_retry=True,
        max_retries=0,
        model_timeout=5.0,
        new_session=new_session,
        session=session,
        no_session=no_session,
        session_root=root,
    )


def test_prompt_parser_session_selection_is_mutually_exclusive() -> None:
    parser = cli_main.build_parser()
    parsed = parser.parse_args(
        ["hello", "--new-session", "--session-root", "sessions"]
    )
    assert parsed.new_session is True
    assert parsed.session_root == Path("sessions")

    with pytest.raises(SystemExit):
        parser.parse_args(["hello", "--new-session", "--no-session"])
    with pytest.raises(SystemExit):
        parser.parse_args(["hello", "--session", "abc", "--no-session"])


def test_cli_auto_continues_latest_session_across_invocations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = tmp_path / "sessions"
    first_model = ContextRecordingModel("first answer")
    second_model = ContextRecordingModel("second answer")
    models = iter([first_model, second_model])
    monkeypatch.setattr(
        cli_main,
        "model_from_environment",
        lambda provider, *, timeout: next(models),
    )

    assert (
        asyncio.run(
            cli_main._run(
                prompt_args(
                    prompt="first question",
                    workspace=workspace,
                    root=root,
                )
            )
        )
        == 0
    )
    assert (
        asyncio.run(
            cli_main._run(
                prompt_args(
                    prompt="second question",
                    workspace=workspace,
                    root=root,
                )
            )
        )
        == 0
    )

    assert [message.content for message in second_model.contexts[0].messages[-3:]] == [
        "first question",
        "first answer",
        "second question",
    ]
    summaries = SessionManager.list(workspace=workspace, root=root)
    assert len(summaries) == 1
    assert summaries[0].message_count == 4
    output = capsys.readouterr()
    assert "EvoPi session" not in output.out
    assert output.err.count("EvoPi session") == 2


def test_session_selection_supports_new_explicit_and_ephemeral(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = tmp_path / "sessions"
    created = cli_main._session_manager_from_args(
        prompt_args(
            prompt="new",
            workspace=workspace,
            root=root,
            new_session=True,
        )
    )
    session_id = created.session_id
    session_path = created.session_path
    assert session_path is not None
    created.close()

    explicit_id = cli_main._session_manager_from_args(
        prompt_args(
            prompt="open",
            workspace=workspace,
            root=root,
            session=session_id,
        )
    )
    assert explicit_id.session_id == session_id
    explicit_id.close()

    explicit_path = cli_main._session_manager_from_args(
        prompt_args(
            prompt="open path",
            workspace=workspace,
            root=root,
            session=str(session_path),
        )
    )
    assert explicit_path.session_id == session_id
    explicit_path.close()

    ephemeral = cli_main._session_manager_from_args(
        prompt_args(
            prompt="temporary",
            workspace=workspace,
            root=root,
            no_session=True,
        )
    )
    assert ephemeral.is_persistent is False
    ephemeral.close()


def test_session_list_supports_workspace_all_and_json(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "sessions"
    first_workspace = tmp_path / "first"
    second_workspace = tmp_path / "second"
    first_workspace.mkdir()
    second_workspace.mkdir()
    first = SessionManager.create(first_workspace, root=root)
    first_id = first.session_id
    first.close()
    second = SessionManager.create(second_workspace, root=root)
    second_id = second.session_id
    second.close()

    assert (
        session_list_main(
            [
                "--workspace",
                str(first_workspace),
                "--session-root",
                str(root),
                "--json",
            ]
        )
        == 0
    )
    workspace_payload = json.loads(capsys.readouterr().out)
    assert [item["session_id"] for item in workspace_payload] == [first_id]

    assert (
        session_list_main(
            ["--all", "--session-root", str(root), "--json"]
        )
        == 0
    )
    all_payload = json.loads(capsys.readouterr().out)
    assert {item["session_id"] for item in all_payload} == {
        first_id,
        second_id,
    }
