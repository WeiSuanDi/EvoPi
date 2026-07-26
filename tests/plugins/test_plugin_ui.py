from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from evopi.cli.plugin_ui import ReplPluginUI
from evopi.core.context import AgentContext
from evopi.core.events import CoreEvent
from evopi.core.messages import AssistantMessage
from evopi.core.stream import ModelComplete, ModelStreamEvent
from evopi.harness import BaseHarness
from evopi.plugins import NullPluginUI, PluginUIUnavailableError


class _Model:
    name = "ui-test"

    async def stream(self, context: AgentContext) -> AsyncIterator[ModelStreamEvent]:
        yield ModelComplete(
            message=AssistantMessage(content="done", stop_reason="stop")
        )


class _Display:
    def __init__(self) -> None:
        self.paused = 0
        self.resumed = 0
        self.status: dict[str, str] = {}

    def pause(self) -> None:
        self.paused += 1

    def resume(self) -> None:
        self.resumed += 1

    def set_plugin_status(self, key: str, text: str | None) -> None:
        if text is None:
            self.status.pop(key, None)
        else:
            self.status[key] = text


def test_null_plugin_ui_fails_closed() -> None:
    ui = NullPluginUI()

    async def run() -> None:
        assert await ui.confirm("Execute", "Continue?") is False
        with pytest.raises(PluginUIUnavailableError):
            await ui.select("Pick", ["a", "b"])
        with pytest.raises(PluginUIUnavailableError):
            await ui.input("Name")

    asyncio.run(run())


def test_repl_plugin_ui_pauses_live_region_for_modal_interactions() -> None:
    display = _Display()
    responses = iter(["y", "2", "typed"])

    async def prompt(text: str) -> str:
        return next(responses)

    ui = ReplPluginUI(display=display, prompt=prompt)

    async def run() -> None:
        assert await ui.confirm("Execute", "Continue?") is True
        assert await ui.select("Mode", ["plan", "execute"]) == "execute"
        assert await ui.input("Value", "Enter") == "typed"
        await ui.set_status("mode", "PLAN")
        await ui.set_status("mode", None)

    asyncio.run(run())

    assert display.paused == 3
    assert display.resumed == 3
    assert display.status == {}


def test_harness_attaches_ui_without_exposing_private_fields(tmp_path: Path) -> None:
    plugin_path = tmp_path / "ui_plugin.py"
    plugin_path.write_text(
        """
from evopi.plugins import Plugin, PluginAPI, PluginMetadata
class UIPlugin(Plugin):
    @property
    def meta(self):
        return PluginMetadata(name="ui-plugin")
    def register(self, api: PluginAPI):
        async def execute(args, context):
            approved = await api.ui.confirm("Execute", "Continue?")
            api.state.set("approved", approved)
        api.register_command("execute", execute)
""",
        encoding="utf-8",
    )

    class ApprovingUI(NullPluginUI):
        async def confirm(self, title: str, message: str) -> bool:
            return True

    harness = BaseHarness(model=_Model(), plugin_paths=[plugin_path])
    harness.attach_plugin_ui(ApprovingUI())
    events: list[CoreEvent] = []
    harness.subscribe(events.append)

    asyncio.run(harness.dispatch_plugin_command("/execute"))

    assert harness.session.plugin_state("ui-plugin") == {"approved": True}
    ui_events = [
        event for event in events if event.type.startswith("plugin_ui_")
    ]
    assert [event.type for event in ui_events] == [
        "plugin_ui_request",
        "plugin_ui_response",
    ]
    assert ui_events[0].data == {
        "plugin": "ui-plugin",
        "operation": "confirm",
    }
    assert ui_events[1].data == {
        "plugin": "ui-plugin",
        "operation": "confirm",
        "success": True,
        "approved": True,
    }
    assert "Execute" not in repr(ui_events)
    assert "Continue" not in repr(ui_events)
