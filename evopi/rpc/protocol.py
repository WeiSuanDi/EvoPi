"""Frozen wire envelopes for the RPC v1 host protocol.

Exact key sets make each envelope unambiguous on the wire: a request always
has ``method``/``params``, a response always has ``ok``, and an event always
has ``sequence``. The codec discriminates envelopes by these key sets.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TypeAlias

from evopi.core.types import JsonObject


@dataclass(slots=True, frozen=True, kw_only=True)
class RpcRequest:
    request_id: str
    method: str
    params: JsonObject
    schema_version: int = 1


@dataclass(slots=True, frozen=True, kw_only=True)
class RpcErrorInfo:
    code: str
    message: str
    details: JsonObject


@dataclass(slots=True, frozen=True, kw_only=True)
class RpcResponse:
    request_id: str
    ok: bool
    result: JsonObject | None = None
    error: RpcErrorInfo | None = None
    schema_version: int = 1


@dataclass(slots=True, frozen=True, kw_only=True)
class RpcEvent:
    event_id: str
    sequence: int
    type: str
    data: JsonObject
    run_id: str | None
    created_at: datetime
    schema_version: int = 1


RpcEnvelope: TypeAlias = RpcRequest | RpcResponse | RpcEvent

__all__ = [
    "RpcEnvelope",
    "RpcErrorInfo",
    "RpcEvent",
    "RpcRequest",
    "RpcResponse",
]
