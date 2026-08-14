"""TLS, proxy, Host, Origin, and client-address security policy."""

from __future__ import annotations

import ipaddress
import ssl
from dataclasses import dataclass
from urllib.parse import urlsplit

from .errors import RemoteSecurityError


@dataclass(slots=True, frozen=True, kw_only=True)
class RemoteGatewayConfig:
    bind: str = "127.0.0.1"
    port: int = 8765
    proxy_mode: bool = False
    cert_file: str | None = None
    key_file: str | None = None
    trusted_proxy_cidrs: tuple[str, ...] = ("127.0.0.0/8", "::1/128")
    allowed_hosts: tuple[str, ...] = ("localhost", "127.0.0.1")
    allowed_origins: tuple[str, ...] = ()
    console_enabled: bool = False
    max_connections: int = 64
    max_connections_per_ip: int = 8
    max_connections_per_device: int = 4
    max_outbound_items: int = 128
    max_outbound_bytes: int = 8 * 1024 * 1024
    handshake_rate_per_minute: int = 10
    pairing_rate_per_minute: int = 5
    request_rate_per_minute: int = 120
    run_rate_per_minute: int = 6
    confirmation_rate_per_minute: int = 30
    minimum_tls_version: ssl.TLSVersion = ssl.TLSVersion.TLSv1_2

    def __post_init__(self) -> None:
        try:
            address = ipaddress.ip_address(self.bind)
        except ValueError as exc:
            raise RemoteSecurityError("bind must be an IP address") from exc
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise RemoteSecurityError("port must be between 1 and 65535")
        if bool(self.cert_file) != bool(self.key_file):
            raise RemoteSecurityError("TLS certificate and private key must be provided together")
        if not address.is_loopback and not self.cert_file:
            raise RemoteSecurityError("non-loopback listeners require TLS")
        if not self.allowed_hosts:
            raise RemoteSecurityError("an explicit Host allowlist is required")
        limits = (
            self.max_connections,
            self.max_connections_per_ip,
            self.max_connections_per_device,
            self.max_outbound_items,
            self.max_outbound_bytes,
            self.handshake_rate_per_minute,
            self.pairing_rate_per_minute,
            self.request_rate_per_minute,
            self.run_rate_per_minute,
            self.confirmation_rate_per_minute,
        )
        if any(type(value) is not int or value <= 0 for value in limits):
            raise RemoteSecurityError("Gateway security limits must be positive integers")
        for cidr in self.trusted_proxy_cidrs:
            try:
                ipaddress.ip_network(cidr, strict=False)
            except ValueError as exc:
                raise RemoteSecurityError("trusted proxy CIDR is invalid") from exc


def resolve_remote_client_ip(
    *, peer_ip: str, forwarded_for: str | None, config: RemoteGatewayConfig
) -> str:
    try:
        peer = ipaddress.ip_address(peer_ip)
    except ValueError as exc:
        raise RemoteSecurityError("peer IP is invalid") from exc
    trusted = any(
        peer in ipaddress.ip_network(cidr, strict=False)
        for cidr in config.trusted_proxy_cidrs
    )
    if not (config.proxy_mode and trusted and forwarded_for):
        return str(peer)
    first = forwarded_for.split(",", 1)[0].strip()
    try:
        return str(ipaddress.ip_address(first))
    except ValueError as exc:
        raise RemoteSecurityError("forwarded client IP is invalid") from exc


def is_trusted_proxy_peer(peer_ip: str, config: RemoteGatewayConfig) -> bool:
    try:
        peer = ipaddress.ip_address(peer_ip)
    except ValueError:
        return False
    return any(
        peer in ipaddress.ip_network(cidr, strict=False)
        for cidr in config.trusted_proxy_cidrs
    )


def validate_remote_request(
    *, host: str | None, origin: str | None, config: RemoteGatewayConfig
) -> None:
    normalized_host = _normalize_host_header(host)
    allowed_hosts = {_normalize_allowed_host(item) for item in config.allowed_hosts}
    if normalized_host not in allowed_hosts:
        raise RemoteSecurityError("Host is not allowed")
    if origin is not None and origin not in config.allowed_origins:
        raise RemoteSecurityError("Origin is not allowed")


def _normalize_host_header(host: str | None) -> str:
    if not host:
        raise RemoteSecurityError("Host is not allowed")
    try:
        parsed = urlsplit(f"//{host}")
        hostname = parsed.hostname
        _ = parsed.port
    except ValueError as exc:
        raise RemoteSecurityError("Host header is malformed") from exc
    if (
        hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise RemoteSecurityError("Host header is malformed")
    return hostname.lower()


def _normalize_allowed_host(host: str) -> str:
    value = host.strip().lower()
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    if not value or "/" in value or "@" in value:
        raise RemoteSecurityError("allowed Host is invalid")
    return value


def create_server_ssl_context(config: RemoteGatewayConfig) -> ssl.SSLContext | None:
    if config.cert_file is None or config.key_file is None:
        return None
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = config.minimum_tls_version
    context.load_cert_chain(config.cert_file, config.key_file)
    return context


__all__ = [
    "RemoteGatewayConfig",
    "create_server_ssl_context",
    "is_trusted_proxy_peer",
    "resolve_remote_client_ip",
    "validate_remote_request",
]
