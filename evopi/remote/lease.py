"""Exclusive, expiring control lease for remote Run mutations."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .errors import RemoteLeaseError


@dataclass(slots=True, frozen=True, kw_only=True)
class ControlLease:
    device_id: str
    connection_id: str
    acquired_at: datetime
    expires_at: datetime
    revision: int


class ControlLeaseManager:
    """Manage one lease without coupling connection loss to Run lifetime."""

    def __init__(
        self,
        *,
        ttl: timedelta = timedelta(seconds=30),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if ttl.total_seconds() <= 0:
            raise ValueError("lease ttl must be positive")
        self._ttl = ttl
        self._clock = clock
        self._current: ControlLease | None = None
        self._revision = 0

    @property
    def current(self) -> ControlLease | None:
        self._expire()
        return self._current

    def acquire(self, *, device_id: str, connection_id: str) -> ControlLease:
        now = self._now()
        self._expire(now)
        current = self._current
        if current is not None and current.device_id != device_id:
            raise RemoteLeaseError("control lease is held by another device")
        self._revision += 1
        lease = ControlLease(
            device_id=_identity(device_id, "device_id"),
            connection_id=_identity(connection_id, "connection_id"),
            acquired_at=now if current is None else current.acquired_at,
            expires_at=now + self._ttl,
            revision=self._revision,
        )
        self._current = lease
        return lease

    def renew(self, *, device_id: str, connection_id: str) -> ControlLease:
        now = self._now()
        self._expire(now)
        current = self._current
        if (
            current is None
            or current.device_id != device_id
            or current.connection_id != connection_id
        ):
            raise RemoteLeaseError("connection does not hold the control lease")
        self._revision += 1
        renewed = ControlLease(
            device_id=current.device_id,
            connection_id=current.connection_id,
            acquired_at=current.acquired_at,
            expires_at=now + self._ttl,
            revision=self._revision,
        )
        self._current = renewed
        return renewed

    def require_holder(self, connection_id: str) -> ControlLease:
        current = self.current
        if current is None or current.connection_id != connection_id:
            raise RemoteLeaseError("connection does not hold the control lease")
        return current

    def is_holder(self, connection_id: str) -> bool:
        current = self.current
        return current is not None and current.connection_id == connection_id

    def release_connection(self, connection_id: str) -> bool:
        current = self.current
        if current is None or current.connection_id != connection_id:
            return False
        self._current = None
        return True

    def revoke_device(self, device_id: str) -> bool:
        current = self.current
        if current is None or current.device_id != device_id:
            return False
        self._current = None
        return True

    def _expire(self, now: datetime | None = None) -> None:
        current = self._current
        if current is not None and current.expires_at <= (now or self._now()):
            self._current = None

    def _now(self) -> datetime:
        value = self._clock()
        if value.utcoffset() != timedelta(0):
            raise RemoteLeaseError("lease clock must return a UTC datetime")
        return value


def _identity(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RemoteLeaseError(f"{field} must be non-empty")
    return value


__all__ = ["ControlLease", "ControlLeaseManager"]
