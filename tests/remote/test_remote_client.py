from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from evopi.remote import (
    EvoPiRemoteClient,
    RemoteFrameCodec,
    create_auth_challenge,
    generate_device_key,
    remote_frame,
)
from evopi.rpc.codec_v2 import decode_v2_request, encode_v2_response
from evopi.rpc.protocol_v2 import RpcV2Response


class _FakeWebSocket:
    def __init__(self, private_key: Any) -> None:
        self.private_key = private_key
        self.incoming: asyncio.Queue[str | None] = asyncio.Queue()
        self.closed = False
        self.connection_id = "c" * 32

    async def send(self, payload: str) -> None:
        try:
            frame = RemoteFrameCodec.decode(payload)
        except Exception:
            request = decode_v2_request(payload)
            if request.method == "initialize":
                result = {
                    "protocol": "evopi.rpc.v2",
                    "schema_version": 2,
                    "host_id": "host-1",
                    "session_id": "session-1",
                    "stream": {
                        "stream_id": "stream-1",
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
            await self.incoming.put(
                RemoteFrameCodec.encode(
                    remote_frame(
                        "lease.granted",
                        frame.request_id,
                        {
                            "connection_id": self.connection_id,
                            "expires_at": (datetime.now(UTC) + timedelta(seconds=30)).isoformat(),
                            "revision": 1,
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

    asyncio.run(scenario())
