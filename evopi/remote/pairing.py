"""In-memory pairing state machine shared by durable and hosted implementations."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, Mapping, Sequence
from uuid import uuid4

from .crypto import jwk_fingerprint, public_key_from_jwk
from .errors import RemotePairingError
from .models import (
    DeviceRecord,
    PairingCode,
    PairingRequest,
    normalize_scopes,
)

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _code_digest(code: str) -> str:
    return hashlib.sha256(code.replace("-", "").upper().encode("ascii")).hexdigest()


def _new_code() -> str:
    raw = "".join(secrets.choice(_ALPHABET) for _ in range(12))
    return f"{raw[:4]}-{raw[4:8]}-{raw[8:]}"


class PairingRegistry:
    """Strict state transitions; persistence is supplied by ``RemoteStateStore``."""

    def __init__(self) -> None:
        self._codes: dict[str, datetime] = {}
        self._requests: dict[str, PairingRequest] = {}
        self._devices: dict[str, DeviceRecord] = {}

    @property
    def pending_requests(self) -> tuple[PairingRequest, ...]:
        return tuple(item for item in self._requests.values() if item.status == "pending")

    @property
    def devices(self) -> tuple[DeviceRecord, ...]:
        return tuple(self._devices.values())

    def issue_code(
        self,
        *,
        ttl: timedelta = timedelta(minutes=10),
        now: datetime | None = None,
    ) -> PairingCode:
        issued_at = now or datetime.now(UTC)
        code = _new_code()
        expires_at = issued_at + ttl
        self._codes[_code_digest(code)] = expires_at
        return PairingCode(code=code, expires_at=expires_at)

    def submit(
        self,
        *,
        code: str,
        device_name: str,
        public_jwk: Mapping[str, str],
        now: datetime | None = None,
    ) -> PairingRequest:
        current = now or datetime.now(UTC)
        digest = _code_digest(code)
        expires_at = self._codes.pop(digest, None)
        if expires_at is None or expires_at <= current:
            raise RemotePairingError("pairing code is invalid or expired")
        name = device_name.strip()
        if not name or len(name.encode("utf-8")) > 128:
            raise RemotePairingError("device name must be 1-128 UTF-8 bytes")
        try:
            public_key_from_jwk(public_jwk)
            fingerprint = jwk_fingerprint(public_jwk)
        except Exception as exc:
            raise RemotePairingError("device public key is invalid") from exc
        if any(item.fingerprint == fingerprint for item in self._devices.values()):
            raise RemotePairingError("device public key is already registered")
        if any(item.fingerprint == fingerprint for item in self.pending_requests):
            raise RemotePairingError("device public key already has a pending request")
        request = PairingRequest(
            request_id=uuid4().hex,
            device_name=name,
            public_jwk=dict(public_jwk),
            fingerprint=fingerprint,
            created_at=current,
        )
        self._requests[request.request_id] = request
        return request

    def approve(
        self,
        request_id: str,
        *,
        scopes: Sequence[str],
        now: datetime | None = None,
    ) -> DeviceRecord:
        request = self._require_pending(request_id)
        approved_at = now or datetime.now(UTC)
        device = DeviceRecord(
            device_id=uuid4().hex,
            device_name=request.device_name,
            public_jwk=dict(request.public_jwk),
            fingerprint=request.fingerprint,
            scopes=normalize_scopes(tuple(scopes)),
            created_at=request.created_at,
            approved_at=approved_at,
        )
        self._requests[request_id] = replace(request, status="approved")
        self._devices[device.device_id] = device
        return device

    def deny(self, request_id: str) -> PairingRequest:
        request = self._require_pending(request_id)
        denied = replace(request, status="denied")
        self._requests[request_id] = denied
        return denied

    def revoke(
        self, device_id: str, *, now: datetime | None = None
    ) -> DeviceRecord:
        device = self._devices.get(device_id)
        if device is None:
            raise RemotePairingError("device is unknown")
        if device.revoked_at is not None:
            raise RemotePairingError("device is already revoked")
        revoked = replace(
            device,
            revoked_at=now or datetime.now(UTC),
            revision=device.revision + 1,
        )
        self._devices[device_id] = revoked
        return revoked

    def _require_pending(self, request_id: str) -> PairingRequest:
        request = self._requests.get(request_id)
        if request is None or request.status != "pending":
            raise RemotePairingError("pairing request is not pending")
        return request

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "codes": [
                {"digest": digest, "expires_at": expires_at.isoformat()}
                for digest, expires_at in sorted(self._codes.items())
            ],
            "requests": [
                {
                    "request_id": item.request_id,
                    "device_name": item.device_name,
                    "public_jwk": dict(item.public_jwk),
                    "fingerprint": item.fingerprint,
                    "created_at": item.created_at.isoformat(),
                    "status": item.status,
                }
                for item in self._requests.values()
            ],
            "devices": [
                {
                    "device_id": item.device_id,
                    "device_name": item.device_name,
                    "public_jwk": dict(item.public_jwk),
                    "fingerprint": item.fingerprint,
                    "scopes": [scope.value for scope in item.scopes],
                    "created_at": item.created_at.isoformat(),
                    "approved_at": item.approved_at.isoformat(),
                    "revoked_at": (
                        item.revoked_at.isoformat() if item.revoked_at is not None else None
                    ),
                    "revision": item.revision,
                }
                for item in self._devices.values()
            ],
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> PairingRegistry:
        if set(raw) != {"schema_version", "codes", "requests", "devices"}:
            raise RemotePairingError("Remote security state has invalid fields")
        if raw.get("schema_version") != 1:
            raise RemotePairingError("unsupported Remote security state")
        codes = raw.get("codes")
        requests = raw.get("requests")
        devices = raw.get("devices")
        if not all(isinstance(value, list) for value in (codes, requests, devices)):
            raise RemotePairingError("Remote security state arrays are invalid")
        registry = cls()
        assert isinstance(codes, list)
        assert isinstance(requests, list)
        assert isinstance(devices, list)
        try:
            for value in codes:
                if not isinstance(value, dict) or set(value) != {"digest", "expires_at"}:
                    raise ValueError
                registry._codes[str(value["digest"])] = datetime.fromisoformat(
                    str(value["expires_at"])
                )
            for value in requests:
                if not isinstance(value, dict):
                    raise ValueError
                request = PairingRequest(
                    request_id=str(value["request_id"]),
                    device_name=str(value["device_name"]),
                    public_jwk=dict(value["public_jwk"]),
                    fingerprint=str(value["fingerprint"]),
                    created_at=datetime.fromisoformat(str(value["created_at"])),
                    status=value["status"],
                )
                registry._requests[request.request_id] = request
            for value in devices:
                if not isinstance(value, dict):
                    raise ValueError
                revoked_raw = value["revoked_at"]
                device = DeviceRecord(
                    device_id=str(value["device_id"]),
                    device_name=str(value["device_name"]),
                    public_jwk=dict(value["public_jwk"]),
                    fingerprint=str(value["fingerprint"]),
                    scopes=normalize_scopes(tuple(value["scopes"])),
                    created_at=datetime.fromisoformat(str(value["created_at"])),
                    approved_at=datetime.fromisoformat(str(value["approved_at"])),
                    revoked_at=(
                        datetime.fromisoformat(str(revoked_raw))
                        if revoked_raw is not None
                        else None
                    ),
                    revision=int(value["revision"]),
                )
                registry._devices[device.device_id] = device
        except (KeyError, TypeError, ValueError) as exc:
            raise RemotePairingError("Remote security state is malformed") from exc
        return registry


__all__ = ["PairingRegistry"]
