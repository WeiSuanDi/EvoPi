"""Round-trip tests for the RPC v1 wire envelopes and strict codec."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from evopi.rpc import (
    RpcEnvelope,
    RpcErrorInfo,
    RpcEvent,
    RpcRequest,
    RpcResponse,
    decode_envelope,
    decode_event,
    decode_request,
    decode_response,
    encode_event,
    encode_request,
    encode_response,
)


def _ts() -> str:
    return "2026-08-04T10:00:00.123456+00:00"


def test_request_defaults_and_roundtrip() -> None:
    request = RpcRequest(
        request_id=str(uuid4()),
        method="confirmation.respond",
        params={"request_id": "req-1", "status": "approved"},
    )
    assert request.schema_version == 1
    decoded = decode_request(encode_request(request))
    assert decoded == request


def test_response_ok_roundtrip_omits_none_fields() -> None:
    response = RpcResponse(
        request_id=str(uuid4()),
        ok=True,
        result={"status": "running"},
    )
    line = encode_response(response)
    assert "result" in line
    assert "error" not in line
    assert decode_response(line) == response


def test_response_error_roundtrip_omits_none_fields() -> None:
    response = RpcResponse(
        request_id=str(uuid4()),
        ok=False,
        error=RpcErrorInfo(code="method_not_found", message="unknown method", details={"method": "nope"}),
    )
    line = encode_response(response)
    assert "result" not in line
    decoded = decode_response(line)
    assert decoded == response
    assert decoded.error == RpcErrorInfo(
        code="method_not_found", message="unknown method", details={"method": "nope"}
    )


def test_event_roundtrip() -> None:
    created = datetime(2026, 8, 4, 10, 0, 0, 123456, tzinfo=UTC)
    event = RpcEvent(
        event_id=str(uuid4()),
        sequence=7,
        type="tool_execution_start",
        data={"name": "work", "nested": {"count": 2}},
        run_id=str(uuid4()),
        created_at=created,
    )
    decoded = decode_event(encode_event(event))
    assert decoded == event
    assert decoded.created_at == created


def test_event_run_id_none_roundtrip() -> None:
    event = RpcEvent(
        event_id=str(uuid4()),
        sequence=1,
        type="agent_start",
        data={},
        run_id=None,
        created_at=datetime(2026, 8, 4, tzinfo=UTC),
    )
    assert decode_event(encode_event(event)) == event


def test_decode_envelope_discriminates_by_key_set() -> None:
    request = RpcRequest(request_id=str(uuid4()), method="initialize", params={})
    response = RpcResponse(request_id=str(uuid4()), ok=True, result={})
    event = RpcEvent(
        event_id=str(uuid4()),
        sequence=1,
        type="agent_start",
        data={},
        run_id=None,
        created_at=datetime(2026, 8, 4, tzinfo=UTC),
    )
    lines = [encode_request(request), encode_response(response), encode_event(event)]
    decoded = [decode_envelope(line) for line in lines]
    assert decoded[0] == request
    assert decoded[1] == response
    assert decoded[2] == event
    assert isinstance(decoded[0], RpcRequest)
    assert isinstance(decoded[1], RpcResponse)
    assert isinstance(decoded[2], RpcEvent)
    # The union shape keeps type narrowing useful for the connection loop.
    assert decoded[0] is not None and decoded[1] is not None and decoded[2] is not None
    _narrowed: RpcEnvelope = decoded[0]  # type check only


def test_encoded_lines_are_compact_single_line_json() -> None:
    request = RpcRequest(request_id=str(uuid4()), method="run.start", params={})
    line = encode_request(request)
    assert "\n" not in line
    assert line.startswith('{"request_id":"')
    # No whitespace after separators (compact separators).
    assert ", " not in line
    assert ": " not in line


def test_unicode_survives_roundtrip_without_escaping() -> None:
    request = RpcRequest(
        request_id=str(uuid4()),
        method="initialize",
        params={"note": "你好，EvoPi"},
    )
    line = encode_request(request)
    assert "你好" in line
    assert decode_request(line) == request
