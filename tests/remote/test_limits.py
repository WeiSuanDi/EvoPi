from __future__ import annotations

from datetime import UTC, datetime, timedelta

from evopi.remote import RemoteRateLimiter


def test_rate_limiter_enforces_window_and_recovers() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    current = [now]
    limiter = RemoteRateLimiter(max_entries=10, clock=lambda: current[0])

    assert limiter.allow("device-1", limit=2, window=timedelta(minutes=1))
    assert limiter.allow("device-1", limit=2, window=timedelta(minutes=1))
    assert not limiter.allow("device-1", limit=2, window=timedelta(minutes=1))

    current[0] = now + timedelta(seconds=61)
    assert limiter.allow("device-1", limit=2, window=timedelta(minutes=1))


def test_rate_limiter_bounds_attacker_controlled_keys() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    limiter = RemoteRateLimiter(max_entries=2, clock=lambda: now)

    assert limiter.allow("one", limit=1, window=timedelta(minutes=1))
    assert limiter.allow("two", limit=1, window=timedelta(minutes=1))
    assert not limiter.allow("three", limit=1, window=timedelta(minutes=1))
    assert limiter.tracked_entries == 2
