"""Asynchronous, typed Python client for EvoPi's local RPC v2 protocol."""

from __future__ import annotations

import asyncio
import inspect
import os
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress
from typing import cast

from evopi.core.types import JsonObject

from .client_codec import (
    client_event_from_wire,
    confirmation_ack_from_data,
    confirmation_record_from_data,
    interaction_receipt_from_result,
    run_result_from_event,
    runtime_status_from_result,
    server_info_from_result,
)
from .client_types import (
    RpcClientEvent,
    RpcConfirmationAck,
    RpcConfirmationAnswer,
    RpcConfirmationRecord,
    RpcEventCursor,
    RpcInteractionReceipt,
    RpcRunEvent,
    RpcRunResult,
    RpcRuntimeStatus,
    RpcServerInfo,
    RpcSubprocessConfig,
)
from .codec_v2 import decode_v2_event
from .connection import AsyncTextReader, AsyncTextWriter
from .connection_v2 import JsonlRpcV2Connection
from .errors import (
    RpcClientError,
    RpcCodecError,
    RpcConnectionClosedError,
    RpcCursorError,
    RpcEventGapError,
    RpcHandshakeError,
    RpcRemoteError,
    RpcSubprocessError,
)
from .methods_v2 import validate_v2_params, validate_v2_result

ConfirmationHandler = Callable[
    [RpcConfirmationRecord],
    Awaitable[RpcConfirmationAnswer | None],
]


class _SubprocessReader:
    def __init__(self, stream: asyncio.StreamReader) -> None:
        self._stream = stream

    async def readline(self) -> str:
        raw = await self._stream.readline()
        try:
            return raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise RpcCodecError("rpc subprocess emitted invalid UTF-8") from exc


class _SubprocessWriter:
    def __init__(self, stream: asyncio.StreamWriter) -> None:
        self._stream = stream

    async def write(self, text: str) -> None:
        self._stream.write(text.encode("utf-8"))

    async def flush(self) -> None:
        await self._stream.drain()

    async def aclose(self) -> None:
        self._stream.close()
        with suppress(Exception):
            await self._stream.wait_closed()


class RpcRunHandle:
    """Capability-bound control and completion view for exactly one Run."""

    def __init__(
        self,
        client: EvoPiRpcClient,
        run_id: str,
        start_sequence: int,
    ) -> None:
        self._client = client
        self.run_id = run_id
        self._start_cursor = RpcEventCursor(
            stream_id=client.server_info.cursor.stream_id,
            sequence=start_sequence - 1,
        )
        self._wait_task = asyncio.create_task(self._wait_for_end())

    @property
    def done(self) -> bool:
        return self._wait_task.done()

    @property
    def start_cursor(self) -> RpcEventCursor:
        return self._start_cursor

    async def events(self) -> AsyncIterator[RpcClientEvent]:
        async for event in self._client.events(after=self._start_cursor):
            if event.run_id == self.run_id:
                yield event
                if event.event_type == "agent_end":
                    return

    async def wait(self) -> RpcRunResult:
        return await asyncio.shield(self._wait_task)

    async def steer(self, content: str) -> RpcInteractionReceipt:
        return await self._client._interaction("run.steer", self.run_id, content)

    async def follow_up(self, content: str) -> RpcInteractionReceipt:
        return await self._client._interaction("run.follow_up", self.run_id, content)

    async def abort(self) -> bool:
        result = await self._client._request("run.abort", {"run_id": self.run_id})
        return cast(bool, result["aborted"])

    async def _wait_for_end(self) -> RpcRunResult:
        async for event in self.events():
            if isinstance(event, RpcRunEvent) and event.event_type == "agent_end":
                return run_result_from_event(event)
        raise RpcConnectionClosedError("connection closed before agent_end")


