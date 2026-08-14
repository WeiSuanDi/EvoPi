"""Immutable identity and pairing records for one Remote Host."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal, Mapping

from .errors import RemoteContractError


class DeviceScope(StrEnum):
    OBSERVE = "observe"
    CONTROL = "control"
    CONFIRM = "confirm"


_SCOPE_ORDER = (DeviceScope.OBSERVE, DeviceScope.CONTROL, DeviceScope.CONFIRM)


def normalize_scopes(scopes: list[str] | tuple[str, ...]) -> tuple[DeviceScope, ...]:
    try:
        selected = {DeviceScope(item) for item in scopes}
    except ValueError as exc:
        raise RemoteContractError("unknown device scope") from exc
    if DeviceScope.CONTROL in selected:
        selected.add(DeviceScope.OBSERVE)
    if not selected:
        raise RemoteContractError("at least one device scope is required")
    return tuple(scope for scope in _SCOPE_ORDER if scope in selected)


@dataclass(slots=True, frozen=True, kw_only=True)
class AuthChallenge:
    host_id: str
    device_id: str
    connection_id: str
    nonce: str
    issued_at: datetime
    expires_at: datetime
    protocol: str = "evopi.remote.v1"

    def with_connection_id(self, connection_id: str) -> AuthChallenge:
        return replace(self, connection_id=connection_id)


@dataclass(slots=True, frozen=True, kw_only=True)
class PairingCode:
    code: str = field(repr=False)
    expires_at: datetime


@dataclass(slots=True, frozen=True, kw_only=True)
class PairingRequest:
    request_id: str
    device_name: str
    public_jwk: Mapping[str, str]
    fingerprint: str
    created_at: datetime
    status: Literal["pending", "approved", "denied"] = "pending"


@dataclass(slots=True, frozen=True, kw_only=True)
class DeviceRecord:
    device_id: str
    device_name: str
    public_jwk: Mapping[str, str]
    fingerprint: str
    scopes: tuple[DeviceScope, ...]
    created_at: datetime
    approved_at: datetime
    revoked_at: datetime | None = None
    revision: int = 1

    @property
    def active(self) -> bool:
        return self.revoked_at is None


def utc_now() -> datetime:
    return datetime.now(UTC)


__all__ = [
    "AuthChallenge",
    "DeviceRecord",
    "DeviceScope",
    "PairingCode",
    "PairingRequest",
    "normalize_scopes",
    "utc_now",
]
