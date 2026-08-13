"""Real ``evopi rpc`` subprocess handshake tests without model/network I/O."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from evopi.rpc import EvoPiRpcClient, RpcSubprocessConfig
from evopi.rpc.errors import RpcSubprocessError


def test_spawn_initializes_real_cli_host_without_calling_model(tmp_path) -> None:
    async def scenario() -> None:
        repository = Path(__file__).resolve().parents[2]
        command = (
            sys.executable,
            "-c",
            "from evopi.cli.main import main; raise SystemExit(main())",
            "rpc",
            "--no-session",
            "--no-memory",
            "--no-evolved-policies",
            "--provider",
            "openai-compatible",
            "--model",
            "mock-model",
            "--base-url",
            "http://127.0.0.1:9/v1",
            "--workspace",
            str(tmp_path),
        )
        client = await EvoPiRpcClient.spawn(
            RpcSubprocessConfig(
                command=command,
                cwd=tmp_path,
                env={
                    "OPENAI_API_KEY": "test-only-placeholder",
                    "PYTHONPATH": str(repository),
                },
            )
        )

        status = await client.runtime_status()

        assert client.server_info.session_id
        assert status.session_id == client.server_info.session_id
        await client.aclose()

    asyncio.run(scenario())


def test_spawn_reports_structured_start_failure() -> None:
    async def scenario() -> None:
        with pytest.raises(RpcSubprocessError):
            await EvoPiRpcClient.spawn(
                RpcSubprocessConfig(command=("evopi-command-that-does-not-exist",))
            )

    asyncio.run(scenario())
