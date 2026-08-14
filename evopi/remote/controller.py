"""Transactional local authority for pairing and device lifecycle."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from .errors import RemoteAuthorizationError
from .models import DeviceRecord, PairingCode, PairingRequest
from .pairing import PairingRegistry
from .store import RemoteHostStore


class RemoteHostController:
    def __init__(self, store: RemoteHostStore, host_name: str) -> None:
        self.store = store
        self.host_name = host_name
        self._registry = store.load_registry(host_name)

    @property
    def pending_requests(self) -> tuple[PairingRequest, ...]:
        return self._registry.pending_requests

    @property
    def devices(self) -> tuple[DeviceRecord, ...]:
        return self._registry.devices

    def get_device(self, device_id: str) -> DeviceRecord:
        device = next(
            (item for item in self._registry.devices if item.device_id == device_id),
            None,
        )
        if device is None or not device.active:
            raise RemoteAuthorizationError("remote device is unknown or revoked")
        return device

    def issue_pairing_code(self) -> PairingCode:
        candidate = self._copy()
        result = candidate.issue_code()
        self._commit(candidate)
        return result

    def submit_pairing(
        self,
        *,
        code: str,
        device_name: str,
        public_jwk: Mapping[str, str],
    ) -> PairingRequest:
        candidate = self._copy()
        result = candidate.submit(
            code=code, device_name=device_name, public_jwk=public_jwk
        )
        self._commit(candidate)
        return result

    def approve(self, request_id: str, *, scopes: Sequence[str]) -> DeviceRecord:
        candidate = self._copy()
        result = candidate.approve(request_id, scopes=scopes)
        self._commit(candidate)
        return result

    def deny(self, request_id: str) -> PairingRequest:
        candidate = self._copy()
        result = candidate.deny(request_id)
        self._commit(candidate)
        return result

    def revoke(self, device_id: str) -> DeviceRecord:
        candidate = self._copy()
        result = candidate.revoke(device_id)
        self._commit(candidate)
        return result

    def update_scopes(self, device_id: str, *, scopes: Sequence[str]) -> DeviceRecord:
        candidate = self._copy()
        result = candidate.update_scopes(device_id, scopes=scopes)
        self._commit(candidate)
        return result

    def _copy(self) -> PairingRegistry:
        return PairingRegistry.from_dict(self._registry.to_dict())

    def _commit(self, candidate: PairingRegistry) -> None:
        self.store.save_registry(self.host_name, candidate)
        self._registry = candidate


__all__ = ["RemoteHostController"]
