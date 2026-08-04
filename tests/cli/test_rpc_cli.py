"""Product routing and stdio adapter tests for ``evopi rpc``."""

from __future__ import annotations

import asyncio
import importlib
from io import StringIO
from types import SimpleNamespace
from typing import Any

from evopi.cli.main import main
from evopi.cli.product import build_product_parser
from evopi.cli.rpc import StdioTextReader, StdioTextWriter
from evopi.harness import ConfirmationBroker

cli_main = importlib.import_module("evopi.cli.main")


def test_product_help_lists_local_rpc_host() -> None:
    help_text = build_product_parser().format_help()

    assert "evopi rpc" in help_text
    assert "local JSONL host over stdio" in help_text


def test_rpc_route_builds_broker_harness_without_terminal_handler(monkeypatch) -> None:
    observed: dict[str, Any] = {}
    sentinel_harness = SimpleNamespace()

    def fake_build(args, **kwargs):
        observed["handler"] = kwargs["confirmation_handler"]
        observed["broker"] = kwargs["confirmation_broker"]
        return sentinel_harness

    async def fake_run(harness, broker):
        observed["harness"] = harness
        observed["run_broker"] = broker
        return 0

    monkeypatch.setattr(cli_main, "_build_harness", fake_build)
    monkeypatch.setattr(cli_main, "run_stdio_rpc", fake_run)

    assert main(["rpc", "--no-session"]) == 0
    assert observed["handler"] is None
    assert isinstance(observed["broker"], ConfirmationBroker)
    assert observed["harness"] is sentinel_harness
    assert observed["run_broker"] is observed["broker"]


def test_rpc_rejects_positional_prompt_before_harness_creation(
    monkeypatch,
    capsys,
) -> None:
    def unexpected(*args, **kwargs):
        raise AssertionError("Harness must not be constructed")

    monkeypatch.setattr(cli_main, "_build_harness", unexpected)

    assert main(["rpc", "not-allowed"]) == 2
    assert "does not accept a positional prompt" in capsys.readouterr().err


def test_stdio_text_adapters_preserve_lines_and_do_not_close_streams() -> None:
    async def scenario() -> None:
        source = StringIO("first\nsecond\n")
        target = StringIO()
        reader = StdioTextReader(source)
        writer = StdioTextWriter(target)

        assert await reader.readline() == "first\n"
        assert await reader.readline() == "second\n"
        assert await reader.readline() == ""
        await writer.write("response\n")
        await writer.flush()

        assert target.getvalue() == "response\n"
        assert source.closed is False
        assert target.closed is False

    asyncio.run(scenario())
