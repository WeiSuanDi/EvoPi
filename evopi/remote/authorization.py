"""Scope and lease enforcement adapter over the one shared RPC v2 Host."""

from __future__ import annotations

from collections.abc import Sequence

from evopi.core.types import JsonObject
from evopi.rpc.errors import RpcHostError
from evopi.rpc.server_v2 import RpcV2Host

from .lease import ControlLeaseManager
from .models import DeviceScope


class RemoteAuthorizedRpcHost:
    """Expose only the RPC methods authorized for one authenticated connection."""

    def __init__(
        self,
        host: RpcV2Host,
        *,
        device_id: str,
        connection_id: str,
        scopes: Sequence[DeviceScope],
        leases: ControlLeaseManager,
    ) -> None:
        self._host = host
        self.device_id = device_id
        self.connection_id = connection_id
        self.scopes = frozenset(scopes)
        self._leases = leases

    async def initialize(self, params: JsonObject) -> JsonObject:
        return await self._host.initialize(params)

    async def runtime_status(self, params: JsonObject) -> JsonObject:
        self._require(DeviceScope.OBSERVE)
        return await self._host.runtime_status(params)

    async def events_replay(self, params: JsonObject) -> JsonObject:
        self._require(DeviceScope.OBSERVE)
        return await self._host.events_replay(params)

    async def run_start(self, params: JsonObject) -> JsonObject:
        self._require_control()
        return await self._host.run_start(params)

    async def run_steer(self, params: JsonObject) -> JsonObject:
        self._require_control()
        return await self._host.run_steer(params)

    async def run_follow_up(self, params: JsonObject) -> JsonObject:
        self._require_control()
        return await self._host.run_follow_up(params)

    async def run_abort(self, params: JsonObject) -> JsonObject:
        self._require_control()
        return await self._host.run_abort(params)

    async def confirmation_list(self, params: JsonObject) -> JsonObject:
        self._require(DeviceScope.CONFIRM)
        return await self._host.confirmation_list(params)

    async def confirmation_respond(self, params: JsonObject) -> JsonObject:
        self._require(DeviceScope.CONFIRM)
        return await self._host.confirmation_respond(params)

    async def confirmation_respond_batch(self, params: JsonObject) -> JsonObject:
        self._require(DeviceScope.CONFIRM)
        return await self._host.confirmation_respond_batch(params)

    async def shutdown(self, params: JsonObject) -> JsonObject:
        del params
        raise RpcHostError(
            code="remote_shutdown_disabled",
            message="shutdown is disabled for remote clients",
            details={},
        )

    def _require(self, scope: DeviceScope) -> None:
        if scope not in self.scopes:
            raise RpcHostError(
                code="remote_scope_required",
                message=f"remote {scope.value} scope is required",
                details={"required_scope": scope.value},
            )

    def _require_control(self) -> None:
        self._require(DeviceScope.CONTROL)
        try:
            self._leases.require_holder(self.connection_id)
        except Exception as exc:
            raise RpcHostError(
                code="remote_lease_required",
                message="a live control lease is required",
                details={},
            ) from exc


__all__ = ["RemoteAuthorizedRpcHost"]
