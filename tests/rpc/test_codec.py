"""Adversarial tests for the strict RPC v1 JSON codec and event-data conversion."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone
from enum import Enum
from pathlib import Path

import pytest

from evopi.rpc import (
    RpcCodecError,
    RpcErrorInfo,
    RpcEvent,
    RpcEventDataError,
    RpcRequest,
    RpcResponse,
    decode_envelope,
    decode_event,
    decode_request,
    decode_response,
    encode_event,
    encode_request,
    encode_response,
    to_event_data,
)

_ID = "11111111-2222-4333-8444-555555555555"
_TS = "2026-08-04T10:00:00.123456+00:00"


@dataclass(slots=True)
class _Nested:
    count: int = 0
    label: str = "x"


@dataclass(slots=True)
class _Item:
    name: str
    nested: _Nested


def _request_line(**overrides: object) -> str:
    payload = {
        "request_id": _ID,
        "method": "initialize",
        "params": {},
        "schema_version": 1,
    }
    payload.update(overrides)
    return json.dumps(payload, separators=(",", ":"))


def _response_line(**overrides: object) -> str:
    ok = overrides.get("ok", True)
    payload = {
        "request_id": _ID,
        "ok": ok,
        "result": overrides.get("result", {"n": 1} if ok else None),
        "error": overrides.get("error", None),
        "schema_version": 1,
    }
    payload.update(overrides)
    return json.dumps(payload, separators=(",", ":"))


def _event_line(**overrides: object) -> str:
    payload = {
        "event_id": _ID,
        "sequence": 1,
        "type": "agent_start",
        "data": {},
        "run_id": None,
        "created_at": _TS,
        "schema_version": 1,
    }
    payload.update(overrides)
    return json.dumps(payload, separators=(",", ":"))


class TestStrictDecoding:
    def test_duplicate_keys_rejected(self) -> None:
        with pytest.raises(RpcCodecError):
            decode_request('{"request_id":"a","request_id":"b","method":"x","params":{},"schema_version":1}')

    def test_nan_and_infinity_rejected(self) -> None:
        with pytest.raises(RpcCodecError):
            decode_request('{"request_id":"a","method":"x","params":{"n":NaN},"schema_version":1}')
        with pytest.raises(RpcCodecError):
            decode_response(
                '{"request_id":"a","ok":true,"result":null,"error":null,"schema_version":1,"x":Infinity}'
            )
        with pytest.raises(RpcCodecError):
            decode_response(
                '{"request_id":"a","ok":true,"result":null,"error":null,"schema_version":1,"x":-Infinity}'
            )

    def test_booleans_used_as_integers_rejected(self) -> None:
        with pytest.raises(RpcCodecError):
            decode_event(_event_line(sequence=True))
        with pytest.raises(RpcCodecError):
            decode_request(_request_line(schema_version=True))

    def test_malformed_timestamps_rejected(self) -> None:
        with pytest.raises(RpcCodecError):
            decode_event(_event_line(created_at="not-a-date"))
        with pytest.raises(RpcCodecError):
            decode_event(_event_line(created_at="2026-08-04T10:00:00"))  # naive, no zone
        with pytest.raises(RpcCodecError):
            decode_event(_event_line(created_at="2026-08-04T10:00:00+01:00"))  # non-UTC

    def test_invalid_uuid_rejected(self) -> None:
        with pytest.raises(RpcCodecError):
            decode_event(_event_line(event_id="abc"))
        with pytest.raises(RpcCodecError):
            decode_event(_event_line(event_id="11111111-2222-4333-8444-55555555555"))  # too short

    def test_unknown_schema_version_rejected(self) -> None:
        with pytest.raises(RpcCodecError):
            decode_request(_request_line(schema_version=2))
        with pytest.raises(RpcCodecError):
            decode_event(_event_line(schema_version=0))

    def test_extra_fields_rejected(self) -> None:
        with pytest.raises(RpcCodecError):
            decode_request(_request_line(extra=1))
        with pytest.raises(RpcCodecError):
            decode_response(_response_line(result={}, extra=1))
        with pytest.raises(RpcCodecError):
            decode_event(_event_line(extra=1))

    def test_missing_required_fields_rejected(self) -> None:
        with pytest.raises(RpcCodecError):
            decode_request(_request_line(method=None))
        with pytest.raises(RpcCodecError):
            decode_request('{"request_id":"a","method":"x","params":{}}')  # no schema_version
        with pytest.raises(RpcCodecError):
            decode_response('{"request_id":"a","schema_version":1}')  # no ok
        with pytest.raises(RpcCodecError):
            decode_response('{"request_id":"a","ok":true,"result":null,"schema_version":1}')  # no error
        with pytest.raises(RpcCodecError):
            decode_response('{"request_id":"a","ok":true,"error":null,"schema_version":1}')  # no result

    def test_response_invariant_contradictions_rejected(self) -> None:
        # ok=true requires an object result and error=null.
        with pytest.raises(RpcCodecError):
            decode_response(_response_line(ok=True, result=None))  # success without result
        with pytest.raises(RpcCodecError):
            decode_response(_response_line(ok=True, error={"code": "x", "message": "m", "details": {}}))
        # ok=false requires result=null and exactly one error object.
        with pytest.raises(RpcCodecError):
            decode_response(_response_line(ok=False, error=None))  # failure without error
        with pytest.raises(RpcCodecError):
            decode_response(_response_line(ok=False, result={"n": 1}, error={"code": "x", "message": "m", "details": {}}))
        with pytest.raises(RpcCodecError):
            decode_response(_response_line(ok=False, result=None, error="not-an-object"))

    def test_empty_strings_rejected(self) -> None:
        with pytest.raises(RpcCodecError):
            decode_request(_request_line(request_id=""))
        with pytest.raises(RpcCodecError):
            decode_request(_request_line(method=""))
        with pytest.raises(RpcCodecError):
            decode_event(_event_line(type=""))
        with pytest.raises(RpcCodecError):
            decode_response(
                _response_line(ok=False, result=None, error={"code": "", "message": "m", "details": {}})
            )

    def test_sequence_zero_rejected(self) -> None:
        with pytest.raises(RpcCodecError):
            decode_event(_event_line(sequence=0))

    def test_non_object_params_and_data_rejected(self) -> None:
        with pytest.raises(RpcCodecError):
            decode_request(_request_line(params=[1, 2]))
        with pytest.raises(RpcCodecError):
            decode_request(_request_line(params="nope"))
        with pytest.raises(RpcCodecError):
            decode_event(_event_line(data=[1]))

    def test_non_object_top_level_rejected(self) -> None:
        with pytest.raises(RpcCodecError):
            decode_request("[1,2,3]")
        with pytest.raises(RpcCodecError):
            decode_request('"just a string"')
        with pytest.raises(RpcCodecError):
            decode_request("42")
        with pytest.raises(RpcCodecError):
            decode_request("null")

    def test_multi_object_and_trailing_input_rejected(self) -> None:
        with pytest.raises(RpcCodecError):
            decode_request('{"request_id":"a","method":"x","params":{},"schema_version":1}{"a":1}')
        with pytest.raises(RpcCodecError):
            decode_request('{"request_id":"a","method":"x","params":{},"schema_version":1} garbage')
        with pytest.raises(RpcCodecError):
            decode_request('{"request_id":"a","method":"x","params":{},"schema_version":1}true')

    def test_error_info_exact_keys_and_types(self) -> None:
        error = {"code": "nope", "message": "safe", "details": {}}
        line = _response_line(ok=False, error=error)
        decoded = decode_response(line)
        assert decoded.ok is False
        assert decoded.error is not None
        assert decoded.error.code == "nope"
        # Extra key inside error info is rejected.
        bad = {"code": "nope", "message": "safe", "details": {}, "secret": 1}
        with pytest.raises(RpcCodecError):
            decode_response(_response_line(ok=False, error=bad))
        # Missing details is rejected.
        with pytest.raises(RpcCodecError):
            decode_response(_response_line(ok=False, error={"code": "nope", "message": "safe"}))
        # Non-object error is rejected.
        with pytest.raises(RpcCodecError):
            decode_response(_response_line(ok=False, error="nope"))

    def test_whitespace_around_line_is_tolerated(self) -> None:
        request = decode_request(_request_line())
        assert decode_request("  " + _request_line() + "  \n") == request

    def test_empty_and_blank_lines_rejected(self) -> None:
        with pytest.raises(RpcCodecError):
            decode_request("")
        with pytest.raises(RpcCodecError):
            decode_request("   ")


class TestEventDataConversion:
    class Color(Enum):
        RED = "red"
        BLUE = "blue"

    def test_primitives_pass_through(self) -> None:
        assert to_event_data(None) is None
        assert to_event_data(True) is True
        assert to_event_data(1) == 1
        assert to_event_data("s") == "s"
        assert to_event_data(1.5) == 1.5

    def test_mapping_requires_string_keys(self) -> None:
        assert to_event_data({"a": 1}) == {"a": 1}
        with pytest.raises(RpcEventDataError):
            to_event_data({1: "a"})

    def test_sequences_convert(self) -> None:
        assert to_event_data([1, "a", None]) == [1, "a", None]
        assert to_event_data((1, 2)) == [1, 2]

    def test_dataclass_enum_path_datetime_convert(self) -> None:
        assert to_event_data(_Item(name="k", nested=_Nested(count=3))) == {
            "name": "k",
            "nested": {"count": 3, "label": "x"},
        }
        assert to_event_data(self.Color.RED) == "red"
        path = Path("a") / "b.txt"
        assert to_event_data(path) == str(path)
        assert to_event_data(datetime(2026, 8, 4, tzinfo=UTC)) == "2026-08-04T00:00:00+00:00"
        assert to_event_data(date(2026, 8, 4)) == "2026-08-04"

    def test_non_finite_floats_rejected(self) -> None:
        with pytest.raises(RpcEventDataError):
            to_event_data(float("nan"))
        with pytest.raises(RpcEventDataError):
            to_event_data(float("inf"))

    def test_unsupported_values_rejected_without_repr(self) -> None:
        for value in [object(), {1, 2}, b"bytes", self, {"a": object()}, [object()]]:
            with pytest.raises(RpcEventDataError) as excinfo:
                to_event_data(value)
            assert "0x" not in str(excinfo.value)  # never leaks repr of the value
            assert str(object()) not in str(excinfo.value)

    def test_enum_value_recurses(self) -> None:
        class EnumWithDict(Enum):
            OPTION = {"key": "value"}

        assert to_event_data(EnumWithDict.OPTION) == {"key": "value"}


class TestEncodeValidation:
    """Crafted invalid dataclass instances must fail before wire output."""

    def _valid_request(self, **overrides: object) -> RpcRequest:
        payload = {"request_id": _ID, "method": "initialize", "params": {}}
        payload.update(overrides)
        return RpcRequest(**payload)  # type: ignore[arg-type]

    def _valid_response(self, **overrides: object) -> RpcResponse:
        payload = {
            "request_id": _ID,
            "ok": True,
            "result": {"n": 1},
            "error": None,
        }
        payload.update(overrides)
        return RpcResponse(**payload)  # type: ignore[arg-type]

    def _valid_event(self, **overrides: object) -> RpcEvent:
        payload = {
            "event_id": _ID,
            "sequence": 1,
            "type": "agent_start",
            "data": {},
            "run_id": None,
            "created_at": datetime(2026, 8, 4, tzinfo=UTC),
        }
        payload.update(overrides)
        return RpcEvent(**payload)  # type: ignore[arg-type]

    def test_invalid_request_instances_fail_at_encode(self) -> None:
        cases = [
            self._valid_request(request_id=""),
            self._valid_request(method=""),
            self._valid_request(params=[]),  # type: ignore[arg-type]
            self._valid_request(schema_version=2),
            self._valid_request(schema_version=True),  # type: ignore[arg-type]
        ]
        for case in cases:
            with pytest.raises(RpcCodecError):
                encode_request(case)

    def test_contradictory_response_instances_fail_at_encode(self) -> None:
        info = RpcErrorInfo(code="boom", message="m", details={})
        cases = [
            self._valid_response(ok=True, result=None),  # success without result
            self._valid_response(ok=True, result={"n": 1}, error=info),  # success with error
            self._valid_response(ok=False, error=None),  # failure without error
            self._valid_response(ok=False, result={"n": 1}),  # failure with result
            self._valid_response(ok=False, result={"n": 1}, error=info),  # both
            self._valid_response(ok="yes"),  # type: ignore[arg-type]  # ok not boolean
            self._valid_response(request_id=""),  # empty request id
            self._valid_response(ok=False, error=RpcErrorInfo(code="", message="m", details={})),
        ]
        for case in cases:
            with pytest.raises(RpcCodecError):
                encode_response(case)

    def test_invalid_event_instances_fail_at_encode(self) -> None:
        cases = [
            self._valid_event(event_id="abc"),
            self._valid_event(event_id=""),
            self._valid_event(sequence=0),
            self._valid_event(sequence=-1),
            self._valid_event(sequence=True),  # type: ignore[arg-type]
            self._valid_event(type=""),
            self._valid_event(data=[]),  # type: ignore[arg-type]
            self._valid_event(run_id=5),  # type: ignore[arg-type]
            self._valid_event(created_at=datetime(2026, 8, 4)),  # naive
            self._valid_event(created_at=datetime(2026, 8, 4, tzinfo=timezone(timedelta(hours=2)))),  # non-UTC
            self._valid_event(schema_version=0),
        ]
        for case in cases:
            with pytest.raises(RpcCodecError):
                encode_event(case)

    def test_non_json_safe_envelopes_fail_at_encode(self) -> None:
        with pytest.raises(RpcCodecError):
            encode_request(self._valid_request(params={"x": object()}))  # type: ignore[arg-type]
        with pytest.raises(RpcCodecError):
            encode_response(self._valid_response(result={"x": float("nan")}))
        with pytest.raises(RpcCodecError):
            encode_event(self._valid_event(data={"x": object()}))  # type: ignore[arg-type]


class TestRoundTripInvariants:
    """Every successfully encoded envelope must decode back identically."""

    def test_all_valid_envelopes_round_trip(self) -> None:
        request = RpcRequest(request_id=_ID, method="initialize", params={"a": 1})
        response_ok = RpcResponse(request_id=_ID, ok=True, result={"n": 1})
        response_err = RpcResponse(
            request_id=_ID,
            ok=False,
            error=RpcErrorInfo(code="nope", message="safe", details={"k": "v"}),
        )
        event = RpcEvent(
            event_id=_ID,
            sequence=1,
            type="agent_start",
            data={"i": 1},
            run_id=None,
            created_at=datetime(2026, 8, 4, 10, 30, tzinfo=UTC),
        )
        event_with_run = RpcEvent(
            event_id=_ID,
            sequence=42,
            type="tool_execution_start",
            data={"name": "work", "nested": {"count": 2}},
            run_id="run-1",
            created_at=datetime(2026, 8, 4, tzinfo=UTC),
        )
        envelopes = [request, response_ok, response_err, event, event_with_run]
        for envelope in envelopes:
            line = _encode_envelope(envelope)
            assert decode_envelope(line) == envelope


def _encode_envelope(envelope: RpcRequest | RpcResponse | RpcEvent) -> str:
    if isinstance(envelope, RpcRequest):
        return encode_request(envelope)
    if isinstance(envelope, RpcResponse):
        return encode_response(envelope)
    return encode_event(envelope)