class EvoPiRpcClient:
    """One initialized RPC v2 connection with replay/live event continuity."""

    def __init__(
        self,
        connection: JsonlRpcV2Connection,
        *,
        owns_transport: bool,
        reader: AsyncTextReader,
        writer: AsyncTextWriter,
        confirmation_handler: ConfirmationHandler | None,
        local_event_capacity: int,
    ) -> None:
        self._connection = connection
        self._owns_transport = owns_transport
        self._reader = reader
        self._writer = writer
        self._confirmation_handler = confirmation_handler
        self._history: deque[RpcClientEvent] = deque(maxlen=local_event_capacity)
        self._condition = asyncio.Condition()
        self._background_errors: asyncio.Queue[RpcClientError | None] = asyncio.Queue()
        self._confirmation_queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._confirmation_seen: set[str] = set()
        self._connection_task: asyncio.Task[None] | None = None
        self._event_task: asyncio.Task[None] | None = None
        self._handler_task: asyncio.Task[None] | None = None
        self._terminal_error: Exception | None = None
        self._closing = False
        self._closed = False
        self._process: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._stderr = bytearray()
        self._shutdown_timeout = 5.0
        self.server_info: RpcServerInfo

    @classmethod
    async def connect(
        cls,
        reader: AsyncTextReader,
        writer: AsyncTextWriter,
        *,
        client_name: str = "evopi-python",
        client_version: str = "1",
        owns_transport: bool = False,
        confirmation_handler: ConfirmationHandler | None = None,
        handshake_timeout: float = 30.0,
        inbound_event_capacity: int = 1000,
    ) -> EvoPiRpcClient:
        if handshake_timeout <= 0:
            raise ValueError("handshake_timeout must be positive")
        connection = JsonlRpcV2Connection(
            reader,
            writer,
            inbound_event_capacity=inbound_event_capacity,
        )
        client = cls(
            connection,
            owns_transport=owns_transport,
            reader=reader,
            writer=writer,
            confirmation_handler=confirmation_handler,
            local_event_capacity=inbound_event_capacity,
        )
        client._connection_task = asyncio.create_task(connection.run())
        try:
            result = await asyncio.wait_for(
                client._request(
                    "initialize",
                    {"client_name": client_name, "client_version": client_version},
                ),
                timeout=handshake_timeout,
            )
            client.server_info = server_info_from_result(result)
        except BaseException as exc:
            await client._close_transport()
            if isinstance(exc, asyncio.CancelledError):
                raise
            if isinstance(exc, RpcClientError):
                raise RpcHandshakeError(str(exc)) from exc
            raise RpcHandshakeError("RPC v2 initialization failed") from exc
        client._event_task = asyncio.create_task(client._event_loop())
        if confirmation_handler is not None:
            client._handler_task = asyncio.create_task(client._confirmation_loop())
        return client

    @classmethod
    async def spawn(
        cls,
        config: RpcSubprocessConfig | None = None,
        *,
        confirmation_handler: ConfirmationHandler | None = None,
    ) -> EvoPiRpcClient:
        settings = config or RpcSubprocessConfig()
        if not settings.command:
            raise ValueError("subprocess command must not be empty")
        environment = None
        if settings.env is not None:
            environment = dict(os.environ)
            environment.update(settings.env)
        try:
            process = await asyncio.create_subprocess_exec(
                *settings.command,
                cwd=str(settings.cwd) if settings.cwd is not None else None,
                env=environment,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (OSError, ValueError) as exc:
            raise RpcSubprocessError("unable to start EvoPi RPC subprocess") from exc
        assert process.stdout is not None
        assert process.stdin is not None
        reader = _SubprocessReader(process.stdout)
        writer = _SubprocessWriter(process.stdin)
        stderr_buffer = bytearray()
        stderr_task = (
            asyncio.create_task(
                _capture_bounded_stderr(
                    process.stderr,
                    stderr_buffer,
                    settings.stderr_limit,
                )
            )
            if process.stderr is not None
            else None
        )
        try:
            client = await cls.connect(
                reader,
                writer,
                client_name=settings.client_name,
                client_version=settings.client_version,
                owns_transport=True,
                confirmation_handler=confirmation_handler,
                handshake_timeout=settings.handshake_timeout,
                inbound_event_capacity=settings.inbound_event_capacity,
            )
        except BaseException:
            process.terminate()
            with suppress(Exception):
                await process.wait()
            if stderr_task is not None:
                with suppress(Exception):
                    await stderr_task
            raise
        client._process = process
        client._shutdown_timeout = settings.shutdown_timeout
        client._stderr = stderr_buffer
        client._stderr_task = stderr_task
        return client

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def stderr_text(self) -> str:
        return bytes(self._stderr).decode("utf-8", errors="replace")

    async def runtime_status(self) -> RpcRuntimeStatus:
        return runtime_status_from_result(await self._request("runtime.status", {}))

    async def start_run(self, prompt: str) -> RpcRunHandle:
        result = await self._request("run.start", {"prompt": prompt})
        return RpcRunHandle(
            self,
            cast(str, result["run_id"]),
            cast(int, result["start_sequence"]),
        )

    async def replay_events(
        self,
        after: RpcEventCursor | None = None,
    ) -> tuple[RpcClientEvent, ...]:
        cursor = after or self.server_info.cursor
        self._require_cursor(cursor)
        try:
            result = await self._request(
                "events.replay",
                {"stream_id": cursor.stream_id, "after_sequence": cursor.sequence},
            )
        except RpcRemoteError as exc:
            if exc.code in {
                "event_stream_mismatch",
                "event_cursor_invalid",
                "event_cursor_expired",
            }:
                raise RpcCursorError(exc.message) from exc
            raise
        raw_events = cast(list[JsonObject], result["events"])
        events = tuple(
            client_event_from_wire(decode_v2_event(_json_line(item))) for item in raw_events
        )
        expected = cursor.sequence + 1
        for event in events:
            if event.cursor.sequence != expected:
                raise RpcEventGapError("replay contains an event sequence gap")
            expected += 1
        return events

    async def events(
        self,
        *,
        after: RpcEventCursor | None = None,
    ) -> AsyncIterator[RpcClientEvent]:
        cursor = after or self.server_info.cursor
        replayed = await self.replay_events(cursor)
        sequence = cursor.sequence
        for event in replayed:
            sequence = event.cursor.sequence
            yield event
        async for event in self.live_events(
            RpcEventCursor(stream_id=cursor.stream_id, sequence=sequence)
        ):
            yield event

    async def live_events(
        self,
        after: RpcEventCursor,
    ) -> AsyncIterator[RpcClientEvent]:
        """Consume only the local live buffer after a validated cursor."""

        self._require_cursor(after)
        sequence = after.sequence
        while True:
            async with self._condition:
                available = [item for item in self._history if item.cursor.sequence > sequence]
                if available:
                    if available[0].cursor.sequence != sequence + 1:
                        raise RpcEventGapError("live event history contains a gap")
                elif self._terminal_error is not None:
                    raise self._terminal_error
                elif self._closed:
                    return
                else:
                    await self._condition.wait()
                    continue
            for event in available:
                if event.cursor.sequence <= sequence:
                    continue
                if event.cursor.sequence != sequence + 1:
                    raise RpcEventGapError("live event sequence contains a gap")
                sequence = event.cursor.sequence
                yield event

    async def list_confirmations(self) -> tuple[RpcConfirmationRecord, ...]:
        result = await self._request("confirmation.list", {})
        return tuple(
            confirmation_record_from_data(item)
            for item in cast(list[JsonObject], result["pending"])
        )

    async def respond_confirmation(
        self,
        answer: RpcConfirmationAnswer,
    ) -> RpcConfirmationAck:
        result = await self._request("confirmation.respond", _answer_data(answer))
        return confirmation_ack_from_data(result)

    async def respond_confirmations(
        self,
        answers: tuple[RpcConfirmationAnswer, ...],
    ) -> tuple[RpcConfirmationAck, ...]:
        result = await self._request(
            "confirmation.respond_batch",
            {"responses": [_answer_data(item) for item in answers]},
        )
        return tuple(
            confirmation_ack_from_data(item)
            for item in cast(list[JsonObject], result["applied"])
        )

    async def background_errors(self) -> AsyncIterator[RpcClientError]:
        while True:
            item = await self._background_errors.get()
            if item is None:
                return
            yield item

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closing = True
        if not self._connection.closed:
            with suppress(Exception):
                await asyncio.wait_for(
                    self._request("shutdown", {}),
                    timeout=self._shutdown_timeout,
                )
        await self._close_transport()
        self._closed = True
        async with self._condition:
            self._condition.notify_all()
        self._background_errors.put_nowait(None)

    async def _interaction(
        self,
        method: str,
        run_id: str,
        content: str,
    ) -> RpcInteractionReceipt:
        return interaction_receipt_from_result(
            await self._request(method, {"run_id": run_id, "content": content})
        )

    async def _request(self, method: str, params: JsonObject) -> JsonObject:
        if self._closed:
            raise RpcConnectionClosedError("client is closed")
        validate_v2_params(method, params)
        response = await self._connection.send_request(method, params)
        if not response.ok:
            assert response.error is not None
            if response.error.code == "connection_closed":
                raise RpcConnectionClosedError("RPC connection closed")
            raise RpcRemoteError(
                response.error.code,
                response.error.message,
                details=response.error.details,
            )
        assert response.result is not None
        validate_v2_result(method, response.result)
        return response.result

    async def _event_loop(self) -> None:
        last = self.server_info.cursor.sequence
        try:
            async for wire_event in self._connection.received_events():
                if wire_event.stream_id != self.server_info.cursor.stream_id:
                    raise RpcCursorError("received event belongs to another stream")
                if wire_event.sequence <= last:
                    continue
                if wire_event.sequence != last + 1:
                    raise RpcEventGapError("live event sequence contains a gap")
                event = client_event_from_wire(wire_event)
                last = wire_event.sequence
                async with self._condition:
                    self._history.append(event)
                    self._condition.notify_all()
                if (
                    self._confirmation_handler is not None
                    and event.event_type == "confirmation_state_changed"
                    and event.data.get("status") == "pending"
                ):
                    request_id = event.data.get("request_id")
                    if isinstance(request_id, str) and request_id not in self._confirmation_seen:
                        self._confirmation_seen.add(request_id)
                        self._confirmation_queue.put_nowait(request_id)
        except asyncio.CancelledError:
            raise
        except RpcClientError as exc:
            self._terminal_error = exc
        except Exception as exc:
            self._terminal_error = RpcConnectionClosedError(type(exc).__name__)
        finally:
            if not self._closing and self._terminal_error is None:
                self._terminal_error = self._connection.failure or RpcConnectionClosedError(
                    "RPC connection closed"
                )
            async with self._condition:
                self._condition.notify_all()

    async def _confirmation_loop(self) -> None:
        assert self._confirmation_handler is not None
        while True:
            request_id = await self._confirmation_queue.get()
            if request_id is None:
                return
            try:
                record = next(
                    (item for item in await self.list_confirmations() if item.request_id == request_id),
                    None,
                )
                if record is None:
                    continue
                result = self._confirmation_handler(record)
                if not inspect.isawaitable(result):
                    raise TypeError("confirmation handler must be asynchronous")
                answer = await result
                if answer is not None:
                    if not isinstance(answer, RpcConfirmationAnswer):
                        raise TypeError("confirmation handler returned an invalid answer")
                    await self.respond_confirmation(answer)
            except asyncio.CancelledError:
                raise
            except RpcRemoteError as exc:
                if exc.code not in {
                    "stale_revision",
                    "duplicate_response",
                    "expired",
                    "orphaned",
                    "unknown_request",
                }:
                    self._background_errors.put_nowait(exc)
            except Exception as exc:
                self._background_errors.put_nowait(
                    RpcClientError(f"confirmation handler failed: {type(exc).__name__}")
                )

    def _require_cursor(self, cursor: RpcEventCursor) -> None:
        if cursor.stream_id != self.server_info.cursor.stream_id:
            raise RpcCursorError("cursor belongs to another event stream")
        if type(cursor.sequence) is not int or cursor.sequence < 0:
            raise RpcCursorError("cursor sequence is invalid")

    async def _close_transport(self) -> None:
        self._confirmation_queue.put_nowait(None)
        for task in (self._handler_task, self._event_task):
            if task is not None and not task.done():
                task.cancel()
        await self._connection.close()
        if self._connection_task is not None and not self._connection_task.done():
            self._connection_task.cancel()
        tasks = [
            task
            for task in (self._handler_task, self._event_task, self._connection_task)
            if task is not None
        ]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._owns_transport:
            await _close_owned_streams(self._reader, self._writer)
        process = self._process
        if process is not None and process.returncode is None:
            try:
                await asyncio.wait_for(process.wait(), timeout=self._shutdown_timeout)
            except TimeoutError:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=self._shutdown_timeout)
                except TimeoutError:
                    process.kill()
                    await process.wait()
        if self._stderr_task is not None:
            with suppress(Exception):
                await self._stderr_task


def _answer_data(answer: RpcConfirmationAnswer) -> JsonObject:
    return {
        "request_id": answer.request_id,
        "expected_revision": answer.expected_revision,
        "decision": answer.decision,
        "reason": answer.reason,
        "metadata": answer.metadata,
    }


def _json_line(value: JsonObject) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


async def _capture_bounded_stderr(
    stream: asyncio.StreamReader,
    target: bytearray,
    limit: int,
) -> None:
    while True:
        chunk = await stream.read(4096)
        if not chunk:
            return
        remaining = limit - len(target)
        if remaining > 0:
            target.extend(chunk[:remaining])


async def _close_owned_streams(reader: AsyncTextReader, writer: AsyncTextWriter) -> None:
    seen: set[int] = set()
    for stream in (writer, reader):
        if id(stream) in seen:
            continue
        seen.add(id(stream))
        close = getattr(stream, "aclose", None) or getattr(stream, "close", None)
        if close is None:
            continue
        with suppress(Exception):
            result = close()
            if inspect.isawaitable(result):
                await result


__all__ = ["ConfirmationHandler", "EvoPiRpcClient", "RpcRunHandle"]
