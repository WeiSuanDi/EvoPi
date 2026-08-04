"""Concurrent REPL controller tests (SFU-2 Task 2).

The runner drives a structural fake Harness so the busy/idle state machine,
command gating, single-render queueing, and Task settling stay isolated from
model and Session behavior. Core and Harness suites cover the real binding.
"""

from __future__ import annotations

import asyncio
from io import StringIO
from types import SimpleNamespace

import pytest
from rich.console import Console

from evopi.cli.main import _TerminalEditor
from evopi.cli.repl import (
    ReplCommandContext,
    ReplCommandRegistry,
    ReplInputPreempted,
    ReplRunner,
    ReplStartupConfig,
)


class FakeReplHarness:
    """Structural stand-in for an interaction-enabled CodingHarness."""

    def __init__(self) -> None:
        self.prompted: list[str] = []
        self.steered: list[tuple[str, str]] = []
        self.followed_up: list[tuple[str, str]] = []
        self.aborted = False
        self.closed = False
        self.prompt_gate = asyncio.Event()
        self.steer_error: Exception | None = None
        self.session = SimpleNamespace(session_id="session-1")
        self.capabilities = SimpleNamespace(
            active_tool_names=("tool_a",),
            tool_names=("tool_a",),
            policy_names=("policy_a",),
            plugin_names=(),
            warnings=(),
        )
        self.resources = SimpleNamespace(
            memory=SimpleNamespace(entry_count=0),
            skills=(),
            subagent_enabled=False,
        )
        self.interaction_snapshot = SimpleNamespace(
            pending_steering_count=0,
            pending_follow_up_count=0,
        )

    async def prompt(self, content: str) -> object:
        self.prompted.append(content)
        await self.prompt_gate.wait()
        return None

    async def steer(self, content: str, *, origin: str = "api") -> object:
        if self.steer_error is not None:
            raise self.steer_error
        self.steered.append((content, origin))
        return SimpleNamespace(
            input_id="in-1", run_id="run-1", kind="steer", origin=origin, position=1
        )

    async def follow_up(self, content: str, *, origin: str = "api") -> object:
        self.followed_up.append((content, origin))
        return SimpleNamespace(
            input_id="in-2", run_id="run-1", kind="follow_up", origin=origin, position=1
        )

    def abort(self) -> None:
        self.aborted = True
        self.prompt_gate.set()

    def close(self) -> None:
        self.closed = True


class FakeDisplay:
    def __init__(self) -> None:
        self.events: list[str] = []

    def show_user_message(self, text: str) -> None:
        self.events.append(f"user:{text}")

    def start_run(self) -> None:
        self.events.append("start_run")

    def end_run(self) -> None:
        self.events.append("end_run")

    def set_status(self, text: str) -> None:
        self.events.append(f"status:{text}")

    def pause(self) -> None:
        self.events.append("pause")

    def resume(self) -> None:
        self.events.append("resume")


class ScriptedReader:
    """Scripted terminal reader; EOF when the queue is exhausted."""

    def __init__(self, *items: str | Exception) -> None:
        self._items = list(items)
        self.labels: list[str] = []

    async def __call__(self, label: str) -> str:
        self.labels.append(label)
        await asyncio.sleep(0)  # let the Run Task progress
        if not self._items:
            raise EOFError
        item = self._items.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _startup(tmp_path) -> ReplStartupConfig:
    return ReplStartupConfig(
        provider="test",
        model="test-model",
        base_url="https://example.test",
        workspace=str(tmp_path),
        session_mode="memory",
        retry_enabled=True,
        max_retries=3,
        deadline=None,
        tool_timeout=None,
        fallbacks=(),
        included_tools=None,
        excluded_tools=None,
        shell_mode="auto",
        shell_kind="cmd",
        shell_executable="cmd.exe",
    )


