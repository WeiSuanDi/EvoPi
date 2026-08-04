"""Direct contract tests for the known-good reference adapters (HIF-3 rev 2).

These are adversarial probes of the reference itself, covering every codec
invariant from CONTEXT.md revision 2, encoder/decoder round-trip symmetry, and
the Confirmation close/crash boundary.  They prove the reference is a
conformant known-good adapter that the reusable scenarios then use as oracle.
"""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta
from enum import Enum
from uuid import UUID

import pytest

from .conformance import (
    KIT_EVENT_UUID,
    RpcRequest,
    RpcResponse,
    WireError,
    make_confirmation_request,
    make_event,
    to_json_safe,
)
from .reference import ReferenceConfirmationAdapter, ReferenceRpcAdapter

UUID_LINE = (
    '{"event_id":"' + KIT_EVENT_UUID + '","sequence":1,"type":"t","data":{},'
    '"run_id":null,"created_at":"2026-01-01T00:00:00Z","schema_version":1}'
)


def _assert_wire_error(result: object, expected: str) -> None:
    assert isinstance(result, WireError), f"expected WireError, got {result!r}"
    assert result.code == expected, f"expected {expected!r}, got {result.code!r}"


def _drop_key(line: str, key: str) -> str:
    obj = json.loads(line)
    del obj[key]
    return json.dumps(obj)


def test_codec_rejects_duplicate_json_keys() -> None:
    adapter = ReferenceRpcAdapter()
    result = asyncio.run(
        adapter.send_wire(
            '{"request_id":"d","method":"runtime.status","params":{},"schema_version":1,'
            '"request_id":"x"}'
        )
    )
    assert not result.ok
    assert result.error is not None and result.error.code == "duplicate_json_key"
    _assert_wire_error(
        adapter.parse_wire_event(UUID_LINE.replace('"sequence":1', '"sequence":1,"sequence":9')),
        "duplicate_json_key",
    )
    _assert_wire_error(
        adapter.parse_wire_response(
            '{"request_id":"w","ok":true,"result":{},"error":null,"schema_version":1,'
            '"request_id":"w"}'
        ),
        "duplicate_json_key",
    )


def test_codec_rejects_non_finite_numbers() -> None:
    adapter = ReferenceRpcAdapter()
    for line in (
        '{"request_id":"n","method":"runtime.status","params":{"x":NaN},"schema_version":1}',
        '{"request_id":"n","method":"runtime.status","params":{"x":Infinity},"schema_version":1}',
    ):
        result = asyncio.run(adapter.send_wire(line))
        assert not result.ok
        assert result.error is not None and result.error.code == "non_finite_number"
    _assert_wire_error(
        adapter.parse_wire_event(UUID_LINE.replace('"data":{}', '"data":{"x":NaN}')),
        "non_finite_number",
    )
    _assert_wire_error(adapter.event_wire(make_event(data={"x": math.nan})), "non_finite_number")


def test_codec_rejects_missing_and_extra_envelope_keys() -> None:
    adapter = ReferenceRpcAdapter()
    missing = asyncio.run(
        adapter.send_wire('{"method":"runtime.status","params":{},"schema_version":1}')
    )
    assert not missing.ok and missing.error is not None and missing.error.code == "invalid_envelope"
    extra = asyncio.run(
        adapter.send_wire(
            '{"request_id":"x","method":"runtime.status","params":{},"schema_version":1,"z":1}'
        )
    )
    assert not extra.ok and extra.error is not None and extra.error.code == "invalid_envelope_key"
    _assert_wire_error(
        adapter.parse_wire_response(
            '{"ok":true,"result":{},"error":null,"schema_version":1}'
        ),
        "invalid_envelope",
    )
    _assert_wire_error(
        adapter.parse_wire_response(
            '{"request_id":"w","ok":true,"result":{},"error":null,"schema_version":1,"z":1}'
        ),
        "invalid_envelope_key",
    )


def test_codec_rejects_booleans_as_integers() -> None:
    adapter = ReferenceRpcAdapter()
    result = asyncio.run(
        adapter.send_wire(
            '{"request_id":"b","method":"events.replay","params":{"after_sequence":true},'
            '"schema_version":1}'
        )
    )
    assert not result.ok and result.error is not None and result.error.code == "invalid_params"
    _assert_wire_error(
        adapter.parse_wire_event(UUID_LINE.replace('"sequence":1', '"sequence":true')),
        "invalid_event",
    )


