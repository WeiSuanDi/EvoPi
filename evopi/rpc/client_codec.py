"""Client-side conversion from validated v2 wire data to public DTOs."""

from __future__ import annotations

from typing import Any, cast

from evopi.core.types import JsonObject
from evopi.harness.confirmation_codec import decode_record

from .client_types import (
    RpcClientEvent,
    RpcConfirmationAck,
    RpcConfirmationEvent,
    RpcConfirmationRecord,
    RpcErrorEvent,
    RpcEventBase,
    RpcEventCursor,
    RpcInteractionEvent,
    RpcInteractionReceipt,
    RpcMessage,
    RpcMessageEvent,
    RpcRunEvent,
    RpcRunResult,
    RpcRuntimeStatus,
    RpcServerInfo,
    RpcToolExecutionEvent,
    RpcToolCall,
    RpcTurnEvent,
    RpcUnknownEvent,
)
from .errors import RpcCodecError
from .codec import parse_utc_timestamp
from .protocol_v2 import RpcV2Event


def server_info_from_result(value: JsonObject) -> RpcServerInfo:
    stream = cast(JsonObject, value["stream"])
    return RpcServerInfo(
        host_id=cast(str, value["host_id"]),
        session_id=cast(str, value["session_id"]),
        cursor=RpcEventCursor(
            stream_id=cast(str, stream["stream_id"]),
            sequence=cast(int, stream["cursor"]),
        ),
        oldest_sequence=cast(int, stream["oldest_sequence"]),
        latest_sequence=cast(int, stream["latest_sequence"]),
        capacity=cast(int, stream["capacity"]),
        active_tool_names=tuple(cast(list[str], value["active_tool_names"])),
        policy_names=tuple(cast(list[str], value["policy_names"])),
        steering_mode=cast(str, value["steering_mode"]),
        follow_up_mode=cast(str, value["follow_up_mode"]),
        capabilities=cast(JsonObject, value["capabilities"]),
    )


def runtime_status_from_result(value: JsonObject) -> RpcRuntimeStatus:
    return RpcRuntimeStatus(**cast(Any, value))


def interaction_receipt_from_result(value: JsonObject) -> RpcInteractionReceipt:
    return RpcInteractionReceipt(
        input_id=cast(str, value["input_id"]),
        kind=cast(Any, value["kind"]),
        run_id=cast(str, value["run_id"]),
        position=cast(int, value["position"]),
    )


def confirmation_record_from_data(value: JsonObject) -> RpcConfirmationRecord:
    record = decode_record(value)
    request = record.request
    tool_name = request.tool_call.name if request.tool_call is not None else None
    return RpcConfirmationRecord(
        request_id=request.id,
        revision=record.revision,
        status=record.status,
        run_id=request.run_id,
        hook=request.hook,
        reason=request.reason,
        risk_level=request.risk_level,
        policy_names=request.policy_names,
        tool_name=tool_name,
    )


def confirmation_ack_from_data(value: JsonObject) -> RpcConfirmationAck:
    return RpcConfirmationAck(
        request_id=cast(str, value["request_id"]),
        status=cast(str, value["status"]),
        revision=cast(int, value["revision"]),
    )


_RUN_EVENTS = frozenset({"agent_start", "agent_end"})
_TURN_EVENTS = frozenset({"turn_start", "turn_end"})
_MESSAGE_EVENTS = frozenset({"message_start", "message_update", "message_end"})
_TOOL_EVENTS = frozenset(
    {"tool_execution_start", "tool_execution_update", "tool_execution_end"}
)
_CONFIRMATION_EVENTS = frozenset(
    {"confirmation_request", "confirmation_response", "confirmation_state_changed"}
)
_INTERACTION_EVENTS = frozenset(
    {
        "interaction_queued",
        "interaction_delivered",
        "interaction_cleared",
        "steering_queued",
        "follow_up_queued",
    }
)


def client_event_from_wire(event: RpcV2Event) -> RpcClientEvent:
    cursor = RpcEventCursor(stream_id=event.stream_id, sequence=event.sequence)
    cls: type[RpcEventBase]
    if event.type in _RUN_EVENTS:
        cls = RpcRunEvent
    elif event.type in _TURN_EVENTS:
        cls = RpcTurnEvent
    elif event.type in _MESSAGE_EVENTS:
        cls = RpcMessageEvent
    elif event.type in _TOOL_EVENTS:
        cls = RpcToolExecutionEvent
    elif event.type in _CONFIRMATION_EVENTS:
        cls = RpcConfirmationEvent
    elif event.type in _INTERACTION_EVENTS:
        cls = RpcInteractionEvent
    elif event.type == "error":
        cls = RpcErrorEvent
    else:
        cls = RpcUnknownEvent
    return cls(
        event_id=event.event_id,
        cursor=cursor,
        event_type=event.type,
        run_id=event.run_id,
        created_at=event.created_at,
        data=event.data,
    )


