"""Exact method contracts for RPC v2."""

from __future__ import annotations

import json
from typing import Any

from evopi.core.types import JsonObject
from evopi.harness.confirmation_codec import decode_record

from .codec_v2 import decode_v2_event
from .errors import RpcCodecError

CONFIRMATION_DECISIONS_V2 = frozenset({"approve", "deny", "cancelled"})

METHOD_HANDLERS_V2: dict[str, str] = {
    "initialize": "initialize",
    "runtime.status": "runtime_status",
    "run.start": "run_start",
    "run.steer": "run_steer",
    "run.follow_up": "run_follow_up",
    "run.abort": "run_abort",
    "confirmation.list": "confirmation_list",
    "confirmation.respond": "confirmation_respond",
    "confirmation.respond_batch": "confirmation_respond_batch",
    "events.replay": "events_replay",
    "shutdown": "shutdown",
}


def _exact(value: Any, keys: frozenset[str], field: str) -> JsonObject:
    if not isinstance(value, dict) or frozenset(value) != keys:
        raise RpcCodecError(f"invalid {field} shape")
    return value


def _str(value: Any, field: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value):
        raise RpcCodecError(f"invalid {field}")
    return value


def _optional_str(value: Any, field: str) -> str | None:
    if value is None:
        return None
    return _str(value, field, empty=True)


