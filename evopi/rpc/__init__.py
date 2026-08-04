"""Host-neutral RPC v1: strict JSONL envelopes, bounded Event Stream, generic server.

This package is transport-neutral and process-local. It must not be connected
to BaseHarness or the top-level CLI within lane scope; that wiring is reserved
for Integration (``evopi/rpc/harness_host.py`` and the CLI route).
"""

from __future__ import annotations

from .codec import (
    SCHEMA_VERSION,
    decode_envelope,
    decode_event,
    decode_request,
    decode_response,
    encode_event,
    encode_request,
    encode_response,
    extract_request_id,
    parse_utc_timestamp,
    to_event_data,
)
from .connection import (
    DEFAULT_INBOUND_EVENT_CAPACITY,
    AsyncTextReader,
    AsyncTextWriter,
    JsonlRpcConnection,
)
from .event_stream import DEFAULT_CAPACITY, DEFAULT_SUBSCRIBER_QUEUE_CAPACITY, EventStream
from .harness_host import HarnessRpcHost
from .server import CONFIRMATION_DECISIONS, RpcHost, RpcServer, error_response
from .errors import (
    EventCursorExpiredError,
    EventCursorInvalidError,
    EventPublishAfterCloseError,
    EventStreamClosedError,
    EventStreamError,
    EventSubscriberDroppedError,
    RpcCodecError,
    RpcConnectionClosedError,
    RpcConnectionProtocolError,
    RpcError,
    RpcEventDataError,
    RpcHostError,
    RpcProtocolError,
)
from .protocol import RpcEnvelope, RpcErrorInfo, RpcEvent, RpcRequest, RpcResponse

__all__ = [
    "CONFIRMATION_DECISIONS",
    "DEFAULT_CAPACITY",
    "DEFAULT_INBOUND_EVENT_CAPACITY",
    "DEFAULT_SUBSCRIBER_QUEUE_CAPACITY",
    "EventStream",
    "HarnessRpcHost",
    "RpcHost",
    "RpcServer",
    "SCHEMA_VERSION",
    "AsyncTextReader",
    "AsyncTextWriter",
    "JsonlRpcConnection",
    "error_response",
    "extract_request_id",
    "EventCursorExpiredError",
    "EventCursorInvalidError",
    "EventPublishAfterCloseError",
    "EventStreamClosedError",
    "EventStreamError",
    "EventSubscriberDroppedError",
    "RpcCodecError",
    "RpcConnectionClosedError",
    "RpcConnectionProtocolError",
    "RpcEnvelope",
    "RpcError",
    "RpcErrorInfo",
    "RpcEvent",
    "RpcEventDataError",
    "RpcHostError",
    "RpcProtocolError",
    "RpcRequest",
    "RpcResponse",
    "decode_envelope",
    "decode_event",
    "decode_request",
    "decode_response",
    "encode_event",
    "encode_request",
    "encode_response",
    "parse_utc_timestamp",
    "to_event_data",
]