def _message_from_data(value: Any) -> RpcMessage:
    if not isinstance(value, dict):
        raise RpcCodecError("agent_end message must be an object")
    message_id = value.get("id")
    role = value.get("role")
    content = value.get("content")
    created_at = value.get("created_at")
    metadata = value.get("metadata")
    stop_reason = value.get("stop_reason")
    if not isinstance(message_id, str) or not message_id:
        raise RpcCodecError("agent_end message id is invalid")
    if not isinstance(role, str) or not role:
        raise RpcCodecError("agent_end message role is invalid")
    if not isinstance(content, str):
        raise RpcCodecError("agent_end message content is invalid")
    parsed_created_at = parse_utc_timestamp(created_at)
    if not isinstance(metadata, dict):
        raise RpcCodecError("agent_end message metadata is invalid")
    if stop_reason is not None and not isinstance(stop_reason, str):
        raise RpcCodecError("agent_end stop reason is invalid")
    raw_tool_calls = value.get("tool_calls", [])
    if not isinstance(raw_tool_calls, list):
        raise RpcCodecError("agent_end message tool_calls is invalid")
    tool_calls = tuple(_tool_call_from_data(item) for item in raw_tool_calls)
    tool_call_id = value.get("tool_call_id")
    tool_name = value.get("tool_name")
    is_error = value.get("is_error")
    terminate = value.get("terminate")
    if tool_call_id is not None and not isinstance(tool_call_id, str):
        raise RpcCodecError("agent_end tool_call_id is invalid")
    if tool_name is not None and not isinstance(tool_name, str):
        raise RpcCodecError("agent_end tool_name is invalid")
    if is_error is not None and type(is_error) is not bool:
        raise RpcCodecError("agent_end is_error is invalid")
    if terminate is not None and type(terminate) is not bool:
        raise RpcCodecError("agent_end terminate is invalid")
    return RpcMessage(
        id=message_id,
        role=role,
        content=content,
        created_at=parsed_created_at,
        metadata=cast(JsonObject, metadata),
        stop_reason=stop_reason,
        tool_calls=tool_calls,
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        is_error=is_error,
        terminate=terminate,
        data=cast(JsonObject, value),
    )


def _tool_call_from_data(value: Any) -> RpcToolCall:
    if not isinstance(value, dict):
        raise RpcCodecError("agent_end tool_call must be an object")
    call_id = value.get("id")
    name = value.get("name")
    arguments = value.get("arguments")
    argument_error = value.get("argument_error")
    if not isinstance(call_id, str) or not call_id:
        raise RpcCodecError("agent_end tool_call id is invalid")
    if not isinstance(name, str) or not name:
        raise RpcCodecError("agent_end tool_call name is invalid")
    if not isinstance(arguments, dict):
        raise RpcCodecError("agent_end tool_call arguments are invalid")
    if argument_error is not None and not isinstance(argument_error, dict):
        raise RpcCodecError("agent_end tool_call argument_error is invalid")
    return RpcToolCall(
        id=call_id,
        name=name,
        arguments=cast(JsonObject, arguments),
        argument_error=cast(JsonObject | None, argument_error),
    )


def run_result_from_event(event: RpcRunEvent) -> RpcRunResult:
    if event.event_type != "agent_end" or event.run_id is None:
        raise RpcCodecError("run result requires a correlated agent_end event")
    data = event.data
    reason = data.get("reason")
    turns_used = data.get("turns_used")
    max_turns = data.get("max_turns")
    raw_messages = data.get("messages")
    error = data.get("error")
    error_info = data.get("error_info")
    if not isinstance(reason, str) or not reason:
        raise RpcCodecError("agent_end reason is invalid")
    if type(turns_used) is not int or turns_used < 0:
        raise RpcCodecError("agent_end turns_used is invalid")
    if type(max_turns) is not int or max_turns < 1:
        raise RpcCodecError("agent_end max_turns is invalid")
    if not isinstance(raw_messages, list):
        raise RpcCodecError("agent_end messages is invalid")
    if error is not None and not isinstance(error, str):
        raise RpcCodecError("agent_end error is invalid")
    if error_info is not None and not isinstance(error_info, dict):
        raise RpcCodecError("agent_end error_info is invalid")
    messages = tuple(_message_from_data(item) for item in raw_messages)
    final_assistant = next((item for item in reversed(messages) if item.role == "assistant"), None)
    return RpcRunResult(
        run_id=event.run_id,
        end_reason=reason,
        turns_used=turns_used,
        max_turns=max_turns,
        messages=messages,
        final_assistant=final_assistant,
        error=error,
        error_info=cast(JsonObject | None, error_info),
        cursor=event.cursor,
    )


__all__ = [
    "client_event_from_wire",
    "confirmation_ack_from_data",
    "confirmation_record_from_data",
    "interaction_receipt_from_result",
    "run_result_from_event",
    "runtime_status_from_result",
    "server_info_from_result",
]