def _make_runner(
    tmp_path,
    harness: FakeReplHarness,
    *inputs: str | Exception,
    initial_prompt: str | None = None,
    display: FakeDisplay | None = None,
) -> tuple[ReplRunner, StringIO, FakeDisplay, ScriptedReader]:
    output = StringIO()
    console = Console(file=output, force_terminal=False, width=120)
    registry = ReplCommandRegistry()
    context = ReplCommandContext(
        harness=harness,  # type: ignore[arg-type]  # structural fake
        startup=_startup(tmp_path),
        display=None,
        console=console,
    )
    fake_display = display or FakeDisplay()
    reader = ScriptedReader(*inputs)
    runner = ReplRunner(
        harness=harness,  # type: ignore[arg-type]  # structural fake
        display=fake_display,
        console=console,
        registry=registry,
        context=context,
        read=reader,
        initial_prompt=initial_prompt,
    )
    return runner, output, fake_display, reader


def test_idle_text_starts_one_run(tmp_path) -> None:
    harness = FakeReplHarness()
    runner, _, _, _ = _make_runner(tmp_path, harness, "hello")

    assert asyncio.run(runner.run()) == 0
    assert harness.prompted == ["hello"]
    assert harness.steered == []
    # EOF settled the still-active Run by aborting it: no orphan Task.
    assert harness.aborted is True


def test_busy_text_queues_steering_instead_of_second_prompt(tmp_path) -> None:
    harness = FakeReplHarness()
    runner, _, _, _ = _make_runner(tmp_path, harness, "hello", "steer me")

    assert asyncio.run(runner.run()) == 0
    assert harness.prompted == ["hello"]
    assert harness.steered == [("steer me", "repl")]
    assert harness.aborted is True


def test_followup_command_queues_follow_up(tmp_path) -> None:
    harness = FakeReplHarness()
    runner, _, _, _ = _make_runner(tmp_path, harness, "hello", "/followup one more")

    assert asyncio.run(runner.run()) == 0
    assert harness.followed_up == [("one more", "repl")]
    assert harness.steered == []


def test_steer_command_queues_steering_with_arguments(tmp_path) -> None:
    harness = FakeReplHarness()
    runner, _, _, _ = _make_runner(tmp_path, harness, "hello", "/steer keep going")

    assert asyncio.run(runner.run()) == 0
    assert harness.steered == [("keep going", "repl")]


def test_steer_and_followup_reject_while_idle(tmp_path) -> None:
    for command in ("/steer x", "/followup x"):
        harness = FakeReplHarness()
        runner, output, _, _ = _make_runner(tmp_path, harness, command)

        assert asyncio.run(runner.run()) == 0
        assert harness.prompted == []
        assert harness.steered == []
        assert harness.followed_up == []
        assert "only accepted while a Run is active" in output.getvalue()


def test_steer_usage_required_while_busy(tmp_path) -> None:
    harness = FakeReplHarness()
    runner, output, _, _ = _make_runner(tmp_path, harness, "hello", "/steer")

    assert asyncio.run(runner.run()) == 0
    assert harness.steered == []
    assert "Usage: /steer TEXT" in output.getvalue()


def test_mutating_commands_reject_while_busy(tmp_path) -> None:
    harness = FakeReplHarness()
    runner, output, _, _ = _make_runner(
        tmp_path, harness, "hello", "/new", "/quit", "/clear"
    )

    assert asyncio.run(runner.run()) == 0
    assert harness.prompted == ["hello"]
    assert "Rejected while a Run is active" in output.getvalue()
    assert "Goodbye" not in output.getvalue()


def test_readonly_commands_run_while_busy(tmp_path) -> None:
    harness = FakeReplHarness()
    runner, output, _, _ = _make_runner(tmp_path, harness, "hello", "/status")

    assert asyncio.run(runner.run()) == 0
    rendered = output.getvalue()
    assert "Workbench Status" in rendered
    assert "Rejected while a Run is active" not in rendered


def test_abort_requests_abort_while_busy(tmp_path) -> None:
    harness = FakeReplHarness()
    runner, output, _, _ = _make_runner(tmp_path, harness, "hello", "/abort")

    assert asyncio.run(runner.run()) == 0
    assert harness.aborted is True
    assert "Abort requested" in output.getvalue()


def test_abort_while_idle_is_harmless(tmp_path) -> None:
    harness = FakeReplHarness()
    runner, output, _, _ = _make_runner(tmp_path, harness, "/abort")

    assert asyncio.run(runner.run()) == 0
    assert harness.aborted is False
    assert "No active Run to abort" in output.getvalue()


