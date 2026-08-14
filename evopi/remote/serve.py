"""Lifecycle owner for one single-process Remote Gateway Host."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

from evopi.harness import BaseHarness, ConfirmationBroker
from evopi.rpc import EventStream, HarnessRpcHost, HarnessRpcV2Host

from .admin import RemoteAdminServer, resolve_admin_endpoint
from .admin_service import RemoteAdminService
from .audit import RemoteAuditLog
from .controller import RemoteHostController
from .gateway import RemoteGateway
from .security import RemoteGatewayConfig
from .store import RemoteHostStore


async def serve_remote_gateway(
    harness: BaseHarness,
    broker: ConfirmationBroker,
    *,
    store: RemoteHostStore,
    host_name: str,
    gateway_config: RemoteGatewayConfig,
) -> int:
    """Serve exactly one Harness and one Uvicorn worker until shutdown."""

    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("install EvoPi with the 'remote' optional feature") from exc
    host_config = store.load_config(host_name)
    controller = RemoteHostController(store, host_name)
    event_stream = EventStream()
    rpc_v1 = HarnessRpcHost(harness, broker, event_stream=event_stream)
    rpc_v2 = HarnessRpcV2Host(rpc_v1, host_id=host_config.host_id)
    gateway = RemoteGateway(
        host_config=host_config,
        controller=controller,
        rpc_host=rpc_v2,
        gateway_config=gateway_config,
        audit=RemoteAuditLog(store.host_path(host_name) / "audit"),
    )
    admin = RemoteAdminServer(
        resolve_admin_endpoint(host_config.host_id, store.host_path(host_name)),
        store.load_management_secret(host_name),
        RemoteAdminService(
            controller,
            disconnect_device=gateway.disconnect_device,
            audit=gateway.record_security_operation,
        ),
    )
    admin_thread = threading.Thread(
        target=admin.serve_forever,
        name=f"evopi-remote-admin-{host_name}",
        daemon=True,
    )
    admin_thread.start()
    config = uvicorn.Config(
        gateway.create_app(),
        host=gateway_config.bind,
        port=gateway_config.port,
        workers=1,
        ws="websockets",
        ws_max_size=128 * 1024,
        ws_per_message_deflate=False,
        proxy_headers=False,
        ssl_certfile=gateway_config.cert_file,
        ssl_keyfile=gateway_config.key_file,
        log_config=None,
    )
    config.load()
    if config.ssl is not None:
        config.ssl.minimum_version = gateway_config.minimum_tls_version
    server = uvicorn.Server(config)
    try:
        await server.serve()
    finally:
        admin.close()
        await asyncio.to_thread(admin_thread.join, 2.0)
        await rpc_v1.close()
    return 0


def ensure_tls_files(config: RemoteGatewayConfig) -> None:
    for value, label in (
        (config.cert_file, "TLS certificate"),
        (config.key_file, "TLS private key"),
    ):
        if value is not None and not Path(value).expanduser().is_file():
            raise ValueError(f"{label} does not exist")


__all__ = ["ensure_tls_files", "serve_remote_gateway"]
