"""Single-process authenticated WSS Gateway over the existing RPC v2 Host."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any, cast
from uuid import uuid4

from evopi.core.types import JsonObject
from evopi.rpc.codec_v2 import decode_v2_envelope, encode_v2_event, encode_v2_response
from evopi.rpc.errors import RpcCodecError
from evopi.rpc.protocol_v2 import RpcV2Event, RpcV2Request, RpcV2Response
from evopi.rpc.server_v2 import RpcV2Host, RpcV2Server

from .audit import RemoteAuditLog
from .authorization import RemoteAuthorizedRpcHost
from .connections import RemoteConnectionRegistry, RemoteSendQueue
from .controller import RemoteHostController
from .crypto import create_auth_challenge, verify_auth_challenge
from .errors import (
    RemoteAuditError,
    RemoteError,
    RemoteRateLimitError,
    RemoteSecurityError,
)
from .lease import ControlLeaseManager
from .limits import RemoteRateLimiter
from .models import AuthChallenge, DeviceRecord, DeviceScope
from .protocol import (
    MAX_INBOUND_FRAME_BYTES,
    MAX_OUTBOUND_FRAME_BYTES,
    RemoteFrame,
    RemoteFrameCodec,
    RemoteProtocolError,
    remote_frame,
)
from .security import RemoteGatewayConfig, resolve_remote_client_ip, validate_remote_request
from .store import RemoteHostConfig

REMOTE_SUBPROTOCOL = "evopi.remote.v1"


class RemoteGateway:
    """Own network connections while delegating all Agent work to one RPC Host."""

    def __init__(
        self,
        *,
        host_config: RemoteHostConfig,
        controller: RemoteHostController,
        rpc_host: RpcV2Host,
        gateway_config: RemoteGatewayConfig,
        audit: RemoteAuditLog,
        leases: ControlLeaseManager | None = None,
        rate_limiter: RemoteRateLimiter | None = None,
        connections: RemoteConnectionRegistry | None = None,
    ) -> None:
        self.host_config = host_config
        self.controller = controller
        self.rpc_host = rpc_host
        self.config = gateway_config
        self.audit = audit
        self.leases = leases or ControlLeaseManager()
        self.rate_limiter = rate_limiter or RemoteRateLimiter()
        self.connections = connections or RemoteConnectionRegistry()
        self.ready = True
        self._websockets: dict[str, Any] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._event_stream = getattr(rpc_host, "event_stream", None)
        self._project_event = getattr(rpc_host, "project_event", None)

    def create_app(self) -> Any:
        try:
            from starlette.applications import Starlette
            from starlette.routing import Route, WebSocketRoute
        except ImportError as exc:  # pragma: no cover - exercised by packaging smoke tests
            raise RemoteError("install EvoPi with the 'remote' optional feature") from exc

        return Starlette(
            routes=[
                Route("/health/live", self._health_live),
                Route("/health/ready", self._health_ready),
                WebSocketRoute("/v1/connect", self._connect),
            ]
        )

    async def _health_live(self, request: Any) -> Any:
        from starlette.responses import JSONResponse

        del request
        return JSONResponse({"status": "live"})

    async def _health_ready(self, request: Any) -> Any:
        from starlette.responses import JSONResponse

        peer = getattr(request.client, "host", "") if request.client is not None else ""
        if peer not in {"127.0.0.1", "::1", "testclient"}:
            return JSONResponse({"status": "not_found"}, status_code=404)
        status = "ready" if self.ready else "not_ready"
        return JSONResponse({"status": status}, status_code=200 if self.ready else 503)

    async def _connect(self, websocket: Any) -> None:
        from starlette.websockets import WebSocketDisconnect

        connection_id = uuid4().hex
        self._loop = asyncio.get_running_loop()
        peer_ip = getattr(websocket.client, "host", "127.0.0.1")
        try:
            if REMOTE_SUBPROTOCOL not in websocket.scope.get("subprotocols", []):
                raise RemoteSecurityError("required WebSocket subprotocol is missing")
            validate_remote_request(
                host=websocket.headers.get("host"),
                origin=websocket.headers.get("origin"),
                config=self.config,
            )
            client_ip = resolve_remote_client_ip(
                peer_ip=peer_ip,
                forwarded_for=websocket.headers.get("x-forwarded-for"),
                config=self.config,
            )
            if not self.ready:
                raise RemoteAuditError("Gateway audit is unavailable")
            self.connections.open(connection_id, client_ip)
        except RemoteError:
            await websocket.close(code=1008)
            return

        await websocket.accept(subprotocol=REMOTE_SUBPROTOCOL)
        self._websockets[connection_id] = websocket
        queue = RemoteSendQueue()
        sender = asyncio.create_task(self._send_loop(websocket, queue))
        device: DeviceRecord | None = None
        pending_device: DeviceRecord | None = None
        challenge: AuthChallenge | None = None
        server: RpcV2Server | None = None
        event_task: asyncio.Task[None] | None = None
        try:
            while True:
                payload = await websocket.receive_text()
                if len(payload.encode("utf-8")) > MAX_INBOUND_FRAME_BYTES:
                    raise RemoteProtocolError("Remote frame exceeds 128 KiB")
                if device is not None and server is not None:
                    rpc = self._try_rpc(payload)
                    if rpc is not None:
                        response = await self._handle_rpc(
                            rpc, server, device, client_ip, queue
                        )
                        if (
                            rpc.method == "initialize"
                            and response.ok
                            and event_task is None
                            and DeviceScope.OBSERVE in device.scopes
                            and self._event_stream is not None
                            and callable(self._project_event)
                        ):
                            assert response.result is not None
                            stream = cast(JsonObject, response.result["stream"])
                            event_task = asyncio.create_task(
                                self._forward_events(
                                    after_sequence=cast(int, stream["cursor"]),
                                    queue=queue,
                                )
                            )
                        continue
                frame = RemoteFrameCodec.decode(payload)
                if frame.type == "auth.begin" and device is None:
                    if not self.rate_limiter.allow(
                        f"handshake:{client_ip}", limit=10, window=_minute()
                    ):
                        raise RemoteRateLimitError("authentication rate limit exceeded")
                    device_id = _required_string(frame.data, "device_id")
                    candidate = self.controller.get_device(device_id)
                    challenge = create_auth_challenge(
                        host_id=self.host_config.host_id,
                        device_id=device_id,
                        connection_id=connection_id,
                    )
                    queue.put_nowait(
                        RemoteFrameCodec.encode(
                            remote_frame(
                                "auth.challenge",
                                frame.request_id,
                                {"challenge": _challenge_to_dict(challenge)},
                            )
                        )
                    )
                    pending_device = candidate
                    continue
                if (
                    frame.type == "auth.complete"
                    and device is None
                    and pending_device is not None
                    and challenge is not None
                ):
                    signature = _required_string(frame.data, "signature")
                    current = self.controller.get_device(pending_device.device_id)
                    challenge_to_verify = challenge
                    challenge = None
                    verify_auth_challenge(
                        current.public_jwk, challenge_to_verify, signature
                    )
                    self.connections.authenticate(connection_id, current.device_id)
                    device = current
                    pending_device = None
                    server = RpcV2Server(
                        RemoteAuthorizedRpcHost(
                            self.rpc_host,
                            device_id=device.device_id,
                            connection_id=connection_id,
                            scopes=device.scopes,
                            leases=self.leases,
                        )
                    )
                    self._audit(
                        action="auth.complete",
                        outcome="allowed",
                        device_id=device.device_id,
                        client_ip=client_ip,
                    )
                    queue.put_nowait(
                        RemoteFrameCodec.encode(
                            remote_frame(
                                "auth.ok",
                                frame.request_id,
                                {
                                    "device_id": device.device_id,
                                    "scopes": [scope.value for scope in device.scopes],
                                },
                            )
                        )
                    )
                    continue
                if frame.type == "pairing.submit" and device is None:
                    if not self.rate_limiter.allow(
                        f"pairing:{client_ip}", limit=5, window=_minute()
                    ):
                        raise RemoteRateLimitError("pairing rate limit exceeded")
                    request = self.controller.submit_pairing(
                        code=_required_string(frame.data, "code"),
                        device_name=_required_string(frame.data, "device_name"),
                        public_jwk=_required_object(frame.data, "public_jwk"),
                    )
                    self._audit(
                        action="pairing.submit", outcome="pending", client_ip=client_ip
                    )
                    queue.put_nowait(
                        RemoteFrameCodec.encode(
                            remote_frame(
                                "pairing.pending",
                                frame.request_id,
                                {"request_id": request.request_id},
                            )
                        )
                    )
                    continue
                if device is None or server is None:
                    raise RemoteProtocolError("device authentication is required")
                await self._handle_control(
                    frame, device, connection_id, client_ip, queue
                )
        except WebSocketDisconnect:
            pass
        except RemoteError as exc:
            try:
                queue.put_nowait(
                    RemoteFrameCodec.encode(
                        remote_frame(
                            "error",
                            uuid4().hex,
                            {"code": _error_code(exc), "message": str(exc)},
                        )
                    )
                )
                await asyncio.sleep(0)
            except RemoteError:
                pass
            await websocket.close(code=1008)
        finally:
            if server is not None:
                await server.close()
            if event_task is not None:
                event_task.cancel()
                await asyncio.gather(event_task, return_exceptions=True)
            self.leases.release_connection(connection_id)
            self.connections.close(connection_id)
            self._websockets.pop(connection_id, None)
            queue.close()
            await asyncio.gather(sender, return_exceptions=True)

    def disconnect_device(self, device_id: str) -> None:
        """Immediately revoke transport access and release any held lease."""

        self.leases.revoke_device(device_id)
        connection_ids = self.connections.device_connections(device_id)
        loop = self._loop
        if loop is None:
            return
        for connection_id in connection_ids:
            websocket = self._websockets.get(connection_id)
            if websocket is not None:
                loop.call_soon_threadsafe(self._schedule_close, websocket)

    @staticmethod
    def _schedule_close(websocket: Any) -> None:
        asyncio.create_task(websocket.close(code=4003))

    async def _handle_control(
        self,
        frame: RemoteFrame,
        device: DeviceRecord,
        connection_id: str,
        client_ip: str,
        queue: RemoteSendQueue,
    ) -> None:
        if frame.type == "events.page":
            if DeviceScope.OBSERVE not in device.scopes:
                raise RemoteSecurityError("remote observe scope is required")
            await self._send_event_page(frame, queue)
            return
        if frame.type not in {"lease.acquire", "lease.renew", "lease.release"}:
            raise RemoteProtocolError("unknown Remote control frame")
        if DeviceScope.CONTROL not in device.scopes:
            raise RemoteSecurityError("remote control scope is required")
        if frame.type == "lease.acquire":
            lease = self.leases.acquire(
                device_id=device.device_id, connection_id=connection_id
            )
        elif frame.type == "lease.renew":
            lease = self.leases.renew(
                device_id=device.device_id, connection_id=connection_id
            )
        else:
            released = self.leases.release_connection(connection_id)
            queue.put_nowait(
                RemoteFrameCodec.encode(
                    remote_frame("lease.released", frame.request_id, {"released": released})
                )
            )
            return
        self._audit(
            action=frame.type,
            outcome="allowed",
            device_id=device.device_id,
            client_ip=client_ip,
        )
        queue.put_nowait(
            RemoteFrameCodec.encode(
                remote_frame(
                    "lease.granted",
                    frame.request_id,
                    {
                        "connection_id": lease.connection_id,
                        "expires_at": lease.expires_at.isoformat(),
                        "revision": lease.revision,
                    },
                )
            )
        )

    async def _handle_rpc(
        self,
        request: RpcV2Request,
        server: RpcV2Server,
        device: DeviceRecord,
        client_ip: str,
        queue: RemoteSendQueue,
    ) -> RpcV2Response:
        if not self.rate_limiter.allow(
            f"request:{device.device_id}", limit=120, window=_minute()
        ):
            raise RemoteRateLimitError("authenticated request rate limit exceeded")
        if request.method == "run.start" and not self.rate_limiter.allow(
            f"run:{device.device_id}", limit=6, window=_minute()
        ):
            raise RemoteRateLimitError("Run start rate limit exceeded")
        if request.method in {"confirmation.respond", "confirmation.respond_batch"} and not self.rate_limiter.allow(
            f"confirmation:{device.device_id}", limit=30, window=_minute()
        ):
            raise RemoteRateLimitError("Confirmation write rate limit exceeded")
        response = await server.dispatch(request)
        encoded = encode_v2_response(response)
        if len(encoded.encode("utf-8")) > MAX_OUTBOUND_FRAME_BYTES:
            raise RemoteRateLimitError("outbound response exceeds 1 MiB")
        queue.put_nowait(encoded)
        self._audit(
            action=request.method,
            outcome="allowed" if response.ok else "denied",
            device_id=device.device_id,
            client_ip=client_ip,
            details={"run_id": _safe_run_id(request.params)},
        )
        return response

    async def _forward_events(
        self, *, after_sequence: int, queue: RemoteSendQueue
    ) -> None:
        event_stream = self._event_stream
        project_event = self._project_event
        if event_stream is None or not callable(project_event):
            raise RemoteProtocolError("event streaming is unavailable")
        subscription = await event_stream.subscribe(after_sequence=after_sequence)
        try:
            async for event in subscription:
                projected = project_event(event)
                queue.put_nowait(encode_v2_event(projected))
        finally:
            close = getattr(subscription, "aclose", None)
            if close is not None:
                await close()

    async def _send_event_page(
        self, frame: RemoteFrame, queue: RemoteSendQueue
    ) -> None:
        if self._event_stream is None or not callable(self._project_event):
            raise RemoteProtocolError("event paging is unavailable")
        stream_id = _required_string(frame.data, "stream_id")
        if stream_id != self._event_stream.stream_id:
            raise RemoteProtocolError("event cursor belongs to another Host")
        after_sequence = frame.data.get("after_sequence")
        if type(after_sequence) is not int or after_sequence < 0:
            raise RemoteProtocolError("after_sequence must be non-negative")
        window = self._event_stream.snapshot(after_sequence=after_sequence)
        events: list[JsonObject] = []
        last_sequence = after_sequence
        for event in window.events[:100]:
            projected: RpcV2Event = self._project_event(event)
            raw = cast(JsonObject, json.loads(encode_v2_event(projected)))
            candidate = RemoteFrameCodec.encode(
                remote_frame(
                    "events.page",
                    frame.request_id,
                    {
                        "stream_id": stream_id,
                        "after_sequence": after_sequence,
                        "snapshot_latest": window.latest_sequence,
                        "next_sequence": projected.sequence,
                        "complete": projected.sequence == window.latest_sequence,
                        "events": [*events, raw],
                    },
                )
            )
            if len(candidate.encode("utf-8")) > MAX_OUTBOUND_FRAME_BYTES:
                break
            events.append(raw)
            last_sequence = projected.sequence
        complete = last_sequence == window.latest_sequence
        queue.put_nowait(
            RemoteFrameCodec.encode(
                remote_frame(
                    "events.page",
                    frame.request_id,
                    {
                        "stream_id": stream_id,
                        "after_sequence": after_sequence,
                        "snapshot_latest": window.latest_sequence,
                        "next_sequence": last_sequence,
                        "complete": complete,
                        "events": events,
                    },
                )
            )
        )

    @staticmethod
    def _try_rpc(payload: str) -> RpcV2Request | None:
        try:
            envelope = decode_v2_envelope(payload)
        except RpcCodecError:
            return None
        if not isinstance(envelope, RpcV2Request):
            raise RemoteProtocolError("only RPC requests are accepted from clients")
        return envelope

    @staticmethod
    async def _send_loop(websocket: Any, queue: RemoteSendQueue) -> None:
        while True:
            payload = await queue.get()
            if payload is None:
                return
            await websocket.send_text(payload)

    def _audit(
        self,
        *,
        action: str,
        outcome: str,
        device_id: str | None = None,
        client_ip: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        try:
            self.audit.append(
                action=action,
                outcome=outcome,
                device_id=device_id,
                client_ip=client_ip,
                details=details,
            )
        except RemoteAuditError:
            self.ready = False
            raise


def _required_string(data: JsonObject, key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise RemoteProtocolError(f"{key} must be a non-empty string")
    return value


def _required_object(data: JsonObject, key: str) -> dict[str, str]:
    value = data.get(key)
    if not isinstance(value, dict) or any(
        not isinstance(item_key, str) or not isinstance(item, str)
        for item_key, item in value.items()
    ):
        raise RemoteProtocolError(f"{key} must be a string object")
    return cast(dict[str, str], value)


def _challenge_to_dict(challenge: AuthChallenge) -> JsonObject:
    return {
        "protocol": challenge.protocol,
        "host_id": challenge.host_id,
        "device_id": challenge.device_id,
        "connection_id": challenge.connection_id,
        "nonce": challenge.nonce,
        "issued_at": challenge.issued_at.isoformat(),
        "expires_at": challenge.expires_at.isoformat(),
    }


def challenge_from_dict(value: JsonObject) -> AuthChallenge:
    expected = {
        "protocol",
        "host_id",
        "device_id",
        "connection_id",
        "nonce",
        "issued_at",
        "expires_at",
    }
    if set(value) != expected or value.get("protocol") != REMOTE_SUBPROTOCOL:
        raise RemoteProtocolError("authentication challenge is malformed")
    try:
        return AuthChallenge(
            host_id=_required_string(value, "host_id"),
            device_id=_required_string(value, "device_id"),
            connection_id=_required_string(value, "connection_id"),
            nonce=_required_string(value, "nonce"),
            issued_at=datetime.fromisoformat(_required_string(value, "issued_at")),
            expires_at=datetime.fromisoformat(_required_string(value, "expires_at")),
        )
    except ValueError as exc:
        raise RemoteProtocolError("authentication challenge timestamp is malformed") from exc


def _safe_run_id(params: JsonObject) -> str | None:
    value = params.get("run_id")
    return value if isinstance(value, str) else None


def _error_code(exc: RemoteError) -> str:
    return type(exc).__name__.removeprefix("Remote").removesuffix("Error").lower()


def _minute() -> Any:
    from datetime import timedelta

    return timedelta(minutes=1)


__all__ = [
    "REMOTE_SUBPROTOCOL",
    "RemoteGateway",
    "challenge_from_dict",
]
