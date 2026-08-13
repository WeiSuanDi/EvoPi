"""Strict wire-envelope coverage for the RPC v2 protocol."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from evopi.rpc import RpcCodecError
from evopi.rpc.codec import decode_envelope
from evopi.rpc.codec_v2 import (
    decode_v2_envelope,
    encode_v2_event,
    encode_v2_request,
    encode_v2_response,
)
from evopi.rpc.protocol_v2 import (
    RpcV2ErrorInfo,
    RpcV2Event,
    RpcV2Request,
    RpcV2Response,
)


def test_v2_request_round_trip_is_distinct_from_v1() -> None:
    request = RpcV2Request(
        request_id="request-1",
        method="initialize",
        params={"client_name": "tests", "client_version": "1.0"},
    )

    encoded = encode_v2_request(request)

    assert decode_v2_envelope(encoded) == request
    with pytest.raises(RpcCodecError, match="unknown schema version"):
        decode_envelope(encoded)


def test_v2_response_round_trip_preserves_structured_error() -> None:
    response = RpcV2Response(
        request_id="request-2",
        ok=False,
        error=RpcV2ErrorInfo(
            code="run_mismatch",
            message="run does not match the active run",
            details={"active_run_id": "run-2"},
        ),
    )

    assert decode_v2_envelope(encode_v2_response(response)) == response


def test_v2_event_requires_stream_identity() -> None:
    event = RpcV2Event(
        event_id="11111111-2222-4333-8444-555555555555",
        stream_id="aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
        sequence=1,
        type="agent_start",
        data={},
        run_id="run-1",
        created_at=datetime(2026, 8, 11, tzinfo=UTC),
    )

    encoded = encode_v2_event(event)

    assert decode_v2_envelope(encoded) == event
    with pytest.raises(RpcCodecError, match="malformed v2 envelope"):
        decode_v2_envelope(encoded.replace('"stream_id":"aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",', ""))


def test_v2_codec_rejects_duplicate_keys_and_non_finite_values() -> None:
    duplicate = (
        '{"request_id":"one","request_id":"two","method":"initialize",'
        '"params":{},"schema_version":2}'
    )
    with pytest.raises(RpcCodecError, match="invalid JSON"):
        decode_v2_envelope(duplicate)

    request = RpcV2Request(
        request_id="request-3",
        method="runtime.status",
        params={"bad": float("nan")},
    )
    with pytest.raises(RpcCodecError, match="JSON-safe"):
        encode_v2_request(request)
