"""Structured errors for the RPC v1 package.

Every error carries a stable machine-readable ``code``. Codes are the only
error identifiers that cross the RPC boundary; messages are safe by design
and never contain tracebacks, serialized values, tool arguments, or secrets.
"""

from __future__ import annotations

from evopi.core.types import JsonObject


class RpcError(Exception):
    """Base class for all RPC package errors."""

    code: str = "rpc_error"


class RpcProtocolError(RpcError):
    """A wire envelope or conversion rule was violated."""

    code: str = "protocol_error"


class RpcCodecError(RpcProtocolError):
    """A wire line could not be strictly decoded or encoded."""

    code: str = "codec_error"


class RpcEventDataError(RpcProtocolError):
    """Event data contained a value outside the strict JSON-safe set."""

    code: str = "event_data_error"


class EventStreamError(RpcError):
    """Base class for bounded Event Stream failures."""

    code: str = "event_stream_error"


class EventCursorExpiredError(EventStreamError):
    """A cursor is older than the retained history; it never silently skips."""

    code: str = "cursor_expired"


class EventCursorInvalidError(EventStreamError):
    """A cursor is not a valid sequence position (negative or non-integer)."""

    code: str = "cursor_invalid"


class EventSubscriberDroppedError(EventStreamError):
    """A slow subscriber overflowed its bounded queue and was disconnected."""

    code: str = "subscriber_dropped"


class EventStreamClosedError(EventStreamError):
    """An operation was attempted on a closed Event Stream."""

    code: str = "stream_closed"


class EventPublishAfterCloseError(EventStreamClosedError):
    """Publishing to a closed Event Stream."""

    code: str = "publish_after_close"


class RpcHostError(RpcError):
    """Raised by an ``RpcHost`` to return a stable, safe error response.

    The instance ``code`` is returned verbatim on the wire; ``message`` must
    already be safe for the client (no tracebacks, prompts, tool arguments,
    provider state, or secrets). Any other exception raised by a Host is
    redacted by the server into ``internal_error``.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: JsonObject | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details: JsonObject = details if details is not None else {}

    def __repr__(self) -> str:
        return f"RpcHostError(code={self.code!r}, message={self.message!r})"


class RpcConnectionClosedError(RpcError):
    """The JSONL connection is closed; no further send is possible."""

    code: str = "connection_closed"


class RpcConnectionProtocolError(RpcError):
    """A malformed wire line forced a clean connection failure."""

    code: str = "connection_protocol_error"


__all__ = [
    "EventCursorExpiredError",
    "EventCursorInvalidError",
    "EventPublishAfterCloseError",
    "EventStreamClosedError",
    "EventStreamError",
    "EventSubscriberDroppedError",
    "RpcCodecError",
    "RpcConnectionClosedError",
    "RpcConnectionProtocolError",
    "RpcError",
    "RpcEventDataError",
    "RpcHostError",
    "RpcProtocolError",
]
