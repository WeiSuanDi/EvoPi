from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from evopi.remote import ControlLeaseManager, RemoteLeaseError


def test_control_lease_allows_same_device_takeover_and_expires() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    current = [now]
    lease = ControlLeaseManager(ttl=timedelta(seconds=30), clock=lambda: current[0])

    first = lease.acquire(device_id="device-a", connection_id="connection-1")
    taken = lease.acquire(device_id="device-a", connection_id="connection-2")

    assert first.device_id == taken.device_id
    assert taken.connection_id == "connection-2"
    assert lease.is_holder("connection-2") is True

    current[0] = now + timedelta(seconds=31)
    replacement = lease.acquire(device_id="device-b", connection_id="connection-3")
    assert replacement.device_id == "device-b"


def test_control_lease_rejects_other_device_until_expiry() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    lease = ControlLeaseManager(ttl=timedelta(seconds=30), clock=lambda: now)
    lease.acquire(device_id="device-a", connection_id="connection-1")

    with pytest.raises(RemoteLeaseError, match="held by another device"):
        lease.acquire(device_id="device-b", connection_id="connection-2")

    with pytest.raises(RemoteLeaseError, match="does not hold"):
        lease.renew(device_id="device-a", connection_id="connection-2")


def test_release_by_connection_does_not_abort_or_transfer_lease() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    lease = ControlLeaseManager(ttl=timedelta(seconds=30), clock=lambda: now)
    lease.acquire(device_id="device-a", connection_id="connection-1")

    assert lease.release_connection("connection-2") is False
    assert lease.release_connection("connection-1") is True
    assert lease.current is None