def test_codec_rejects_empty_identifiers() -> None:
    adapter = ReferenceRpcAdapter()
    empty_id = asyncio.run(
        adapter.send_wire('{"request_id":"","method":"runtime.status","params":{},"schema_version":1}')
    )
    assert not empty_id.ok and empty_id.error is not None and empty_id.error.code == "invalid_envelope"
    empty_method = asyncio.run(
        adapter.send_wire('{"request_id":"x","method":"","params":{},"schema_version":1}')
    )
    assert (
        not empty_method.ok
        and empty_method.error is not None
        and empty_method.error.code == "invalid_envelope"
    )
    _assert_wire_error(
        adapter.parse_wire_event(UUID_LINE.replace(KIT_EVENT_UUID, "")),
        "invalid_event",
    )
    _assert_wire_error(
        adapter.parse_wire_event(UUID_LINE.replace('"type":"t"', '"type":""')),
        "invalid_event",
    )
    _assert_wire_error(
        adapter.parse_wire_response(
            '{"request_id":"","ok":true,"result":{},"error":null,"schema_version":1}'
        ),
        "invalid_response",
    )


def test_codec_requires_every_request_and_event_key() -> None:
    """Request and Event require the exact frozen key set, one key at a time."""
    adapter = ReferenceRpcAdapter()
    request_line = '{"request_id":"x","method":"runtime.status","params":{},"schema_version":1}'
    for key in ("request_id", "method", "params", "schema_version"):
        result = asyncio.run(adapter.send_wire(_drop_key(request_line, key)))
        assert not result.ok
        assert result.error is not None and result.error.code == "invalid_envelope", (
            f"request missing {key!r} must be invalid_envelope"
        )
    for key in ("event_id", "sequence", "type", "data", "run_id", "created_at", "schema_version"):
        _assert_wire_error(adapter.parse_wire_event(_drop_key(UUID_LINE, key)), "invalid_envelope")
    # mixed missing and extra keys
    mixed_request = json.loads(_drop_key(request_line, "schema_version"))
    mixed_request["extra"] = 1
    result = asyncio.run(adapter.send_wire(json.dumps(mixed_request)))
    assert not result.ok
    assert result.error is not None and result.error.code == "invalid_envelope_key"
    mixed_event = json.loads(_drop_key(UUID_LINE, "run_id"))
    mixed_event["extra"] = 1
    _assert_wire_error(adapter.parse_wire_event(json.dumps(mixed_event)), "invalid_envelope_key")


def test_codec_rejects_negative_after_sequence() -> None:
    adapter = ReferenceRpcAdapter()
    for line in (
        '{"request_id":"n","method":"events.replay","params":{"after_sequence":-1},"schema_version":1}',
        '{"request_id":"n","method":"events.replay","params":{"after_sequence":-5},"schema_version":1}',
    ):
        result = asyncio.run(adapter.send_wire(line))
        assert not result.ok
        assert result.error is not None and result.error.code == "invalid_params"


def test_response_wire_rejects_null_or_non_object_error_details() -> None:
    adapter = ReferenceRpcAdapter()
    for line in (
        '{"request_id":"w","ok":false,"result":null,"error":{"code":"c","message":"m",'
        '"details":null},"schema_version":1}',
        '{"request_id":"w","ok":false,"result":null,"error":{"code":"c","message":"m",'
        '"details":[]},"schema_version":1}',
    ):
        _assert_wire_error(adapter.parse_wire_response(line), "invalid_response")
    fail_response = _call(adapter, "no.such.method", {})
    wire = adapter.response_wire(fail_response)
    assert isinstance(wire, str)
    payload = json.loads(wire)
    assert isinstance(payload["error"]["details"], dict)
    assert payload["error"]["message"]  # non-empty safe message


def test_to_json_safe_accepts_generic_mappings_sequences_and_dates() -> None:
    class _Map(Mapping[str, int]):
        def __init__(self, data: dict[str, int]) -> None:
            self._data = data

        def __getitem__(self, key: str) -> int:
            return self._data[key]

        def __iter__(self):
            return iter(self._data)

        def __len__(self) -> int:
            return len(self._data)

    class _Seq(Sequence[int]):
        def __init__(self, data: list[int]) -> None:
            self._data = data

        def __getitem__(self, index):
            return self._data[index]

        def __len__(self) -> int:
            return len(self._data)

    assert to_json_safe(_Map({"a": 1, "b": {"c": 2}})) == {"a": 1, "b": {"c": 2}}
    assert to_json_safe(_Seq([1, (2, 3)])) == [1, [2, 3]]
    assert to_json_safe(range(3)) == [0, 1, 2]
    assert to_json_safe(date(2026, 1, 2)) == "2026-01-02"
    assert to_json_safe({"when": date(2026, 1, 2)}) == {"when": "2026-01-02"}
    with pytest.raises(ValueError):
        to_json_safe(b"raw bytes")
    with pytest.raises(ValueError, match="non-string mapping key"):
        to_json_safe({1: "x"})


