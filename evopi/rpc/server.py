"""Generic asynchronous RPC server for the frozen v1 method set.

The server recognizes only the v1 methods, validates each method's exact
params, tracks in-flight request IDs so a duplicate never reaches the Host
twice, and maps failures to stable codes with redacted messages. The Host is
a transport-neutral Protocol; the server never touches BaseHarness internals.
``run.start`` is dispatched as a task so the connection stays responsive to
Abort and Confirmation methods while a Run is active. One active Run is
enforced by the Host contract (``run_already_active``).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable, Protocol

from evopi.core.types import JsonObject

from .errors import RpcConnectionClosedError, RpcHostError
from .protocol import RpcErrorInfo, RpcRequest, RpcResponse

CONFIRMATION_STATUSES = frozenset({"approved", "denied", "cancelled"})

_METHOD_HANDLERS: dict[str, str] = {
    "initialize": "initialize",
    "runtime.status": "runtime_status",
    "run.start": "run_start",
    "run.abort": "run_abort",
    "confirmation.list": "confirmation_list",
    "confirmation.respond": "confirmation_respond",
    "confirmation.respond_batch": "confirmation_respond_batch",
    "events.replay": "events_replay",
    "shutdown": "shutdown",
}

# Server-level exact param validation per method: key -> (kind, required).
# The Host additionally performs its own typed semantic validation.
_PARAM_SCHEMAS: dict[str, dict[str, tuple[str, bool]]] = {
    "initialize": {},
    "runtime.status": {},
    "run.start": {},
    "run.abort": {},
    "confirmation.list": {},
    "confirmation.respond": {
        "request_id": ("str", True),
        "status": ("status", True),
        "metadata": ("dict", False),
    },
    "confirmation.respond_batch": {
        "responses": ("responses", True),
    },
    "events.replay": {
        "after_sequence": ("int", True),
    },
    "shutdown": {},
}


class RpcHost(Protocol):
    """Typed Host contract for the v1 method set (see CONTEXT.md section 5.3).

    Implementations raise ``RpcHostError`` to return stable, safe error
    responses; the server redacts every other exception as ``internal_error``.
    """

    async def initialize(self, params: JsonObject) -> JsonObject: ...
    async def runtime_status(self, params: JsonObject) -> JsonObject: ...
    async def run_start(self, params: JsonObject) -> JsonObject: ...
    async def run_abort(self, params: JsonObject) -> JsonObject: ...
    async def confirmation_list(self, params: JsonObject) -> JsonObject: ...
    async def confirmation_respond(self, params: JsonObject) -> JsonObject: ...
    async def confirmation_respond_batch(self, params: JsonObject) -> JsonObject: ...
    async def events_replay(self, params: JsonObject) -> JsonObject: ...
    async def shutdown(self, params: JsonObject) -> JsonObject: ...


def error_response(
    request_id: str,
    code: str,
    message: str,
    details: JsonObject,
) -> RpcResponse:
    """Build a safe error response with a stable code and JSON-safe details."""
    return RpcResponse(
        request_id=request_id,
        ok=False,
        error=RpcErrorInfo(code=code, message=message, details=details),
    )


def _field_error(kind: str, value: Any) -> str | None:
    if kind == "str":
        return None if isinstance(value, str) else "expected_string"
    if kind == "int":
        return None if type(value) is int else "expected_integer"
    if kind == "dict":
        return None if isinstance(value, dict) else "expected_object"
    if kind == "status":
        return None if isinstance(value, str) and value in CONFIRMATION_STATUSES else "expected_status"
    if kind == "responses":
        return None if _valid_batch(value) else "expected_batch"
    return "unknown_field_kind"


def _valid_batch(value: Any) -> bool:
    if not isinstance(value, list):
        return False
    for item in value:
        if not isinstance(item, dict):
            return False
        keys = frozenset(item)
        if not keys <= {"request_id", "status", "metadata"}:
            return False
        if "request_id" not in keys or "status" not in keys:
            return False
        if not isinstance(item["request_id"], str):
            return False
        if not isinstance(item["status"], str) or item["status"] not in CONFIRMATION_STATUSES:
            return False
        if "metadata" in keys and not isinstance(item["metadata"], dict):
            return False
    return True


def _validate_params(method: str, params: JsonObject) -> JsonObject | None:
    """Return details for the first violation, or None when params are exact."""
    schema = _PARAM_SCHEMAS[method]
    for key in params:
        if key not in schema:
            return {"issue": "unknown_key", "keys": [key]}
    for key, (kind, required) in schema.items():
        if key not in params:
            if required:
                return {"issue": "missing_key", "keys": [key]}
            continue
        reason = _field_error(kind, params[key])
        if reason is not None:
            return {"issue": reason, "keys": [key]}
    return None


def _is_json_safe(value: Any) -> bool:
    try:
        json.dumps(value, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return False
    return True


class RpcServer:
    """Validates requests, tracks in-flight IDs, and maps Host failures safely."""

    def __init__(self, host: RpcHost) -> None:
        self._host = host
        self._inflight: dict[str, asyncio.Task[RpcResponse]] = {}
        self._closing = False

    async def dispatch(self, request: RpcRequest) -> RpcResponse:
        """Handle one request and return its response (Host failures redacted)."""
        if self._closing:
            raise RpcConnectionClosedError("rpc server is closed")
        handler = self._resolve_handler(request.method)
        if handler is None:
            return error_response(
                request.request_id,
                "method_not_found",
                "unknown method",
                {"method": request.method},
            )
        issue = _validate_params(request.method, request.params)
        if issue is not None:
            return error_response(
                request.request_id,
                "invalid_params",
                "invalid params",
                {"method": request.method, **issue},
            )
        if request.request_id in self._inflight:
            return error_response(
                request.request_id,
                "duplicate_request",
                "duplicate request id",
                {"request_id": request.request_id},
            )
        task = asyncio.create_task(self._invoke(handler, request))
        self._inflight[request.request_id] = task
        try:
            return await task
        finally:
            self._inflight.pop(request.request_id, None)

    async def close(self) -> None:
        """Cancel in-flight Host calls exactly once; idempotent."""
        if self._closing:
            return
        self._closing = True
        tasks = list(self._inflight.values())
        self._inflight.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _resolve_handler(self, method: str) -> Callable[[JsonObject], Any] | None:
        attribute = _METHOD_HANDLERS.get(method)
        if attribute is None:
            return None
        handler = getattr(self._host, attribute, None)
        if not callable(handler):
            return None
        return handler

    async def _invoke(
        self,
        handler: Callable[[JsonObject], Any],
        request: RpcRequest,
    ) -> RpcResponse:
        try:
            result = await handler(request.params)
        except RpcHostError as exc:
            return error_response(request.request_id, exc.code, exc.message, exc.details)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Never echo tracebacks, prompts, tool arguments, or provider state.
            return error_response(
                request.request_id,
                "internal_error",
                "internal error",
                {"method": request.method},
            )
        if not _is_json_safe(result):
            return error_response(
                request.request_id,
                "internal_error",
                "internal error",
                {"method": request.method},
            )
        return RpcResponse(request_id=request.request_id, ok=True, result=result)


__all__ = ["CONFIRMATION_STATUSES", "RpcHost", "RpcServer", "error_response"]
