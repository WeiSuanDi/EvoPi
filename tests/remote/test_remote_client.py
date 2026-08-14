from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any, get_type_hints

import pytest

from evopi.remote import (
    EvoPiRemoteClient,
    RemoteFrameCodec,
    RemoteOutcomeUnknownError,
    RemoteProtocolError,
    RemoteRunHandle,
    create_auth_challenge,
    generate_device_key,
    remote_frame,
    submit_remote_pairing,
)
from evopi.rpc import RpcInteractionReceipt, RpcServerInfo
from evopi.rpc.codec_v2 import decode_v2_request, encode_v2_response
from evopi.rpc.protocol_v2 import RpcV2Response


class _FakeWebSocket:
    stream_id = "11111111-1111-1111-1111-111111111111"

    def __init__(self, private_key: Any) -> None:
        self.private_key = private_key
        self.incoming: asyncio.Queue[str | None] = asyncio.Queue()
        self.closed = False
        self.connection_id = "c" * 32
        self.ignore_lease_requests = False
        self.rpc_methods: list[str] = []
        self.lease_expires_at: str | None = None

    async def send(self, payload: str) -> None:
        try:
            frame = RemoteFrameCodec.decode(payload)
        except Exception:
            request = decode_v2_request(payload)
            self.rpc_methods.append(request.method)
            if request.method == "initialize":
                result = {
                    "protocol": "evopi.rpc.v2",
                    "schema_version": 2,
                    "host_id": "host-1",
                    "session_id": "session-1",
                    "stream": {
                        "stream_id": self.stream_id,
                        "cursor": 0,
                        "oldest_sequence": 0,
                        "latest_sequence": 0,
                        "capacity": 1000,
                    },
                    "active_tool_names": [],
                    "policy_names": [],
                    "capabilities": {
                        "event_replay": True,
                        "confirmation": True,
                        "text_steering": True,
                        "text_follow_up": True,
                    },
                    "steering_mode": "one-at-a-time",
                    "follow_up_mode": "one-at-a-time",
                }
            elif request.method == "runtime.status":
                result = {
                    "active_run_id": None,
                    "lifecycle": "idle",
                    "session_id": "session-1",
                    "pending_confirmation_count": 0,
                    "last_end_reason": None,
                    "last_run_error": None,
                    "steering_mode": "one-at-a-time",
                    "follow_up_mode": "one-at-a-time",
                    "pending_steering_count": 0,
                    "pending_follow_up_count": 0,
                }
            elif request.method == "shutdown":
                await self.incoming.put(
                    encode_v2_response(
                        RpcV2Response(
                            request_id=request.request_id,
                            ok=False,
                            error=None,
                        )
                    )
                )
                return
            else:
                raise AssertionError(request.method)
            await self.incoming.put(
                encode_v2_response(
                    RpcV2Response(request_id=request.request_id, ok=True, result=result)
                )
            )
            return
        if frame.type == "auth.begin":
            challenge = create_auth_challenge(
                host_id="h" * 32,
                device_id=frame.data["device_id"],
                connection_id=self.connection_id,
            )
            await self.incoming.put(
                RemoteFrameCodec.encode(
                    remote_frame(
                        "auth.challenge",
                        frame.request_id,
                        {
                            "challenge": {
                                "protocol": challenge.protocol,
                                "host_id": challenge.host_id,
                                "device_id": challenge.device_id,
                                "connection_id": challenge.connection_id,
                                "nonce": challenge.nonce,
                                "issued_at": challenge.issued_at.isoformat(),
                                "expires_at": challenge.expires_at.isoformat(),
                            }
                        },
                    )
                )
            )
        elif frame.type == "auth.complete":
            await self.incoming.put(
                RemoteFrameCodec.encode(
                    remote_frame(
                        "auth.ok",
                        frame.request_id,
                        {"device_id": "device-1", "scopes": ["observe", "control"]},
                    )
                )
            )
        elif frame.type == "lease.acquire":
            if self.ignore_lease_requests:
                return
            await self.incoming.put(
                RemoteFrameCodec.encode(
                    remote_frame(
                        "lease.granted",
                        frame.request_id,
                        {
                            "connection_id": self.connection_id,
                            "expires_at": self.lease_expires_at
                            or (datetime.now(UTC) + timedelta(seconds=30)).isoformat(),
                            "revision": 1,
                        },
                    )
                )
            )
        elif frame.type == "pairing.submit":
            await self.incoming.put(
                RemoteFrameCodec.encode(
                    remote_frame(
                        "pairing.pending",
                        frame.request_id,
                        {"request_id": "pending-1"},
                    )
                )
            )
        elif frame.type == "events.page":
            sequence = frame.data["after_sequence"] + 1
            await self.incoming.put(
                RemoteFrameCodec.encode(
                    remote_frame(
                        "events.page",
                        frame.request_id,
                        {
                            "stream_id": self.stream_id,
                            "after_sequence": frame.data["after_sequence"],
                            "snapshot_latest": sequence,
                            "next_sequence": sequence,
                            "complete": True,
                            "events": [
                                {
                                    "event_id": "22222222-2222-2222-2222-222222222222",
                                    "stream_id": self.stream_id,
                                    "sequence": sequence,
                                    "type": "plugin.extension.event",
                                    "data": {"safe": True},
                                    "run_id": None,
                                    "created_at": datetime.now(UTC).isoformat(),
                                    "schema_version": 2,
                                }
                            ],
                        },
                    )
                )
            )

    async def recv(self) -> str:
        value = await self.incoming.get()
        if value is None:
            raise ConnectionError("closed")
        return value

    async def close(self) -> None:
        self.closed = True
        await self.incoming.put(None)