def test_codec_rejects_unknown_schema_versions() -> None:
    adapter = ReferenceRpcAdapter()
    for line in (
        '{"request_id":"v","method":"runtime.status","params":{},"schema_version":2}',
        '{"request_id":"v","method":"runtime.status","params":{},"schema_version":0}',
    ):
        result = asyncio.run(adapter.send_wire(line))
        assert not result.ok and result.error is not None and result.error.code == "invalid_schema_version"
    _assert_wire_error(
        adapter.parse_wire_event(UUID_LINE.replace('"schema_version":1', '"schema_version":2')),
        "invalid_schema_version",
    )
    _assert_wire_error(
        adapter.parse_wire_response(
            '{"request_id":"w","ok":true,"result":{},"error":null,"schema_version":2}'
        ),
        "invalid_schema_version",
    )


def test_codec_rejects_non_object_payloads() -> None:
    adapter = ReferenceRpcAdapter()
    result = asyncio.run(
        adapter.send_wire('{"request_id":"p","method":"runtime.status","params":5,"schema_version":1}')
    )
    assert not result.ok and result.error is not None and result.error.code == "invalid_params"
    _assert_wire_error(
        adapter.parse_wire_event(UUID_LINE.replace('"data":{}', '"data":[]')),
        "invalid_data",
    )
    _assert_wire_error(
        adapter.parse_wire_response(
            '{"request_id":"w","ok":true,"result":5,"error":null,"schema_version":1}'
        ),
        "invalid_response",
    )


def test_codec_rejects_invalid_event_uuids() -> None:
    adapter = ReferenceRpcAdapter()
    _assert_wire_error(
        adapter.parse_wire_event(UUID_LINE.replace(KIT_EVENT_UUID, "not-a-uuid")),
        "invalid_event",
    )
    _assert_wire_error(adapter.event_wire(make_event(event_id="not-a-uuid")), "invalid_event")
    # hex-without-dashes is not the canonical UUID string either
    _assert_wire_error(
        adapter.parse_wire_event(UUID_LINE.replace(KIT_EVENT_UUID, KIT_EVENT_UUID.replace("-", ""))),
        "invalid_event",
    )


def test_codec_rejects_non_positive_sequences() -> None:
    adapter = ReferenceRpcAdapter()
    for sequence in (0, -1):
        _assert_wire_error(
            adapter.parse_wire_event(
                UUID_LINE.replace('"sequence":1', f'"sequence":{sequence}')
            ),
            "invalid_event",
        )
    _assert_wire_error(adapter.event_wire(make_event(sequence=0)), "invalid_event")


def test_codec_rejects_empty_event_types() -> None:
    adapter = ReferenceRpcAdapter()
    _assert_wire_error(
        adapter.parse_wire_event(UUID_LINE.replace('"type":"t"', '"type":""')),
        "invalid_event",
    )
    _assert_wire_error(adapter.event_wire(make_event(type_="")), "invalid_event")


def test_codec_rejects_non_utc_timestamps() -> None:
    adapter = ReferenceRpcAdapter()
    _assert_wire_error(
        adapter.parse_wire_event(UUID_LINE.replace('"2026-01-01T00:00:00Z"', '"2026-01-01T00:00:00"')),
        "invalid_timestamp",
    )
    _assert_wire_error(
        adapter.parse_wire_event(UUID_LINE.replace('"2026-01-01T00:00:00Z"', '"2026-01-01T05:00:00+05:00"')),
        "invalid_timestamp",
    )
    _assert_wire_error(adapter.event_wire(make_event(created_at=datetime(2026, 1, 1))), "invalid_timestamp")


def test_to_json_safe_rejects_non_string_mapping_keys() -> None:
    with pytest.raises(ValueError, match="non-string mapping key"):
        to_json_safe({1: "x"})
    with pytest.raises(ValueError, match="non-string mapping key"):
        to_json_safe({"ok": {"nested": {b"raw": 1}}})


def test_to_json_safe_rejects_non_finite_floats() -> None:
    with pytest.raises(ValueError, match="non-finite"):
        to_json_safe(math.nan)
    with pytest.raises(ValueError, match="non-finite"):
        to_json_safe(math.inf)
    with pytest.raises(ValueError, match="non-finite"):
        to_json_safe({"nested": -math.inf})


