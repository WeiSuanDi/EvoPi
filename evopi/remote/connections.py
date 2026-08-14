"""Bounded Remote connection and outbound-queue accounting."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from .errors import RemoteRateLimitError


@dataclass(slots=True, kw_only=True)
class _Connection:
    client_ip: str
    device_id: str | None = None


class RemoteConnectionRegistry:
    def __init__(
        self,
        *,
        max_global: int = 64,
        max_per_ip: int = 8,
        max_per_device: int = 4,
    ) -> None:
        if min(max_global, max_per_ip, max_per_device) <= 0:
            raise ValueError("connection limits must be positive")
        self._max_global = max_global
        self._max_per_ip = max_per_ip
        self._max_per_device = max_per_device
        self._connections: dict[str, _Connection] = {}

    def open(self, connection_id: str, client_ip: str) -> None:
        if connection_id in self._connections:
            raise RemoteRateLimitError("duplicate connection")
        if len(self._connections) >= self._max_global:
            raise RemoteRateLimitError("global connection limit exceeded")
        if sum(item.client_ip == client_ip for item in self._connections.values()) >= self._max_per_ip:
            raise RemoteRateLimitError("per-IP connection limit exceeded")
        self._connections[connection_id] = _Connection(client_ip=client_ip)

    def authenticate(self, connection_id: str, device_id: str) -> None:
        connection = self._connections.get(connection_id)
        if connection is None:
            raise RemoteRateLimitError("connection is not registered")
        if (
            sum(item.device_id == device_id for item in self._connections.values())
            >= self._max_per_device
        ):
            raise RemoteRateLimitError("per-device connection limit exceeded")
        connection.device_id = device_id

    def device_connections(self, device_id: str) -> tuple[str, ...]:
        return tuple(
            key for key, value in self._connections.items() if value.device_id == device_id
        )

    def close(self, connection_id: str) -> None:
        self._connections.pop(connection_id, None)


class RemoteSendQueue:
    def __init__(self, *, max_items: int = 128, max_bytes: int = 8 * 1024 * 1024) -> None:
        if max_items <= 0 or max_bytes <= 0:
            raise ValueError("queue limits must be positive")
        self._queue: asyncio.Queue[tuple[str, int] | None] = asyncio.Queue(maxsize=max_items)
        self._max_bytes = max_bytes
        self._bytes = 0

    def put_nowait(self, payload: str) -> None:
        size = len(payload.encode("utf-8"))
        if self._queue.full() or self._bytes + size > self._max_bytes:
            raise RemoteRateLimitError("outbound queue limit exceeded")
        self._queue.put_nowait((payload, size))
        self._bytes += size

    async def get(self) -> str | None:
        item = await self._queue.get()
        if item is None:
            return None
        payload, size = item
        self._bytes -= size
        return payload

    def close(self) -> None:
        try:
            self._queue.put_nowait(None)
        except asyncio.QueueFull:
            pass


__all__ = ["RemoteConnectionRegistry", "RemoteSendQueue"]
