from __future__ import annotations

from pathlib import Path

import pytest

from evopi.remote import (
    RemoteAdminCodec,
    RemoteAdminProtocolError,
    resolve_admin_endpoint,
)


def test_admin_codec_is_strict_json_and_rejects_duplicate_keys() -> None:
    payload = RemoteAdminCodec.encode_request(
        request_id="a" * 32,
        method="status",
        params={},
    )
    decoded = RemoteAdminCodec.decode_request(payload)
    assert decoded.method == "status"

    with pytest.raises(RemoteAdminProtocolError, match="duplicate"):
        RemoteAdminCodec.decode_request(
            b'{"schema_version":1,"request_id":"x","request_id":"y",'
            b'"method":"status","params":{}}'
        )

    with pytest.raises(RemoteAdminProtocolError, match="field types"):
        RemoteAdminCodec.decode_request(
            b'{"schema_version":true,"request_id":"x",'
            b'"method":"status","params":{}}'
        )


def test_admin_endpoint_is_host_specific_and_local(tmp_path: Path) -> None:
    endpoint = resolve_admin_endpoint("a" * 32, tmp_path)
    assert endpoint.family in {"AF_PIPE", "AF_UNIX"}
    assert "a" * 12 in endpoint.address
