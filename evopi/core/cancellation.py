"""Run-scoped cooperative cancellation primitives."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from datetime import UTC, datetime
from threading import Lock
from typing import Any


class AbortSignal:
    """Read-only view of one run's cancellation state."""

    def __init__(self, *, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._event = asyncio.Event()
        self._notified = asyncio.Event()
        self._lock = Lock()
        self._requested_at: datetime | None = None

    @property
    def aborted(self) -> bool:
        with self._lock:
            return self._requested_at is not None

    @property
    def requested_at(self) -> datetime | None:
        with self._lock:
            return self._requested_at

    async def wait(self) -> None:
        await self._event.wait()

    def _request(self) -> bool:
        with self._lock:
            if self._requested_at is not None:
                return False
            self._requested_at = datetime.now(UTC)
        try:
            self._loop.call_soon_threadsafe(self._event.set)
        except RuntimeError:
            # The owning run is already gone; the timestamp still makes the
            # request idempotent and observable to any remaining caller.
            pass
        return True

    async def _wait_until_notified(self) -> None:
        if self.aborted:
            await self._notified.wait()

    def _mark_notified(self) -> None:
        self._notified.set()


class AbortController:
    """Internal mutable owner of an AbortSignal."""

    def __init__(self, *, loop: asyncio.AbstractEventLoop) -> None:
        self.signal = AbortSignal(loop=loop)

    def abort(self) -> bool:
        return self.signal._request()


def accepts_signal(callback: Callable[..., Any]) -> bool:
    """Return whether a callback explicitly accepts the optional signal keyword."""

    try:
        parameters = inspect.signature(callback).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.name == "signal" or parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    )


def call_with_optional_signal(
    callback: Callable[..., Any],
    *args: Any,
    signal: AbortSignal | None,
) -> Any:
    if accepts_signal(callback):
        return callback(*args, signal=signal)
    return callback(*args)


__all__ = ["AbortSignal"]
