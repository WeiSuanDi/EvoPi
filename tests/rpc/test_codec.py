"""Adversarial tests for the strict RPC v1 JSON codec and event-data conversion."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import Enum
from pathlib import Path

import pytest

from evopi.rpc import (
    RpcCodecError,
    RpcEventDataError,
    decode_event,
    decode_request,
    decode_response,
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
    payload = {
        "request_id": _ID,
        "ok": True,
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
            decode_response('{"request_id":"a","ok":true,"result":null,"schema_version":1,"x":Infinity}')
        with pytest.raises(RpcCodecError):
            decode_response('{"request_id":"a","ok":true,"result":null,"schema_version":1,"x":-Infinity}')

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
