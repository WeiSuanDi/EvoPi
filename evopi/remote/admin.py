"""Local-only authenticated management protocol and IPC helpers."""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from multiprocessing.connection import Client, Listener
from pathlib import Path
from typing import Any, Callable, Mapping

from .errors import RemoteError

_MAX_MESSAGE_BYTES = 65_536


class RemoteAdminProtocolError(RemoteError):
    """Raised when a local management frame violates its strict contract."""


def _no_duplicates(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        if key in result:
            raise RemoteAdminProtocolError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


@dataclass(slots=True, frozen=True, kw_only=True)
class RemoteAdminEndpoint:
    family: str
    address: str


@dataclass(slots=True, frozen=True, kw_only=True)
class RemoteAdminRequest:
    request_id: str
    method: str
    params: Mapping[str, Any]
    schema_version: int = 1


@dataclass(slots=True, frozen=True, kw_only=True)
class RemoteAdminResponse:
    request_id: str
    ok: bool
    result: Mapping[str, Any] | None = None
    error: str | None = None
    schema_version: int = 1


class RemoteAdminCodec:
    @staticmethod
    def encode_request(
        *, request_id: str, method: str, params: Mapping[str, Any]
    ) -> bytes:
        return _encode(
            {
                "schema_version": 1,
                "request_id": request_id,
                "method": method,
                "params": dict(params),
            }
        )

    @staticmethod
    def decode_request(payload: bytes) -> RemoteAdminRequest:
        raw = _decode(payload)
        if set(raw) != {"schema_version", "request_id", "method", "params"}:
            raise RemoteAdminProtocolError("admin request has invalid fields")
        request_id = raw["request_id"]
        method = raw["method"]
        params = raw["params"]
        if (
            raw["schema_version"] != 1
            or not isinstance(request_id, str)
            or not request_id
            or not isinstance(method, str)
            or not method
            or not isinstance(params, dict)
        ):
            raise RemoteAdminProtocolError("admin request has invalid field types")
        return RemoteAdminRequest(request_id=request_id, method=method, params=params)

    @staticmethod
    def encode_response(response: RemoteAdminResponse) -> bytes:
        return _encode(
            {
                "schema_version": 1,
                "request_id": response.request_id,
                "ok": response.ok,
                "result": dict(response.result) if response.result is not None else None,
                "error": response.error,
            }
        )

    @staticmethod
    def decode_response(payload: bytes) -> RemoteAdminResponse:
        raw = _decode(payload)
        if set(raw) != {"schema_version", "request_id", "ok", "result", "error"}:
            raise RemoteAdminProtocolError("admin response has invalid fields")
        if raw["schema_version"] != 1 or not isinstance(raw["request_id"], str):
            raise RemoteAdminProtocolError("admin response has invalid identity")
        if not isinstance(raw["ok"], bool):
            raise RemoteAdminProtocolError("admin response ok must be boolean")
        result = raw["result"]
        error = raw["error"]
        if result is not None and not isinstance(result, dict):
            raise RemoteAdminProtocolError("admin response result must be an object or null")
        if error is not None and not isinstance(error, str):
            raise RemoteAdminProtocolError("admin response error must be a string or null")
        if raw["ok"] and (result is None or error is not None):
            raise RemoteAdminProtocolError("successful admin response is inconsistent")
        if not raw["ok"] and (error is None or result is not None):
            raise RemoteAdminProtocolError("failed admin response is inconsistent")
        return RemoteAdminResponse(
            request_id=raw["request_id"], ok=raw["ok"], result=result, error=error
        )


def _encode(value: Mapping[str, Any]) -> bytes:
    try:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise RemoteAdminProtocolError("admin message is not JSON-safe") from exc
    if len(payload) > _MAX_MESSAGE_BYTES:
        raise RemoteAdminProtocolError("admin message exceeds 64 KiB")
    return payload


def _decode(payload: bytes) -> dict[str, Any]:
    if len(payload) > _MAX_MESSAGE_BYTES:
        raise RemoteAdminProtocolError("admin message exceeds 64 KiB")
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_no_duplicates,
            parse_constant=lambda value: (_ for _ in ()).throw(
                RemoteAdminProtocolError(f"invalid JSON constant: {value}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RemoteAdminProtocolError("admin message is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise RemoteAdminProtocolError("admin message root must be an object")
    return value


def resolve_admin_endpoint(host_id: str, host_directory: Path) -> RemoteAdminEndpoint:
    suffix = host_id[:12]
    if os.name == "nt":
        return RemoteAdminEndpoint(
            family="AF_PIPE", address=rf"\\.\pipe\evopi-remote-{suffix}"
        )
    return RemoteAdminEndpoint(
        family="AF_UNIX", address=str(host_directory / f"admin-{suffix}.sock")
    )


class RemoteAdminClient:
    def __init__(self, endpoint: RemoteAdminEndpoint, secret: bytes) -> None:
        self.endpoint = endpoint
        self.secret = secret

    def call(self, request: RemoteAdminRequest) -> RemoteAdminResponse:
        connection = Client(
            self.endpoint.address, family=self.endpoint.family, authkey=self.secret
        )
        try:
            connection.send_bytes(
                RemoteAdminCodec.encode_request(
                    request_id=request.request_id,
                    method=request.method,
                    params=request.params,
                )
            )
            return RemoteAdminCodec.decode_response(
                connection.recv_bytes(maxlength=_MAX_MESSAGE_BYTES)
            )
        finally:
            connection.close()


AdminHandler = Callable[[RemoteAdminRequest], RemoteAdminResponse]


class RemoteAdminServer:
    """Blocking local IPC server intended to run in one dedicated thread."""

    def __init__(
        self,
        endpoint: RemoteAdminEndpoint,
        secret: bytes,
        handler: AdminHandler,
    ) -> None:
        self.endpoint = endpoint
        self.secret = secret
        self.handler = handler
        self._listener: Listener | None = None
        self._closing = threading.Event()

    def serve_once(self) -> None:
        listener = Listener(
            self.endpoint.address,
            family=self.endpoint.family,
            authkey=self.secret,
        )
        self._listener = listener
        if self.endpoint.family == "AF_UNIX":
            os.chmod(self.endpoint.address, 0o600)
        try:
            connection = listener.accept()
            self._handle(connection)
        finally:
            listener.close()
            self._listener = None

    def close(self) -> None:
        self._closing.set()
        if self._listener is not None:
            self._listener.close()

    def serve_forever(self) -> None:
        listener = Listener(
            self.endpoint.address,
            family=self.endpoint.family,
            authkey=self.secret,
        )
        self._listener = listener
        if self.endpoint.family == "AF_UNIX":
            os.chmod(self.endpoint.address, 0o600)
        try:
            while not self._closing.is_set():
                try:
                    connection = listener.accept()
                except (OSError, EOFError):
                    if self._closing.is_set():
                        return
                    raise
                self._handle(connection)
        finally:
            listener.close()
            self._listener = None

    def _handle(self, connection: Any) -> None:
        try:
            request = RemoteAdminCodec.decode_request(
                connection.recv_bytes(maxlength=_MAX_MESSAGE_BYTES)
            )
            response = self.handler(request)
        except Exception as exc:
            response = RemoteAdminResponse(
                request_id="unknown",
                ok=False,
                error=f"admin request rejected: {type(exc).__name__}",
            )
        try:
            connection.send_bytes(RemoteAdminCodec.encode_response(response))
        finally:
            connection.close()


__all__ = [
    "RemoteAdminClient",
    "RemoteAdminCodec",
    "RemoteAdminEndpoint",
    "RemoteAdminProtocolError",
    "RemoteAdminRequest",
    "RemoteAdminResponse",
    "RemoteAdminServer",
    "resolve_admin_endpoint",
]
