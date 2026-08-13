"""Stateful asynchronous server for the frozen RPC v2 method set."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable, Protocol

from evopi.core.types import JsonObject

from .codec_v2 import encode_v2_request
from .errors import RpcCodecError, RpcConnectionClosedError, RpcHostError
from .methods_v2 import METHOD_HANDLERS_V2, validate_v2_params, validate_v2_result
from .protocol_v2 import RpcV2ErrorInfo, RpcV2Request, RpcV2Response


class RpcV2Host(Protocol):
    async def initialize(self, params: JsonObject) -> JsonObject: ...
    async def runtime_status(self, params: JsonObject) -> JsonObject: ...
    async def run_start(self, params: JsonObject) -> JsonObject: ...
    async def run_steer(self, params: JsonObject) -> JsonObject: ...
    async def run_follow_up(self, params: JsonObject) -> JsonObject: ...
    async def run_abort(self, params: JsonObject) -> JsonObject: ...
    async def confirmation_list(self, params: JsonObject) -> JsonObject: ...
    async def confirmation_respond(self, params: JsonObject) -> JsonObject: ...
    async def confirmation_respond_batch(self, params: JsonObject) -> JsonObject: ...
    async def events_replay(self, params: JsonObject) -> JsonObject: ...
    async def shutdown(self, params: JsonObject) -> JsonObject: ...


def v2_error_response(
    request_id: str,
    code: str,
    message: str,
    details: JsonObject,
) -> RpcV2Response:
    return RpcV2Response(
        request_id=request_id,
        ok=False,
        error=RpcV2ErrorInfo(code=code, message=message, details=details),
    )


def _safe_error(exc: RpcHostError) -> bool:
    if not exc.code or not exc.message or not isinstance(exc.details, dict):
        return False
    try:
        json.dumps(exc.details, allow_nan=False)
    except (TypeError, ValueError):
        return False
    return True


class RpcV2Server:
    """Validate v2 requests and enforce the initialization state machine."""

    def __init__(self, host: RpcV2Host) -> None:
        self._host = host
        self._initialized = False
        self._initializing = False
        self._seen_ids: set[str] = set()
        self._inflight: dict[str, asyncio.Task[RpcV2Response]] = {}
        self._closing = False

    @property
    def initialized(self) -> bool:
        return self._initialized

    async def dispatch(self, request: RpcV2Request) -> RpcV2Response:
        if self._closing:
            raise RpcConnectionClosedError("rpc v2 server is closed")
        try:
            encode_v2_request(request)
        except RpcCodecError:
            if isinstance(request.request_id, str) and request.request_id:
                return v2_error_response(
                    request.request_id,
                    "invalid_request",
                    "invalid request envelope",
                    {"request_id": request.request_id},
                )
            raise
        if request.request_id in self._seen_ids:
            return v2_error_response(
                request.request_id,
                "duplicate_request",
                "duplicate request id",
                {"request_id": request.request_id},
            )
        self._seen_ids.add(request.request_id)
        if not self._initialized and request.method != "initialize":
            return v2_error_response(
                request.request_id,
                "not_initialized",
                "initialize must be the first request",
                {},
            )
        if (self._initialized or self._initializing) and request.method == "initialize":
            return v2_error_response(
                request.request_id,
                "already_initialized",
                "connection is already initialized",
                {},
            )
        if request.method == "initialize":
            self._initializing = True
        attribute = METHOD_HANDLERS_V2.get(request.method)
        handler = getattr(self._host, attribute, None) if attribute is not None else None
        if not callable(handler):
            return v2_error_response(
                request.request_id,
                "method_not_found",
                "unknown method",
                {"method": request.method},
            )
        try:
            validate_v2_params(request.method, request.params)
        except RpcCodecError:
            return v2_error_response(
                request.request_id,
                "invalid_params",
                "invalid params",
                {"method": request.method},
            )
        task = asyncio.create_task(self._invoke(handler, request))
        self._inflight[request.request_id] = task
        try:
            response = await task
        finally:
            self._inflight.pop(request.request_id, None)
            if request.method == "initialize":
                self._initializing = False
        if request.method == "initialize":
            if response.ok:
                self._initialized = True
        return response

    async def _invoke(
        self,
        handler: Callable[[JsonObject], Any],
        request: RpcV2Request,
    ) -> RpcV2Response:
        try:
            result = await handler(request.params)
            if not isinstance(result, dict):
                raise RpcCodecError("host result must be an object")
            validate_v2_result(request.method, result)
        except RpcHostError as exc:
            if _safe_error(exc):
                return v2_error_response(request.request_id, exc.code, exc.message, exc.details)
            return self._internal_error(request)
        except asyncio.CancelledError:
            raise
        except Exception:
            return self._internal_error(request)
        return RpcV2Response(request_id=request.request_id, ok=True, result=result)

    def _internal_error(self, request: RpcV2Request) -> RpcV2Response:
        return v2_error_response(
            request.request_id,
            "internal_error",
            "internal error",
            {"method": request.method},
        )

    async def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        tasks = tuple(self._inflight.values())
        self._inflight.clear()
        self._seen_ids.clear()
        self._initializing = False
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


__all__ = ["RpcV2Host", "RpcV2Server", "v2_error_response"]
