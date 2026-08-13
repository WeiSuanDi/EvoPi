"""Frozen wire envelopes for the local RPC v2 protocol."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TypeAlias

from evopi.core.types import JsonObject


@dataclass(slots=True, frozen=True, kw_only=True)
class RpcV2Request:
    request_id: str
    method: str
    params: JsonObject
    schema_version: int = 2


@dataclass(slots=True, frozen=True, kw_only=True)
class RpcV2ErrorInfo:
    code: str
    message: str
    details: JsonObject


@dataclass(slots=True, frozen=True, kw_only=True)
class RpcV2Response:
    request_id: str
    ok: bool
    result: JsonObject | None = None
    error: RpcV2ErrorInfo | None = None
    schema_version: int = 2


@dataclass(slots=True, frozen=True, kw_only=True)
class RpcV2Event:
    event_id: str
    stream_id: str
    sequence: int
    type: str
    data: JsonObject
    run_id: str | None
    created_at: datetime
    schema_version: int = 2


RpcV2Envelope: TypeAlias = RpcV2Request | RpcV2Response | RpcV2Event

__all__ = [
    "RpcV2Envelope",
    "RpcV2ErrorInfo",
    "RpcV2Event",
    "RpcV2Request",
    "RpcV2Response",
]
