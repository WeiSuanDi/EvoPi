from evopi.core.stream import AssistantMessageBuilder


def test_stream_builder_combines_text_and_fragmented_tool_call() -> None:
    builder = AssistantMessageBuilder()
    builder.add_text("I will ")
    builder.add_text("read it.")
    builder.add_tool_call_delta(
        index=0,
        tool_call_id="call-1",
        tool_name="read_",
        arguments_delta='{"path":',
    )
    builder.add_tool_call_delta(
        index=0,
        tool_name="file",
        arguments_delta='"README.md"}',
    )

    message = builder.build(stop_reason="tool_use")

    assert message.content == "I will read it."
    assert message.tool_calls[0].id == "call-1"
    assert message.tool_calls[0].name == "read_file"
    assert message.tool_calls[0].arguments == {"path": "README.md"}


def test_stream_builder_preserves_invalid_arguments_for_model_feedback() -> None:
    builder = AssistantMessageBuilder()
    builder.add_tool_call_delta(index=0, tool_name="broken", arguments_delta="{")

    message = builder.build(stop_reason="tool_use")

    assert message.tool_calls[0].arguments == {"_raw": "{"}
