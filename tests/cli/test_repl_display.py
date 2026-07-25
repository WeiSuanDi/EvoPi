from __future__ import annotations

import io
from importlib import import_module
from types import SimpleNamespace

from prompt_toolkit.input import DummyInput
from prompt_toolkit.output import DummyOutput

from evopi.cli.display import ReplDisplay

cli_main = import_module("evopi.cli.main")


def test_repl_prompt_session_erases_the_submitted_edit_line() -> None:
    session = cli_main._create_repl_prompt_session(
        input=DummyInput(),
        output=DummyOutput(),
    )

    assert session.app.erase_when_done is True


def test_repl_display_uses_one_rich_output_region_for_streaming_and_final_markdown(
    monkeypatch,
) -> None:
    stdout = io.StringIO()
    stderr = io.StringIO()
    monkeypatch.setattr("evopi.cli.display.sys.stdout", stdout)
    monkeypatch.setattr("evopi.cli.display.sys.stderr", stderr)
    display = ReplDisplay()

    display.start_run()
    display.handle_event(
        SimpleNamespace(
            type="message_update",
            data={"role": "assistant", "kind": "text", "delta": "## 标题"},
        )
    )
    display.end_run()

    assert stdout.getvalue() == ""
    assert "标题" in stderr.getvalue()