def test_remote_client_preserves_rpc_public_result_types() -> None:
    assert get_type_hints(RemoteRunHandle.steer)["return"] is RpcInteractionReceipt
    assert get_type_hints(RemoteRunHandle.follow_up)["return"] is RpcInteractionReceipt
    getter = EvoPiRemoteClient.server_info.fget
    assert getter is not None
    assert get_type_hints(getter)["return"] is RpcServerInfo


def test_remote_client_authenticates_composes_rpc_and_acquires_lease() -> None:
    async def scenario() -> None:
        private_key = generate_device_key()
        socket = _FakeWebSocket(private_key)
        client = await EvoPiRemoteClient.connect(
            socket,
            device_id="device-1",
            private_key=private_key,
            owns_transport=True,
        )

        assert client.device_id == "device-1"
        assert (await client.runtime_status()).session_id == "session-1"
        lease = await client.acquire_control()
        assert lease.revision == 1
        await client.aclose()
        assert socket.closed is True
        assert "shutdown" not in socket.rpc_methods

    asyncio.run(scenario())


def test_remote_client_close_completes_pending_remote_requests() -> None:
    async def scenario() -> None:
        private_key = generate_device_key()
        socket = _FakeWebSocket(private_key)
        socket.ignore_lease_requests = True
        client = await EvoPiRemoteClient.connect(
            socket,
            device_id="device-1",
            private_key=private_key,
            owns_transport=True,
        )

        pending = asyncio.create_task(client.acquire_control())
        await asyncio.sleep(0)
        await client.aclose()

        with pytest.raises(RemoteOutcomeUnknownError):
            await asyncio.wait_for(pending, timeout=0.1)

    asyncio.run(scenario())


def test_remote_client_rejects_naive_lease_timestamp() -> None:
    async def scenario() -> None:
        private_key = generate_device_key()
        socket = _FakeWebSocket(private_key)
        socket.lease_expires_at = "2026-08-14T12:00:30"
        client = await EvoPiRemoteClient.connect(
            socket,
            device_id="device-1",
            private_key=private_key,
            owns_transport=True,
        )

        with pytest.raises(RemoteProtocolError, match="timestamp"):
            await client.acquire_control()
        await client.aclose()

    asyncio.run(scenario())


def test_remote_client_can_submit_pairing_without_authentication() -> None:
    async def scenario() -> None:
        private_key = generate_device_key()
        socket = _FakeWebSocket(private_key)

        result = await submit_remote_pairing(
            socket,
            code="ABCD-EFGH-JKLM",
            device_name="laptop",
            public_jwk={"kty": "EC", "crv": "P-256", "x": "x", "y": "y"},
        )

        assert result.request_id == "pending-1"

    asyncio.run(scenario())


def test_remote_client_uses_remote_event_pages_before_live_delivery() -> None:
    async def scenario() -> None:
        private_key = generate_device_key()
        socket = _FakeWebSocket(private_key)
        client = await EvoPiRemoteClient.connect(
            socket,
            device_id="device-1",
            private_key=private_key,
            owns_transport=True,
        )

        iterator = client.events()
        event = await anext(iterator)
        assert event.cursor.stream_id == socket.stream_id
        assert event.cursor.sequence == 1
        assert event.event_type == "plugin.extension.event"
        await iterator.aclose()
        await client.aclose()

    asyncio.run(scenario())
