from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from evopi.ai.api.anthropic_messages import AnthropicMessagesModel
from evopi.ai.api.openai_chat_completions import OpenAICompatibleModel
from evopi.core.agent import Agent
from evopi.core.context import AgentContext
from evopi.core.messages import SystemMessage, UserMessage
from evopi.core.model_errors import ModelError
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


def test_abort_closes_openai_response_without_closing_injected_client() -> None:
    class BlockingBody(httpx.AsyncByteStream):
        def __init__(self) -> None:
            self.blocking = asyncio.Event()
            self.closed = False

        async def __aiter__(self):
            yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
            self.blocking.set()
            await asyncio.Event().wait()

        async def aclose(self) -> None:
            self.closed = True

    body = BlockingBody()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            stream=body,
            headers={"content-type": "text/event-stream"},
        )

    async def scenario() -> None:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        model = OpenAICompatibleModel(
            model="test-model",
            api_key="test-key",
            base_url="https://example.test/v1",
            client=client,
        )
        agent = Agent(model=model)
        task = asyncio.create_task(agent.prompt("start"))
        await body.blocking.wait()

        agent.abort()
        answer = await task

        assert answer.content == "partial"
        assert answer.stop_reason == "aborted"
        assert body.closed is True
        assert client.is_closed is False
        await client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("provider", ["anthropic", "openai-compatible"])
@pytest.mark.parametrize(
    ("status", "body", "kind"),
    [
        (401, {"error": {"message": "bad key"}}, "authentication"),
        (403, {"error": {"message": "denied"}}, "permission"),
        (429, {"error": {"message": "slow down"}}, "rate_limited"),
        (529, {"error": {"message": "overloaded"}}, "overloaded"),
        (503, {"error": {"message": "unavailable"}}, "server"),
    ],
)
def test_adapters_share_http_error_classification(
    provider: str,
    status: int,
    body: dict,
    kind: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.extensions["timeout"]["read"] == 7.0
        return httpx.Response(status, json=body, headers={"retry-after": "1"})

    async def scenario() -> None:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        if provider == "anthropic":
            model = AnthropicMessagesModel(
                model="test",
                api_key="key",
                base_url="https://example.test",
                timeout=7,
                client=client,
            )
        else:
            model = OpenAICompatibleModel(
                model="test",
                api_key="key",
                base_url="https://example.test/v1",
                timeout=7,
                client=client,
            )
        with pytest.raises(ModelError) as caught:
            _ = [event async for event in model.stream(AgentContext())]
        assert caught.value.info.kind == kind
        assert caught.value.info.retry_after == 1
        assert client.is_closed is False
        await client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("provider", ["anthropic", "openai-compatible"])
def test_adapters_classify_premature_stream_eof_as_connection(provider: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if provider == "anthropic":
            body = 'data: {"type":"message_start","message":{}}\n\n'
        else:
            body = 'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
        return httpx.Response(200, text=body)

    async def scenario() -> None:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        if provider == "anthropic":
            model = AnthropicMessagesModel(model="test", api_key="key", client=client)
        else:
            model = OpenAICompatibleModel(model="test", api_key="key", client=client)
        with pytest.raises(ModelError) as caught:
            _ = [event async for event in model.stream(AgentContext())]
        assert caught.value.info.kind == "connection"
        assert caught.value.info.retryable is True
        await client.aclose()

    asyncio.run(scenario())


def test_invalid_sse_json_is_a_non_retryable_protocol_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="data: {not-json}\n\n")

    async def scenario() -> None:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        model = OpenAICompatibleModel(model="test", api_key="key", client=client)
        with pytest.raises(ModelError) as caught:
            _ = [event async for event in model.stream(AgentContext())]
        assert caught.value.info.kind == "protocol"
        assert caught.value.info.retryable is False
        await client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("provider", "body", "kind"),
    [
        (
            "anthropic",
            'data: {"type":"error","error":{"type":"overloaded_error","message":"busy"}}\n\n',
            "overloaded",
        ),
        (
            "openai-compatible",
            'data: {"error":{"code":"rate_limit_error","message":"slow down"}}\n\n',
            "rate_limited",
        ),
    ],
)
def test_adapters_classify_errors_emitted_inside_stream(
    provider: str,
    body: str,
    kind: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body, headers={"retry-after": "4"})

    async def scenario() -> None:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        if provider == "anthropic":
            model = AnthropicMessagesModel(model="test", api_key="key", client=client)
        else:
            model = OpenAICompatibleModel(model="test", api_key="key", client=client)
        with pytest.raises(ModelError) as caught:
            _ = [event async for event in model.stream(AgentContext())]
        assert caught.value.info.kind == kind
        assert caught.value.info.retry_after == 4
        await client.aclose()

    asyncio.run(scenario())
