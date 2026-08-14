from __future__ import annotations

import ssl

import pytest

from evopi.remote import (
    RemoteGatewayConfig,
    RemoteSecurityError,
    is_trusted_proxy_peer,
    resolve_remote_client_ip,
    validate_remote_request,
)


def test_non_loopback_plaintext_listener_is_rejected() -> None:
    with pytest.raises(RemoteSecurityError, match="TLS"):
        RemoteGatewayConfig(bind="0.0.0.0", allowed_hosts=("agent.example",))


def test_direct_tls_requires_host_allowlist_and_tls_12() -> None:
    config = RemoteGatewayConfig(
        bind="0.0.0.0",
        cert_file="cert.pem",
        key_file="key.pem",
        allowed_hosts=("agent.example",),
    )
    assert config.minimum_tls_version is ssl.TLSVersion.TLSv1_2


def test_forwarded_header_is_only_trusted_from_explicit_proxy() -> None:
    config = RemoteGatewayConfig(
        bind="127.0.0.1",
        proxy_mode=True,
        trusted_proxy_cidrs=("127.0.0.0/8",),
        allowed_hosts=("agent.example",),
    )
    assert (
        resolve_remote_client_ip(
            peer_ip="127.0.0.1", forwarded_for="203.0.113.9, 127.0.0.1", config=config
        )
        == "203.0.113.9"
    )
    assert (
        resolve_remote_client_ip(
            peer_ip="198.51.100.2", forwarded_for="203.0.113.9", config=config
        )
        == "198.51.100.2"
    )
    assert is_trusted_proxy_peer("127.0.0.1", config) is True
    assert is_trusted_proxy_peer("198.51.100.2", config) is False


def test_host_and_browser_origin_require_exact_allowlist_match() -> None:
    config = RemoteGatewayConfig(
        bind="127.0.0.1",
        proxy_mode=True,
        allowed_hosts=("agent.example",),
        allowed_origins=("https://agent.example",),
    )
    validate_remote_request(
        host="agent.example", origin="https://agent.example", config=config
    )
    validate_remote_request(host="agent.example", origin=None, config=config)

    with pytest.raises(RemoteSecurityError, match="Host"):
        validate_remote_request(host="evil.example", origin=None, config=config)
    with pytest.raises(RemoteSecurityError, match="Origin"):
        validate_remote_request(
            host="agent.example", origin="https://evil.example", config=config
        )


def test_ipv6_host_header_is_parsed_without_losing_the_address() -> None:
    config = RemoteGatewayConfig(bind="::1", allowed_hosts=("::1",))

    validate_remote_request(host="[::1]:8765", origin=None, config=config)

    with pytest.raises(RemoteSecurityError, match="Host"):
        validate_remote_request(host="[::2]:8765", origin=None, config=config)
