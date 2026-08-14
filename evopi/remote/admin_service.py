"""Local-only administrative method dispatcher for a running Gateway."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from .admin import RemoteAdminRequest, RemoteAdminResponse
from .controller import RemoteHostController
from .models import DeviceRecord, PairingRequest


class RemoteAdminService:
    def __init__(
        self,
        controller: RemoteHostController,
        *,
        disconnect_device: Callable[[str], None] | None = None,
        audit: Callable[[str, str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.controller = controller
        self._disconnect_device = disconnect_device
        self._audit = audit

    def __call__(self, request: RemoteAdminRequest) -> RemoteAdminResponse:
        try:
            result = self.dispatch(request.method, dict(request.params))
        except Exception as exc:
            if self._audit is not None:
                self._audit(
                    f"admin.{request.method}",
                    "denied",
                    _audit_details(dict(request.params)),
                )
            return RemoteAdminResponse(
                request_id=request.request_id,
                ok=False,
                error=f"management operation rejected: {type(exc).__name__}: {exc}",
            )
        if self._audit is not None:
            self._audit(
                f"admin.{request.method}",
                "allowed",
                _audit_details(dict(request.params)),
            )
        return RemoteAdminResponse(request_id=request.request_id, ok=True, result=result)

    def dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "status":
            return {
                "pending_request_count": len(self.controller.pending_requests),
                "device_count": len(self.controller.devices),
            }
        if method == "pair.issue":
            code = self.controller.issue_pairing_code()
            return {"code": code.code, "expires_at": code.expires_at.isoformat()}
        if method == "requests.list":
            return {"requests": [_request_dict(item) for item in self.controller.pending_requests]}
        if method == "requests.approve":
            device = self.controller.approve(
                _string(params, "request_id"), scopes=_strings(params, "scopes")
            )
            return {"device": _device_dict(device)}
        if method == "requests.deny":
            denied = self.controller.deny(_string(params, "request_id"))
            return {"request": _request_dict(denied)}
        if method == "devices.list":
            return {"devices": [_device_dict(item) for item in self.controller.devices]}
        if method == "devices.scopes":
            device = self.controller.update_scopes(
                _string(params, "device_id"), scopes=_strings(params, "scopes")
            )
            if self._disconnect_device is not None:
                self._disconnect_device(device.device_id)
            return {"device": _device_dict(device)}
        if method == "devices.revoke":
            device = self.controller.revoke(_string(params, "device_id"))
            if self._disconnect_device is not None:
                self._disconnect_device(device.device_id)
            return {"device": _device_dict(device)}
        raise ValueError("unknown local management method")


def _string(params: dict[str, Any], key: str) -> str:
    value = params.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _strings(params: dict[str, Any], key: str) -> Sequence[str]:
    value = params.get(key)
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{key} must be a string array")
    return value


def _request_dict(item: PairingRequest) -> dict[str, Any]:
    return {
        "request_id": item.request_id,
        "device_name": item.device_name,
        "fingerprint": item.fingerprint,
        "created_at": item.created_at.isoformat(),
        "status": item.status,
    }


def _device_dict(item: DeviceRecord) -> dict[str, Any]:
    return {
        "device_id": item.device_id,
        "device_name": item.device_name,
        "fingerprint": item.fingerprint,
        "scopes": [scope.value for scope in item.scopes],
        "created_at": item.created_at.isoformat(),
        "approved_at": item.approved_at.isoformat(),
        "revoked_at": item.revoked_at.isoformat() if item.revoked_at else None,
        "revision": item.revision,
        "active": item.active,
    }


def _audit_details(params: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in params.items()
        if key in {"request_id", "device_id", "scopes"}
    }


__all__ = ["RemoteAdminService"]
