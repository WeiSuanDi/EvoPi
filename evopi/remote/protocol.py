"""Strict Remote control frames layered beside native RPC v2 envelopes."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping

from evopi.core.types import JsonObject

from .errors import RemoteContractError

MAX_INBOUND_FRAME_BYTES = 128 * 1024
MAX_OUTBOUND_FRAME_BYTES = 1024 * 1024


class RemoteProtocolError(RemoteContractError):
    """Raised when a Remote control frame violates the v1 wire contract."""


@dataclass(slots=True, frozen=True, kw_only=True)
class RemoteFrame:
    type: str
    request_id: str
    data: JsonObject
    schema_version: int = 1


class RemoteFrameCodec:
    _FIELDS = frozenset({"schema_version", "type", "request_id", "data"})

    @classmethod
    def encode(cls, frame: RemoteFrame) -> str:
        if frame.schema_version != 1:
            raise RemoteProtocolError("unsupported Remote frame version")
        if not frame.type or not frame.request_id or not isinstance(frame.data, dict):
            raise RemoteProtocolError("Remote frame fields are invalid")
        try:
            payload = json.dumps(
                {
                    "schema_version": 1,
                    "type": frame.type,
                    "request_id": frame.request_id,
                    "data": frame.data,
                },
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            )
        except (TypeError, ValueError) as exc:
            raise RemoteProtocolError("Remote frame is not JSON-safe") from exc
        if len(payload.encode("utf-8")) > MAX_OUTBOUND_FRAME_BYTES:
            raise RemoteProtocolError("Remote frame exceeds 1 MiB")
        return payload

    @classmethod
    def decode(cls, payload: str) -> RemoteFrame:
        if not isinstance(payload, str):
            raise RemoteProtocolError("Remote frame must be text")
        if len(payload.encode("utf-8")) > MAX_INBOUND_FRAME_BYTES:
            raise RemoteProtocolError("Remote frame exceeds 128 KiB")
        try:
            raw = json.loads(
                payload,
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )
        except RemoteProtocolError:
            raise
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise RemoteProtocolError("Remote frame is not valid JSON") from exc
        if not isinstance(raw, dict) or frozenset(raw) != cls._FIELDS:
            raise RemoteProtocolError("Remote frame has invalid fields")
        if raw["schema_version"] != 1:
            raise RemoteProtocolError("unsupported Remote frame version")
        if not isinstance(raw["type"], str) or not raw["type"]:
            raise RemoteProtocolError("Remote frame type is invalid")
        if not isinstance(raw["request_id"], str) or not raw["request_id"]:
            raise RemoteProtocolError("Remote frame request_id is invalid")
        if not isinstance(raw["data"], dict):
            raise RemoteProtocolError("Remote frame data must be an object")
        return RemoteFrame(
            type=raw["type"], request_id=raw["request_id"], data=raw["data"]
        )


def remote_frame(
    frame_type: str, request_id: str, data: Mapping[str, Any]
) -> RemoteFrame:
    return RemoteFrame(type=frame_type, request_id=request_id, data=dict(data))


def _unique_object(items: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in items:
        if key in value:
            raise RemoteProtocolError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _reject_constant(value: str) -> Any:
    raise RemoteProtocolError(f"invalid JSON constant: {value}")


__all__ = [
    "MAX_INBOUND_FRAME_BYTES",
    "MAX_OUTBOUND_FRAME_BYTES",
    "RemoteFrame",
    "RemoteFrameCodec",
    "RemoteProtocolError",
    "remote_frame",
]
