from __future__ import annotations

import asyncio
import json

import httpx

from evopi.ai.api.anthropic_messages import AnthropicMessagesModel
from evopi.ai.api.openai_chat_completions import OpenAICompatibleModel
from evopi.core.context import AgentContext
from evopi.core.messages import SystemMessage, UserMessage
from evopi.core.stream import ModelComplete, TextDelta
from evopi.core.tool import Tool


def test_openai_compatible_stream_is_converted_to_core_events() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        body = "\n\n".join(
            [
                'data: {"choices":[{"delta":{"content":"Hello"},"finish_reason":null}]}',
                'data: {"choices":[{"delta":{"content":"!"},"finish_reason":"stop"}]}',
                "data: [DONE]",
            ]
        )
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    model = OpenAICompatibleModel(
        model="test-model", api_key="test-key", base_url="https://example.test/v1", client=client
    )

    async def collect() -> list:
        events = [
            event
            async for event in model.stream(
                AgentContext(
                    messages=[SystemMessage(content="Be helpful"), UserMessage(content="Hi")]
                )
            )
        ]
        await client.aclose()
        return events

    events = asyncio.run(collect())

    assert [event.delta for event in events if isinstance(event, TextDelta)] == ["Hello", "!"]
    final = next(event.message for event in events if isinstance(event, ModelComplete))
    assert final.content == "Hello!"
    assert captured["messages"][0] == {"role": "system", "content": "Be helpful"}


def test_anthropic_stream_builds_tool_call() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        body = "\n\n".join(
            [
                'data: {"type":"message_start","message":{}}',
                'data: {"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"call-1","name":"read_file","input":{}}}',
                'data: {"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"{\\"path\\":\\"README.md\\"}"}}',
                'data: {"type":"content_block_stop","index":0}',
                'data: {"type":"message_delta","delta":{"stop_reason":"tool_use"}}',
                'data: {"type":"message_stop"}',
            ]
        )
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    model = AnthropicMessagesModel(
        model="test-model", api_key="test-key", base_url="https://example.test", client=client
    )
    tool = Tool(
        name="read_file",
        description="Read a file",
        parameters={"type": "object", "properties": {"path": {"type": "string"}}},
        handler=lambda path: path,
    )

    async def collect() -> list:
        events = [
            event
            async for event in model.stream(
                AgentContext(
                    messages=[SystemMessage(content="Be helpful"), UserMessage(content="Read")],
                    tools=[tool],
                )
            )
        ]
        await client.aclose()
        return events

    events = asyncio.run(collect())

    final = next(event.message for event in events if isinstance(event, ModelComplete))
    assert final.stop_reason == "tool_use"
    assert final.tool_calls[0].arguments == {"path": "README.md"}
    assert captured["system"] == "Be helpful"
    assert captured["tools"][0]["input_schema"]["type"] == "object"
