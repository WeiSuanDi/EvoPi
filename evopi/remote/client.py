"""Typed asynchronous Python client for the Remote Gateway v1 transport."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, TypeVar, cast
from uuid import uuid4

from evopi.rpc import (
    EvoPiRpcClient,
    RpcClientEvent,
    RpcConnectionClosedError,
    RpcEventCursor,
    RpcConfirmationAck,
    RpcConfirmationAnswer,
    RpcConfirmationRecord,
    RpcInteractionReceipt,
    RpcRunHandle,
    RpcRunResult,
    RpcRuntimeStatus,
    RpcServerInfo,
    decode_v2_envelope,
    decode_v2_event,
)
from evopi.rpc.client_codec import client_event_from_wire

from .crypto import sign_auth_challenge
from .errors import RemoteConnectionError, RemoteOutcomeUnknownError
from .gateway import REMOTE_SUBPROTOCOL, challenge_from_dict
from .protocol import RemoteFrame, RemoteFrameCodec, RemoteProtocolError, remote_frame


_T = TypeVar("_T")


class RemoteWebSocket(Protocol):
    async def send(self, payload: str) -> None: ...
    async def recv(self) -> str: ...
    async def close(self) -> None: ...


@dataclass(slots=True, frozen=True, kw_only=True)
class RemoteClientConfig:
    url: str
    device_id: str
    private_key: Any
    origin: str | None = None
    handshake_timeout: float = 30.0


@dataclass(slots=True, frozen=True, kw_only=True)
class RemoteLeaseInfo:
    connection_id: str
    expires_at: datetime
    revision: int


@dataclass(slots=True, frozen=True, kw_only=True)
class RemotePairingSubmission:
    request_id: str


async def submit_remote_pairing(
    socket: RemoteWebSocket,
    *,
    code: str,
    device_name: str,
    public_jwk: dict[str, str],
    timeout: float = 30.0,
) -> RemotePairingSubmission:
    """Submit a public key for local approval without creating a trusted client."""

    request_id = uuid4().hex
    await socket.send(
        RemoteFrameCodec.encode(
            remote_frame(
                "pairing.submit",
                request_id,
                {"code": code, "device_name": device_name, "public_jwk": public_jwk},
            )
        )
    )
    response = RemoteFrameCodec.decode(await asyncio.wait_for(socket.recv(), timeout=timeout))
    pending_id = response.data.get("request_id")
    if (
        response.type != "pairing.pending"
        or response.request_id != request_id
        or not isinstance(pending_id, str)
        or not pending_id
    ):
        raise RemoteProtocolError("Gateway returned an invalid pairing response")
    return RemotePairingSubmission(request_id=pending_id)


class _RpcReader:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[str] = asyncio.Queue()

    async def readline(self) -> str:
        return await self.queue.get()


class _RpcWriter:
    def __init__(self, socket: RemoteWebSocket) -> None:
        self.socket = socket

    async def write(self, text: str) -> None:
        await self.socket.send(text.removesuffix("\n"))

    async def flush(self) -> None:
        return None


class _RemoteTransport:
    def __init__(self, socket: RemoteWebSocket, *, owns_transport: bool) -> None:
        self.socket = socket
        self.owns_transport = owns_transport
        self.reader = _RpcReader()
        self.writer = _RpcWriter(socket)
        self.pending: dict[str, asyncio.Future[RemoteFrame]] = {}
        self.task: asyncio.Task[None] | None = None
        self.error: Exception | None = None
        self.closed = False

    def start(self) -> None:
        self.task = asyncio.create_task(self._read_loop())

    async def request(self, frame_type: str, data: dict[str, Any]) -> RemoteFrame:
        if self.closed:
            raise RemoteConnectionError("Remote transport is closed")
        request_id = uuid4().hex
        future: asyncio.Future[RemoteFrame] = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        sent = False
        try:
            await self.socket.send(
                RemoteFrameCodec.encode(remote_frame(frame_type, request_id, data))
            )
            sent = True
            return await future
        except BaseException as exc:
            self.pending.pop(request_id, None)
            if sent and isinstance(exc, (ConnectionError, RemoteConnectionError)):
                raise RemoteOutcomeUnknownError(
                    f"outcome of {frame_type} is unknown"
                ) from exc
            raise

    async def aclose(self) -> None:
        if self.closed:
            return
        self.closed = True
        self._fail_pending()
        if self.task is not None and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        await self.reader.queue.put("")
        if self.owns_transport:
            await self.socket.close()

    async def _read_loop(self) -> None:
        try:
            while True:
                payload = await self.socket.recv()
                try:
                    decode_v2_envelope(payload)
                except Exception:
                    frame = RemoteFrameCodec.decode(payload)
                    future = self.pending.pop(frame.request_id, None)
                    if future is None:
                        if frame.type == "error":
                            raise RemoteConnectionError(
                                cast(str, frame.data.get("message", "remote error"))
                            )
                        continue
                    if not future.done():
                        future.set_result(frame)
                else:
                    await self.reader.queue.put(payload + "\n")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.error = exc
            self.closed = True
            self._fail_pending()
            await self.reader.queue.put("")

    def _fail_pending(self) -> None:
        for future in tuple(self.pending.values()):
            if not future.done():
                future.set_exception(RemoteConnectionError("Remote connection closed"))
        self.pending.clear()


class RemoteRunHandle:
    def __init__(self, handle: RpcRunHandle, client: EvoPiRemoteClient) -> None:
        self._handle = handle
        self._client = client
        self.run_id = handle.run_id

    @property
    def done(self) -> bool:
        return self._handle.done

    async def events(self) -> AsyncIterator[RpcClientEvent]:
        async for event in self._client.events(after=self._handle.start_cursor):
            if event.run_id == self.run_id:
                yield event
                if event.event_type == "agent_end":
                    return

    async def wait(self) -> RpcRunResult:
        return await self._handle.wait()

    async def steer(self, content: str) -> RpcInteractionReceipt:
        return await _unknown_on_disconnect("run.steer", self._handle.steer(content))

    async def follow_up(self, content: str) -> RpcInteractionReceipt:
        return await _unknown_on_disconnect(
            "run.follow_up", self._handle.follow_up(content)
        )

    async def abort(self) -> bool:
        return await _unknown_on_disconnect("run.abort", self._handle.abort())


class EvoPiRemoteClient:
    """Device-authenticated transport composed with ``EvoPiRpcClient``."""

    def __init__(
        self,
        transport: _RemoteTransport,
        rpc: EvoPiRpcClient,
        *,
        device_id: str,
        scopes: tuple[str, ...],
        config: RemoteClientConfig | None = None,
    ) -> None:
        self._transport = transport
        self.rpc = rpc
        self.device_id = device_id
        self.scopes = scopes
        self._config = config

    @classmethod
    async def connect(
        cls,
        socket: RemoteWebSocket,
        *,
        device_id: str,
        private_key: Any,
        owns_transport: bool = False,
        handshake_timeout: float = 30.0,
    ) -> EvoPiRemoteClient:
        begin_id = uuid4().hex
        await socket.send(
            RemoteFrameCodec.encode(
                remote_frame("auth.begin", begin_id, {"device_id": device_id})
            )
        )
        challenge_frame = RemoteFrameCodec.decode(
            await asyncio.wait_for(socket.recv(), timeout=handshake_timeout)
        )
        if challenge_frame.type != "auth.challenge" or challenge_frame.request_id != begin_id:
            raise RemoteProtocolError("Gateway returned an invalid authentication challenge")
        challenge_data = challenge_frame.data.get("challenge")
        if not isinstance(challenge_data, dict):
            raise RemoteProtocolError("Gateway returned an invalid authentication challenge")
        challenge = challenge_from_dict(challenge_data)
        complete_id = uuid4().hex
        await socket.send(
            RemoteFrameCodec.encode(
                remote_frame(
                    "auth.complete",
                    complete_id,
                    {"signature": sign_auth_challenge(private_key, challenge)},
                )
            )
        )
        authenticated = RemoteFrameCodec.decode(
            await asyncio.wait_for(socket.recv(), timeout=handshake_timeout)
        )
        if authenticated.type != "auth.ok" or authenticated.request_id != complete_id:
            raise RemoteProtocolError("Gateway rejected device authentication")
        scopes = authenticated.data.get("scopes")
        if not isinstance(scopes, list) or any(not isinstance(item, str) for item in scopes):
            raise RemoteProtocolError("Gateway returned invalid device scopes")
        transport = _RemoteTransport(socket, owns_transport=owns_transport)
        transport.start()
        try:
            rpc = await EvoPiRpcClient.connect(
                transport.reader,
                transport.writer,
                owns_transport=False,
                handshake_timeout=handshake_timeout,
                request_shutdown_on_close=False,
            )
        except BaseException:
            await transport.aclose()
            raise
        return cls(transport, rpc, device_id=device_id, scopes=tuple(scopes))

    @classmethod
    async def open(cls, config: RemoteClientConfig) -> EvoPiRemoteClient:
        try:
            from websockets.asyncio.client import connect
        except ImportError as exc:  # pragma: no cover
            raise RemoteConnectionError("install EvoPi with the 'remote' feature") from exc
        socket = await connect(
            config.url,
            subprotocols=cast(Any, [REMOTE_SUBPROTOCOL]),
            origin=cast(Any, config.origin),
            compression=None,
            max_size=128 * 1024,
        )
        try:
            client = await cls.connect(
                cast(RemoteWebSocket, socket),
                device_id=config.device_id,
                private_key=config.private_key,
                owns_transport=True,
                handshake_timeout=config.handshake_timeout,
            )
            client._config = config
            return client
        except BaseException:
            await socket.close()
            raise

    @property
    def server_info(self) -> RpcServerInfo:
        return self.rpc.server_info

    async def runtime_status(self) -> RpcRuntimeStatus:
        return await self.rpc.runtime_status()

    async def acquire_control(self) -> RemoteLeaseInfo:
        frame = await self._transport.request("lease.acquire", {})
        return _lease_from_frame(frame)

    async def renew_control(self) -> RemoteLeaseInfo:
        frame = await self._transport.request("lease.renew", {})
        return _lease_from_frame(frame)

    async def release_control(self) -> bool:
        frame = await self._transport.request("lease.release", {})
        return frame.type == "lease.released" and frame.data.get("released") is True

    async def start_run(self, prompt: str) -> RemoteRunHandle:
        handle = await _unknown_on_disconnect("run.start", self.rpc.start_run(prompt))
        return RemoteRunHandle(handle, self)

    async def events(
        self, *, after: RpcEventCursor | None = None
    ) -> AsyncIterator[RpcClientEvent]:
        cursor = after or self.rpc.server_info.cursor
        async for event in self._replay_pages(cursor):
            cursor = event.cursor
            yield event
        async for event in self.rpc.live_events(cursor):
            yield event

    async def resilient_events(
        self, *, after: RpcEventCursor | None = None
    ) -> AsyncIterator[RpcClientEvent]:
        """Reconnect only the observation transport, never a side-effect request."""

        cursor = after or self.rpc.server_info.cursor
        while True:
            try:
                async for event in self.events(after=cursor):
                    cursor = event.cursor
                    yield event
                return
            except RpcConnectionClosedError:
                await self._reconnect_observation(cursor)

    async def list_confirmations(self) -> tuple[RpcConfirmationRecord, ...]:
        return await self.rpc.list_confirmations()

    async def respond_confirmation(
        self, answer: RpcConfirmationAnswer
    ) -> RpcConfirmationAck:
        result = await _unknown_on_disconnect(
            "confirmation.respond", self.rpc.respond_confirmation(answer)
        )
        return result

    async def aclose(self) -> None:
        await self.rpc.aclose()
        await self._transport.aclose()

    async def _reconnect_observation(self, cursor: RpcEventCursor) -> None:
        config = self._config
        if config is None:
            raise RemoteConnectionError("automatic reconnect requires RemoteClientConfig")
        replacement = await type(self).open(config)
        if replacement.rpc.server_info.cursor.stream_id != cursor.stream_id:
            await replacement.aclose()
            raise RemoteConnectionError("Remote Host event stream changed")
        await self.rpc.aclose()
        await self._transport.aclose()
        self.rpc = replacement.rpc
        self._transport = replacement._transport

    async def _replay_pages(
        self, cursor: RpcEventCursor
    ) -> AsyncIterator[RpcClientEvent]:
        expected = cursor.sequence + 1
        while True:
            frame = await self._transport.request(
                "events.page",
                {
                    "stream_id": cursor.stream_id,
                    "after_sequence": cursor.sequence,
                },
            )
            if frame.type != "events.page":
                raise RemoteProtocolError("Gateway returned an invalid event page")
            raw_events = frame.data.get("events")
            next_sequence = frame.data.get("next_sequence")
            complete = frame.data.get("complete")
            if (
                not isinstance(raw_events, list)
                or type(next_sequence) is not int
                or type(complete) is not bool
            ):
                raise RemoteProtocolError("Gateway returned an invalid event page")
            for raw in raw_events:
                if not isinstance(raw, dict):
                    raise RemoteProtocolError("Gateway returned an invalid Remote event")
                wire = decode_v2_event(
                    json.dumps(raw, ensure_ascii=False, separators=(",", ":"))
                )
                event = client_event_from_wire(wire)
                if (
                    event.cursor.stream_id != cursor.stream_id
                    or event.cursor.sequence != expected
                ):
                    raise RemoteProtocolError("Remote event page contains a cursor gap")
                expected += 1
                cursor = event.cursor
                yield event
            if next_sequence != cursor.sequence:
                raise RemoteProtocolError("Remote event page cursor is inconsistent")
            if complete:
                return
            if not raw_events:
                raise RemoteProtocolError("Remote event page made no progress")


async def _unknown_on_disconnect(operation: str, awaitable: Awaitable[_T]) -> _T:
    try:
        return await awaitable
    except (RpcConnectionClosedError, RemoteConnectionError) as exc:
        raise RemoteOutcomeUnknownError(f"outcome of {operation} is unknown") from exc


def _lease_from_frame(frame: RemoteFrame) -> RemoteLeaseInfo:
    if frame.type != "lease.granted":
        raise RemoteProtocolError("Gateway did not grant the control lease")
    connection_id = frame.data.get("connection_id")
    expires_at = frame.data.get("expires_at")
    revision = frame.data.get("revision")
    if (
        not isinstance(connection_id, str)
        or not isinstance(expires_at, str)
        or type(revision) is not int
    ):
        raise RemoteProtocolError("Gateway returned an invalid lease")
    try:
        parsed = datetime.fromisoformat(expires_at)
    except ValueError as exc:
        raise RemoteProtocolError("Gateway returned an invalid lease timestamp") from exc
    return RemoteLeaseInfo(
        connection_id=connection_id, expires_at=parsed, revision=revision
    )


__all__ = [
    "EvoPiRemoteClient",
    "RemoteClientConfig",
    "RemoteLeaseInfo",
    "RemotePairingSubmission",
    "RemoteRunHandle",
    "RemoteWebSocket",
    "submit_remote_pairing",
]