def _int(value: Any, field: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise RpcCodecError(f"invalid {field}")
    return value


def _bool(value: Any, field: str) -> bool:
    if type(value) is not bool:
        raise RpcCodecError(f"invalid {field}")
    return value


def _string_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise RpcCodecError(f"invalid {field}")
    return value


def _json_object(value: Any, field: str) -> JsonObject:
    if not isinstance(value, dict):
        raise RpcCodecError(f"invalid {field}")
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise RpcCodecError(f"invalid {field}") from exc
    return value


def validate_v2_params(method: str, params: JsonObject) -> None:
    if method == "initialize":
        value = _exact(params, frozenset({"client_name", "client_version"}), "params")
        _str(value["client_name"], "client_name")
        _str(value["client_version"], "client_version")
        return
    if method in {"runtime.status", "confirmation.list", "shutdown"}:
        _exact(params, frozenset(), "params")
        return
    if method == "run.start":
        value = _exact(params, frozenset({"prompt"}), "params")
        _str(value["prompt"], "prompt")
        return
    if method in {"run.steer", "run.follow_up"}:
        value = _exact(params, frozenset({"run_id", "content"}), "params")
        _str(value["run_id"], "run_id")
        _str(value["content"], "content")
        return
    if method == "run.abort":
        value = _exact(params, frozenset({"run_id"}), "params")
        _str(value["run_id"], "run_id")
        return
    if method == "confirmation.respond":
        _validate_confirmation_answer(params)
        return
    if method == "confirmation.respond_batch":
        value = _exact(params, frozenset({"responses"}), "params")
        responses = value["responses"]
        if not isinstance(responses, list):
            raise RpcCodecError("invalid responses")
        for response in responses:
            _validate_confirmation_answer(response)
        return
    if method == "events.replay":
        value = _exact(params, frozenset({"stream_id", "after_sequence"}), "params")
        _str(value["stream_id"], "stream_id")
        _int(value["after_sequence"], "after_sequence")
        return
    raise RpcCodecError("unknown method")


def _validate_confirmation_answer(value: Any) -> None:
    answer = _exact(
        value,
        frozenset({"request_id", "expected_revision", "decision", "reason", "metadata"}),
        "confirmation answer",
    )
    _str(answer["request_id"], "request_id")
    _int(answer["expected_revision"], "expected_revision", minimum=1)
    if answer["decision"] not in CONFIRMATION_DECISIONS_V2:
        raise RpcCodecError("invalid decision")
    _str(answer["reason"], "reason", empty=True)
    _json_object(answer["metadata"], "metadata")


def validate_v2_result(method: str, result: JsonObject) -> None:
    if method == "initialize":
        _validate_initialize_result(result)
    elif method == "runtime.status":
        _validate_status_result(result)
    elif method == "run.start":
        value = _exact(result, frozenset({"run_id", "start_sequence"}), "run.start result")
        _str(value["run_id"], "run_id")
        _int(value["start_sequence"], "start_sequence", minimum=1)
    elif method in {"run.steer", "run.follow_up"}:
        _validate_interaction_receipt(result)
    elif method == "run.abort":
        value = _exact(result, frozenset({"run_id", "aborted"}), "run.abort result")
        _str(value["run_id"], "run_id")
        _bool(value["aborted"], "aborted")
    elif method == "confirmation.list":
        value = _exact(result, frozenset({"pending"}), "confirmation.list result")
        pending = value["pending"]
        if not isinstance(pending, list):
            raise RpcCodecError("invalid pending records")
        for record in pending:
            decode_record(_json_object(record, "pending record"))
    elif method == "confirmation.respond":
        _validate_confirmation_ack(result)
    elif method == "confirmation.respond_batch":
        value = _exact(result, frozenset({"applied"}), "confirmation batch result")
        applied = value["applied"]
        if not isinstance(applied, list):
            raise RpcCodecError("invalid applied records")
        for item in applied:
            _validate_confirmation_ack(item)
    elif method == "events.replay":
        _validate_replay_result(result)
    elif method == "shutdown":
        value = _exact(result, frozenset({"closed"}), "shutdown result")
        if _bool(value["closed"], "closed") is not True:
            raise RpcCodecError("shutdown result must be closed")
    else:
        raise RpcCodecError("unknown method")


def _validate_initialize_result(result: JsonObject) -> None:
    value = _exact(
        result,
        frozenset(
            {
                "protocol",
                "schema_version",
                "host_id",
                "session_id",
                "stream",
                "active_tool_names",
                "policy_names",
                "capabilities",
                "steering_mode",
                "follow_up_mode",
            }
        ),
        "initialize result",
    )
    if value["protocol"] != "evopi.rpc.v2" or value["schema_version"] != 2:
        raise RpcCodecError("invalid protocol identity")
    _str(value["host_id"], "host_id")
    _str(value["session_id"], "session_id")
    stream = _exact(
        value["stream"],
        frozenset({"stream_id", "cursor", "oldest_sequence", "latest_sequence", "capacity"}),
        "stream",
    )
    _str(stream["stream_id"], "stream_id")
    cursor = _int(stream["cursor"], "cursor")
    oldest = _int(stream["oldest_sequence"], "oldest_sequence")
    latest = _int(stream["latest_sequence"], "latest_sequence")
    _int(stream["capacity"], "capacity", minimum=1)
    if cursor != latest or oldest > latest:
        raise RpcCodecError("invalid stream bounds")
    _string_list(value["active_tool_names"], "active_tool_names")
    _string_list(value["policy_names"], "policy_names")
    capabilities = _exact(
        value["capabilities"],
        frozenset({"event_replay", "confirmation", "text_steering", "text_follow_up"}),
        "capabilities",
    )
    for key in capabilities:
        _bool(capabilities[key], f"capabilities.{key}")
    _str(value["steering_mode"], "steering_mode")
    _str(value["follow_up_mode"], "follow_up_mode")


def _validate_status_result(result: JsonObject) -> None:
    value = _exact(
        result,
        frozenset(
            {
                "active_run_id",
                "lifecycle",
                "session_id",
                "pending_confirmation_count",
                "last_end_reason",
                "last_run_error",
                "steering_mode",
                "follow_up_mode",
                "pending_steering_count",
                "pending_follow_up_count",
            }
        ),
        "runtime.status result",
    )
    _optional_str(value["active_run_id"], "active_run_id")
    _str(value["lifecycle"], "lifecycle")
    _str(value["session_id"], "session_id")
    _int(value["pending_confirmation_count"], "pending_confirmation_count")
    _optional_str(value["last_end_reason"], "last_end_reason")
    _optional_str(value["last_run_error"], "last_run_error")
    _str(value["steering_mode"], "steering_mode")
    _str(value["follow_up_mode"], "follow_up_mode")
    _int(value["pending_steering_count"], "pending_steering_count")
    _int(value["pending_follow_up_count"], "pending_follow_up_count")


def _validate_interaction_receipt(value: Any) -> None:
    receipt = _exact(
        value,
        frozenset({"input_id", "kind", "run_id", "position"}),
        "interaction receipt",
    )
    _str(receipt["input_id"], "input_id")
    if receipt["kind"] not in {"steer", "follow_up"}:
        raise RpcCodecError("invalid interaction kind")
    _str(receipt["run_id"], "run_id")
    _int(receipt["position"], "position", minimum=1)


def _validate_confirmation_ack(value: Any) -> None:
    ack = _exact(
        value,
        frozenset({"request_id", "status", "revision"}),
        "confirmation acknowledgement",
    )
    _str(ack["request_id"], "request_id")
    if ack["status"] not in {"approved", "denied", "cancelled"}:
        raise RpcCodecError("invalid confirmation status")
    _int(ack["revision"], "revision", minimum=1)


def _validate_replay_result(result: JsonObject) -> None:
    value = _exact(
        result,
        frozenset(
            {"stream_id", "after_sequence", "oldest_sequence", "latest_sequence", "events"}
        ),
        "events.replay result",
    )
    stream_id = _str(value["stream_id"], "stream_id")
    after = _int(value["after_sequence"], "after_sequence")
    oldest = _int(value["oldest_sequence"], "oldest_sequence")
    latest = _int(value["latest_sequence"], "latest_sequence")
    if oldest > latest or after > latest:
        raise RpcCodecError("invalid replay bounds")
    events = value["events"]
    if not isinstance(events, list):
        raise RpcCodecError("invalid replay events")
    previous = after
    for raw in events:
        event = decode_v2_event(json.dumps(raw, allow_nan=False))
        if (
            event.stream_id != stream_id
            or event.sequence != previous + 1
            or event.sequence > latest
        ):
            raise RpcCodecError("invalid replay event ordering")
        previous = event.sequence
    if previous != latest:
        raise RpcCodecError("replay events do not reach the snapshot boundary")


__all__ = [
    "CONFIRMATION_DECISIONS_V2",
    "METHOD_HANDLERS_V2",
    "validate_v2_params",
    "validate_v2_result",
]
