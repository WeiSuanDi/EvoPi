"""Bounded in-process rate and connection accounting for Remote Gateway."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime, timedelta


class RemoteRateLimiter:
    """Fixed-window-equivalent sliding limiter with bounded attacker keys."""

    def __init__(
        self,
        *,
        max_entries: int = 4096,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if type(max_entries) is not int or max_entries <= 0:
            raise ValueError("max_entries must be a positive integer")
        self._max_entries = max_entries
        self._clock = clock
        self._entries: dict[str, deque[datetime]] = {}

    @property
    def tracked_entries(self) -> int:
        return len(self._entries)

    def allow(self, key: str, *, limit: int, window: timedelta) -> bool:
        if not key or type(limit) is not int or limit <= 0:
            raise ValueError("rate limit key and limit must be valid")
        if window.total_seconds() <= 0:
            raise ValueError("rate limit window must be positive")
        now = self._clock()
        cutoff = now - window
        bucket = self._entries.get(key)
        if bucket is None:
            self._prune_empty(cutoff)
            if len(self._entries) >= self._max_entries:
                return False
            bucket = deque()
            self._entries[key] = bucket
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        if len(bucket) >= limit:
            return False
        bucket.append(now)
        return True

    def _prune_empty(self, cutoff: datetime) -> None:
        for key, bucket in tuple(self._entries.items()):
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if not bucket:
                self._entries.pop(key, None)


__all__ = ["RemoteRateLimiter"]
