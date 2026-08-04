"""Failing-first tests for the frozen Confirmation v2 types and strict codecs."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from evopi.core.tool import ToolArgumentError, ToolCall
from evopi.harness.confirmation import (
    ConfirmationBatchResponse,
    ConfirmationFormatError,
    ConfirmationRecord,
    ConfirmationRequest,
    ConfirmationResponse,
    ConfirmationSettings,
    ConfirmationStatus,
    ConfirmationTransition,
)
from evopi.harness.confirmation_codec import (
    decode_record,
    decode_request,
    decode_response,
    decode_transition,
    encode_record,
    encode_request,
    encode_response,
    encode_transition,
)


def _request(**overrides: object) -> ConfirmationRequest:
    base: dict[str, object] = dict(
        hook="before_tool_call",
        reason="echo requires approval",
        risk_level="high",
        policy_names=("shell_confirmation",),
        tool_call=ToolCall(
            id="call-1",
            name="echo",
            arguments={"value": "hello"},
            argument_error=ToolArgumentError(
                code="bad_type", message="must be a string", raw_fragment='"hello"'
            ),
        ),
        arguments={"value": "hello"},
        id="a" * 32,
        created_at=datetime(2026, 8, 4, 10, 0, 0, tzinfo=UTC),
        metadata={"workspace": "demo", "count": 3, "nested": {"ok": True}},
        run_id="run-1",
        session_id="session-1",
        expires_at=datetime(2026, 8, 4, 10, 5, 0, tzinfo=UTC),
    )
    base.update(overrides)
    return ConfirmationRequest(
        hook=base["hook"],  # type: ignore[arg-type]
        reason=base["reason"],  # type: ignore[arg-type]
        risk_level=base["risk_level"],  # type: ignore[arg-type]
        policy_names=base["policy_names"],  # type: ignore[arg-type]
        tool_call=base["tool_call"],  # type: ignore[arg-type]
        arguments=base["arguments"],  # type: ignore[arg-type]
        id=base["id"],  # type: ignore[arg-type]
        created_at=base["created_at"],  # type: ignore[arg-type]
        metadata=base["metadata"],  # type: ignore[arg-type]
        run_id=base["run_id"],  # type: ignore[arg-type]
        session_id=base["session_id"],  # type: ignore[arg-type]
        expires_at=base["expires_at"],  # type: ignore[arg-type]
    )


def _response(**overrides: object) -> ConfirmationResponse:
    base: dict[str, object] = dict(
        request_id="a" * 32,
        decision="approve",
        reason="looks safe",
        metadata={"source": "terminal"},
    )
    base.update(overrides)
    return ConfirmationResponse(
        request_id=base["request_id"],  # type: ignore[arg-type]
        decision=base["decision"],  # type: ignore[arg-type]
        reason=base["reason"],  # type: ignore[arg-type]
        metadata=base["metadata"],  # type: ignore[arg-type]
    )


def _record(**overrides: object) -> ConfirmationRecord:
    base: dict[str, object] = dict(
        request=_request(),
        status="pending",
        runtime_id="runtime-1",
        revision=2,
        response=None,
        updated_at=datetime(2026, 8, 4, 10, 1, 0, tzinfo=UTC),
    )
    base.update(overrides)
    return ConfirmationRecord(
        request=base["request"],  # type: ignore[arg-type]
        status=base["status"],  # type: ignore[arg-type]
        runtime_id=base["runtime_id"],  # type: ignore[arg-type]
        revision=base["revision"],  # type: ignore[arg-type]
        response=base["response"],  # type: ignore[arg-type]
        updated_at=base["updated_at"],  # type: ignore[arg-type]
    )


def _transition(**overrides: object) -> ConfirmationTransition:
    base: dict[str, object] = dict(
        request_id="a" * 32,
        expected_revision=1,
        status="approved",
        response=_response(),
    )
    base.update(overrides)
    return ConfirmationTransition(
        request_id=base["request_id"],  # type: ignore[arg-type]
        expected_revision=base["expected_revision"],  # type: ignore[arg-type]
        status=base["status"],  # type: ignore[arg-type]
        response=base["response"],  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# Types: status literal and settings
# ---------------------------------------------------------------------------


def test_confirmation_status_is_the_frozen_literal() -> None:
    statuses: tuple[ConfirmationStatus, ...] = (
        "pending",
        "approved",
        "denied",
        "cancelled",
        "expired",
        "orphaned",
    )
    assert statuses == (
        "pending",
        "approved",
        "denied",
        "cancelled",
        "expired",
        "orphaned",
    )


def test_confirmation_settings_defaults_to_no_timeout() -> None:
    settings = ConfirmationSettings()
    assert settings.timeout_seconds is None
    assert ConfirmationSettings(timeout_seconds=5.0).timeout_seconds == 5.0


def test_batch_response_holds_an_explicit_tuple() -> None:
    batch = ConfirmationBatchResponse(
        responses=(_response(), _response(request_id="b" * 32, decision="deny"))
    )
    assert len(batch.responses) == 2
    assert batch.responses[1].approved is False


# ---------------------------------------------------------------------------
# Round trips
# ---------------------------------------------------------------------------


def test_request_round_trip_preserves_all_v2_fields() -> None:
    original = _request()
    restored = decode_request(encode_request(original))

    assert restored.id == original.id
    assert restored.hook == original.hook
    assert restored.reason == original.reason
    assert restored.risk_level == original.risk_level
    assert restored.policy_names == original.policy_names
    assert restored.created_at == original.created_at
    assert restored.created_at.tzinfo is UTC
    assert restored.metadata == original.metadata
    assert restored.run_id == "run-1"
    assert restored.session_id == "session-1"
    assert restored.expires_at == original.expires_at
    assert restored.tool_call is not None
    assert restored.tool_call.id == "call-1"
    assert restored.tool_call.name == "echo"
    assert restored.tool_call.arguments == {"value": "hello"}
    assert restored.tool_call.argument_error is not None
    assert restored.tool_call.argument_error.code == "bad_type"
    assert restored.tool_call.argument_error.raw_fragment == '"hello"'


def test_request_without_tool_call_round_trips() -> None:
    original = _request(tool_call=None, arguments=None)
    restored = decode_request(encode_request(original))
    assert restored.tool_call is None
    assert restored.arguments is None


def test_response_round_trip() -> None:
    original = _response(metadata={"automatic": True, "expired": True})
    restored = decode_response(encode_response(original))
    assert restored.request_id == original.request_id
    assert restored.decision == "approve"
    assert restored.reason == "looks safe"
    assert restored.metadata == {"automatic": True, "expired": True}


def test_record_round_trip() -> None:
    original = _record(status="approved", response=_response())
    restored = decode_record(encode_record(original))
    assert restored.request.id == original.request.id
    assert restored.status == "approved"
    assert restored.runtime_id == "runtime-1"
    assert restored.revision == 2
    assert restored.updated_at == original.updated_at
    assert restored.response is not None
    assert restored.response.decision == "approve"


def test_transition_round_trip() -> None:
    original = _transition()
    restored = decode_transition(encode_transition(original))
    assert restored.request_id == original.request_id
    assert restored.expected_revision == 1
    assert restored.status == "approved"
    assert restored.response is not None
    assert restored.response.request_id == original.request_id


# ---------------------------------------------------------------------------
# Old constructor compatibility
# ---------------------------------------------------------------------------


def test_old_request_constructor_behavior_is_preserved() -> None:
    request = ConfirmationRequest(hook="before_model_call", reason="legacy")
    assert request.id
    assert request.created_at.tzinfo is UTC
    assert request.run_id is None
    assert request.session_id is None
    assert request.expires_at is None
    assert request.metadata == {}
    assert request.policy_names == ()


# ---------------------------------------------------------------------------
# Strict rejection: structure
# ---------------------------------------------------------------------------


def test_unknown_top_level_field_is_rejected() -> None:
    payload = encode_request(_request())
    payload["surprise"] = True
    with pytest.raises(ConfirmationFormatError, match="unknown field"):
        decode_request(payload)


def test_missing_field_is_rejected() -> None:
    payload = encode_request(_request())
    del payload["reason"]
    with pytest.raises(ConfirmationFormatError, match="missing field"):
        decode_request(payload)


def test_boolean_as_integer_is_rejected() -> None:
    record = _record(revision=True)
    with pytest.raises(ConfirmationFormatError, match="must be an integer"):
        encode_record(record)

    payload = encode_record(_record())
    payload["revision"] = True
    with pytest.raises(ConfirmationFormatError, match="must be an integer"):
        decode_record(payload)


def test_unknown_schema_version_is_rejected() -> None:
    payload = encode_record(_record())
    payload["schema_version"] = 2
    with pytest.raises(ConfirmationFormatError, match="unsupported schema version"):
        decode_record(payload)


def test_missing_schema_version_is_rejected() -> None:
    payload = encode_request(_request())
    del payload["schema_version"]
    with pytest.raises(ConfirmationFormatError, match="missing field"):
        decode_request(payload)


def test_naive_datetime_is_rejected() -> None:
    payload = encode_request(_request())
    payload["created_at"] = "2026-08-04T10:00:00"
    with pytest.raises(ConfirmationFormatError, match="timezone offset"):
        decode_request(payload)


def test_malformed_datetime_is_rejected() -> None:
    payload = encode_request(_request())
    payload["created_at"] = "not-a-date"
    with pytest.raises(ConfirmationFormatError, match="ISO-8601"):
        decode_request(payload)


# ---------------------------------------------------------------------------
# Strict rejection: ids and enums
# ---------------------------------------------------------------------------


def test_non_string_id_is_rejected() -> None:
    payload = encode_request(_request())
    payload["id"] = 123
    with pytest.raises(ConfirmationFormatError, match="non-empty string id"):
        decode_request(payload)


def test_empty_id_is_rejected() -> None:
    payload = encode_request(_request())
    payload["id"] = ""
    with pytest.raises(ConfirmationFormatError, match="non-empty string id"):
        decode_request(payload)


def test_unknown_status_value_is_rejected() -> None:
    payload = encode_record(_record())
    payload["status"] = "weird"
    with pytest.raises(ConfirmationFormatError, match="unsupported value"):
        decode_record(payload)

    with pytest.raises(ConfirmationFormatError, match="unsupported value"):
        encode_record(_record(status="weird"))  # type: ignore[arg-type]


def test_unknown_decision_value_is_rejected() -> None:
    payload = encode_response(_response())
    payload["decision"] = "maybe"
    with pytest.raises(ConfirmationFormatError, match="unsupported value"):
        decode_response(payload)


def test_unknown_risk_level_is_rejected() -> None:
    payload = encode_request(_request())
    payload["risk_level"] = "extreme"
    with pytest.raises(ConfirmationFormatError, match="unsupported value"):
        decode_request(payload)


def test_unknown_hook_is_rejected() -> None:
    payload = encode_request(_request())
    payload["hook"] = "before_confirmation"
    with pytest.raises(ConfirmationFormatError, match="unsupported value"):
        decode_request(payload)


# ---------------------------------------------------------------------------
# Strict rejection: ToolCall structure and nested JSON
# ---------------------------------------------------------------------------


def test_unknown_tool_call_field_is_rejected() -> None:
    payload = encode_request(_request())
    call = payload["tool_call"]
    assert isinstance(call, dict)
    call["extra"] = 1
    with pytest.raises(ConfirmationFormatError, match="unknown field"):
        decode_request(payload)


def test_malformed_tool_call_type_is_rejected() -> None:
    payload = encode_request(_request())
    payload["tool_call"] = {"id": 1, "name": "echo", "arguments": {}, "argument_error": None}
    with pytest.raises(ConfirmationFormatError, match="non-empty string id"):
        decode_request(payload)


def test_non_json_metadata_is_rejected_without_repr_fallback() -> None:
    with pytest.raises(ConfirmationFormatError, match="datetime"):
        encode_request(_request(metadata={"when": datetime.now(UTC)}))


def test_non_json_nested_argument_is_rejected() -> None:
    with pytest.raises(ConfirmationFormatError, match="object"):
        encode_request(_request(arguments={"bad": object()}))


def test_non_finite_float_is_rejected() -> None:
    with pytest.raises(ConfirmationFormatError, match="non-finite"):
        encode_request(_request(metadata={"score": float("nan")}))


def test_non_string_metadata_key_is_rejected() -> None:
    with pytest.raises(ConfirmationFormatError, match="non-string key"):
        encode_request(_request(metadata={1: "one"}))


def test_boolean_metadata_value_round_trips() -> None:
    original = _request(metadata={"flag": True})
    restored = decode_request(encode_request(original))
    assert restored.metadata == {"flag": True}


def test_expired_timestamp_is_preserved() -> None:
    expires = datetime.now(UTC) + timedelta(seconds=30)
    original = _request(expires_at=expires)
    restored = decode_request(encode_request(original))
    assert restored.expires_at == expires