def test_quit_while_idle_returns_zero(tmp_path) -> None:
    harness = FakeReplHarness()
    runner, _, _, _ = _make_runner(tmp_path, harness, "/quit")

    assert asyncio.run(runner.run()) == 0
    assert harness.prompted == []


def test_keyboard_interrupt_returns_130_and_settles_run(tmp_path) -> None:
    harness = FakeReplHarness()
    runner, _, _, _ = _make_runner(tmp_path, harness, "hello", KeyboardInterrupt())

    assert asyncio.run(runner.run()) == 130
    assert harness.prompted == ["hello"]
    assert harness.aborted is True


def test_completed_run_is_followed_by_a_new_run(tmp_path) -> None:
    harness = FakeReplHarness()
    harness.prompt_gate.set()  # Runs complete naturally
    runner, _, _, _ = _make_runner(tmp_path, harness, "first", "second")

    assert asyncio.run(runner.run()) == 0
    assert harness.prompted == ["first", "second"]
    assert harness.steered == []


def test_retry_runs_most_recent_prompt(tmp_path) -> None:
    harness = FakeReplHarness()
    harness.prompt_gate.set()
    runner, _, _, _ = _make_runner(tmp_path, harness, "hello", "/retry")

    assert asyncio.run(runner.run()) == 0
    assert harness.prompted == ["hello", "hello"]


def test_accepted_steer_renders_once_as_queued(tmp_path) -> None:
    harness = FakeReplHarness()
    display = FakeDisplay()
    runner, _, display, _ = _make_runner(
        tmp_path, harness, "hello", "queued text", display=display
    )

    asyncio.run(runner.run())
    assert display.events.count("user:queued text") == 1
    assert display.events.count("user:hello") == 1
    assert display.events.count("start_run") == 1
    assert display.events.count("end_run") == 1


def test_failed_steer_renders_error_without_duplicate_panel(tmp_path) -> None:
    harness = FakeReplHarness()
    harness.steer_error = RuntimeError("queue is full")
    display = FakeDisplay()
    runner, output, display, _ = _make_runner(
        tmp_path, harness, "hello", "queued text", display=display
    )

    asyncio.run(runner.run())
    assert display.events.count("user:queued text") == 0
    assert "queue is full" in output.getvalue()


def test_initial_prompt_starts_run_without_reading(tmp_path) -> None:
    harness = FakeReplHarness()
    runner, _, _, reader = _make_runner(
        tmp_path, harness, initial_prompt="start now"
    )

    assert asyncio.run(runner.run()) == 0
    assert harness.prompted == ["start now"]
    # One read happens at the loop boundary to reach EOF; no input was consumed.
    assert reader.labels == ["> "]


def test_blank_input_is_ignored(tmp_path) -> None:
    harness = FakeReplHarness()
    runner, _, _, _ = _make_runner(tmp_path, harness, "   ")

    assert asyncio.run(runner.run()) == 0
    assert harness.prompted == []
    assert harness.steered == []


def test_modal_prompt_preempts_and_then_restores_background_editor() -> None:
    class FakePromptSession:
        def __init__(self) -> None:
            self.calls: list[str] = []
            self.first_started = asyncio.Event()
            self.never = asyncio.Event()
            self.active = 0
            self.max_active = 0

        async def prompt_async(self, label: str) -> str:
            self.calls.append(label)
            call_number = len(self.calls)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            try:
                if call_number == 1:
                    self.first_started.set()
                    await self.never.wait()
                if call_number == 2:
                    return "modal answer"
                return "resumed answer"
            finally:
                self.active -= 1

    async def scenario() -> None:
        session = FakePromptSession()
        editor = _TerminalEditor()
        editor.attach(session)  # type: ignore[arg-type]

        background = asyncio.create_task(editor.read("> "))
        await session.first_started.wait()

        assert await editor.modal_read("Confirm: ") == "modal answer"
        with pytest.raises(ReplInputPreempted):
            await background
        assert await editor.read("> ") == "resumed answer"
        assert session.calls == ["> ", "Confirm: ", "> "]
        assert session.max_active == 1

    asyncio.run(scenario())