def test_to_json_safe_validates_enum_values_without_fallback() -> None:
    class Safe(Enum):
        ALPHA = "alpha"
        ONE = 1

    class Unsafe(Enum):
        OBJECT = object()

    class NonFinite(Enum):
        NAN = float("nan")

    assert to_json_safe(Safe.ALPHA) == "alpha"
    assert to_json_safe(Safe.ONE) == 1
    with pytest.raises(ValueError, match="not JSON-safe"):
        to_json_safe(Unsafe.OBJECT)
    with pytest.raises(ValueError, match="non-finite"):
        to_json_safe(NonFinite.NAN)


def test_event_wire_round_trip_symmetry() -> None:
    adapter = ReferenceRpcAdapter()
    event = make_event(sequence=3, type_="tool_execution_end", data={"n": 2, "ok": True})
    wire = adapter.event_wire(event)
    assert isinstance(wire, str)
    decoded = adapter.parse_wire_event(wire)
    assert decoded == event
    assert adapter.event_wire(decoded) == wire  # canonical form is stable


def _call(adapter: ReferenceRpcAdapter, method: str, params: dict) -> RpcResponse:
    return asyncio.run(adapter.call(RpcRequest(request_id="rt-1", method=method, params=params)))


def test_response_wire_round_trip_symmetry() -> None:
    adapter = ReferenceRpcAdapter()
    ok_response = _call(adapter, "initialize", {})
    assert ok_response.ok
    wire = adapter.response_wire(ok_response)
    assert isinstance(wire, str)
    decoded = adapter.parse_wire_response(wire)
    assert decoded == ok_response
    assert adapter.response_wire(decoded) == wire
    fail_response = _call(adapter, "no.such.method", {})
    assert not fail_response.ok
    wire = adapter.response_wire(fail_response)
    assert isinstance(wire, str)
    decoded = adapter.parse_wire_response(wire)
    assert decoded == fail_response
    assert adapter.response_wire(decoded) == wire


def test_response_wire_canonical_invariant() -> None:
    adapter = ReferenceRpcAdapter()
    ok_response = _call(adapter, "initialize", {})
    payload = json.loads(adapter.response_wire(ok_response))
    assert set(payload) == {"request_id", "ok", "result", "error", "schema_version"}
    assert payload["ok"] is True and isinstance(payload["result"], dict) and payload["error"] is None
    fail_response = _call(adapter, "no.such.method", {})
    payload = json.loads(adapter.response_wire(fail_response))
    assert payload["ok"] is False and payload["result"] is None
    assert set(payload["error"]) == {"code", "message", "details"}
    assert payload["error"]["code"] and payload["error"]["message"]


def test_graceful_close_wakes_waiters_and_leaves_no_pending() -> None:
    async def probe() -> None:
        adapter = ReferenceConfirmationAdapter()
        tasks = [
            asyncio.create_task(adapter.request(make_confirmation_request(request_id="g-1"))),
            asyncio.create_task(adapter.request(make_confirmation_request(request_id="g-2"))),
        ]
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await adapter.close()
        outcomes = [await task for task in tasks]
        assert all(outcome.status == "cancelled" for outcome in outcomes)
        assert all(not outcome.executed for outcome in outcomes)
        assert all(task.done() for task in tasks)  # no leaked task
        assert adapter.pending() == ()
        assert all(adapter.record(f"g-{i}").status != "pending" for i in (1, 2))
        assert adapter.execution_log() == ()

    asyncio.run(probe())


def test_crash_recovery_orphans_even_with_reused_runtime_id() -> None:
    async def probe() -> None:
        adapter = ReferenceConfirmationAdapter()
        request = make_confirmation_request(request_id="crash-1")
        task = asyncio.create_task(adapter.request(request))
        await asyncio.sleep(0)
        runtime_id = adapter.pending()[0].runtime_id
        await adapter.crash()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        recovered = await adapter.recover(runtime_id=runtime_id)
        assert len(recovered.orphaned) == 1
        assert recovered.orphaned[0].status == "orphaned"
        assert adapter.pending() == ()
        assert adapter.record("crash-1").status == "orphaned"
        assert adapter.execution_log() == ()

    asyncio.run(probe())


def test_publish_assigns_uuid_event_ids() -> None:
    async def probe() -> None:
        adapter = ReferenceRpcAdapter()
        event = await adapter.publish("type", {"n": 1})
        assert str(UUID(event.event_id)) == event.event_id
        assert event.sequence == 1
        assert event.created_at.utcoffset() == timedelta(0)

    asyncio.run(probe())
