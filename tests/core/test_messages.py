from datetime import UTC

from evopi.core.messages import (
    AssistantMessage,
    SystemMessage,
    ToolResultMessage,
    UserMessage,
)


def test_message_roles_are_fixed_by_message_type() -> None:
    assert SystemMessage(content="system").role == "system"
    assert UserMessage(content="user").role == "user"
    assert AssistantMessage(content="assistant").role == "assistant"
    assert (
        ToolResultMessage(
            content="result",
            tool_call_id="call-1",
            tool_name="read_file",
        ).role
        == "tool_result"
    )


def test_message_defaults_are_created_per_instance() -> None:
    first = UserMessage(content="first")
    second = UserMessage(content="second")

    first.metadata["source"] = "test"

    assert first.id != second.id
    assert first.created_at.tzinfo is UTC
    assert second.metadata == {}


def test_assistant_message_starts_without_tool_calls() -> None:
    first = AssistantMessage(content="first")
    second = AssistantMessage(content="second", stop_reason="stop")

    first.tool_calls.append(object())  # type: ignore[arg-type]

    assert second.tool_calls == []
    assert second.stop_reason == "stop"


def test_tool_result_records_its_origin_and_control_flags() -> None:
    result = ToolResultMessage(
        content="permission denied",
        tool_call_id="call-1",
        tool_name="write_file",
        is_error=True,
        terminate=True,
    )

    assert result.tool_call_id == "call-1"
    assert result.tool_name == "write_file"
    assert result.is_error is True
    assert result.terminate is True
