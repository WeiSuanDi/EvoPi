from __future__ import annotations

import asyncio
import json

import httpx
import pytest

import evopi.ai as ai
import evopi.ai.api.openai_responses as responses
from evopi.ai.api.openai_chat_completions import OpenAICompatibleModel
from evopi.ai.models import model_from_environment
from evopi.cli.main import build_parser
from evopi.core.agent import Agent
from evopi.core.context import AgentContext
from evopi.core.events import CoreEvent
from evopi.core.messages import (
    AssistantMessage,
    SystemMessage,
    ToolResultMessage,
    UserMessage,
)
from evopi.core.model_errors import ModelError, ModelRetryConfig
from evopi.core.stream import ModelComplete, TextDelta, ToolCallDelta
from evopi.core.tool import Tool, ToolCall


def test_openai_responses_adapter_exposes_a_distinct_model_type() -> None:
    assert hasattr(responses, "OpenAIResponsesModel")
    assert responses.OpenAIResponsesModel is not responses.OpenAICompatibleModel


def test_openai_responses_is_public_and_selected_explicitly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_MODEL", "test-model")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.test/v1")

    assert ai.OpenAIResponsesModel is responses.OpenAIResponsesModel
    selected = model_from_environment("openai-responses")
    legacy = model_from_environment("openai")
    parsed = build_parser().parse_args(
        ["task", "--provider", "openai-responses"]
    )

    assert isinstance(selected, responses.OpenAIResponsesModel)
    assert selected.base_url == "https://example.test/v1"
    assert isinstance(legacy, OpenAICompatibleModel)
    assert parsed.provider == "openai-responses"


def test_openai_responses_streams_text_with_native_request_shape() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers["authorization"]
        captured["timeout"] = request.extensions["timeout"]["read"]
        captured["payload"] = json.loads(request.content)
        body = "\n\n".join(
            [
                (
                    'data: {"type":"response.output_text.delta",'
                    '"item_id":"msg-1","output_index":0,"content_index":0,'
                    '"delta":"Hello"}'
                ),
                (
                    'data: {"type":"response.completed","response":{'
                    '"id":"resp-1","status":"completed","incomplete_details":null,'
                    '"output":[{"id":"msg-1","type":"message","role":"assistant",'
                    '"status":"completed","content":[{"type":"output_text",'
                    '"text":"Hello","annotations":[]}]}],'
                    '"usage":{"input_tokens":7,"output_tokens":2,"total_tokens":9}}}'
                ),
            ]
        )
        return httpx.Response(
            200,
            text=body,
            headers={"content-type": "text/event-stream"},
        )

    async def scenario() -> tuple[list[object], bool]:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        model = responses.OpenAIResponsesModel(
            model="test-model",
            api_key="test-key",
            base_url="https://example.test/v1",
            max_tokens=123,
            temperature=0.25,
            timeout=7,
            client=client,
        )
        tool = Tool(
            name="read_file",
            description="Read one file",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
            handler=lambda path: path,
        )
        events = [
            event
            async for event in model.stream(
                AgentContext(
                    messages=[
                        SystemMessage(content="First"),
                        SystemMessage(content="Second"),
                        UserMessage(content="Hello"),
                    ],
                    tools=[tool],
                )
            )
        ]
        is_closed = client.is_closed
        await client.aclose()
        return events, is_closed

    events, client_was_closed = asyncio.run(scenario())

    assert captured == {
        "url": "https://example.test/v1/responses",
        "authorization": "Bearer test-key",
        "timeout": 7,
        "payload": {
            "model": "test-model",
            "input": [{"role": "user", "content": "Hello"}],
            "instructions": "First\n\nSecond",
            "stream": True,
            "store": False,
            "max_output_tokens": 123,
            "temperature": 0.25,
            "tools": [
                {
                    "type": "function",
                    "name": "read_file",
                    "description": "Read one file",
                    "parameters": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                        "required": ["path"],
                    },
                    "strict": False,
                }
            ],
        },
    }
    assert [event.delta for event in events if isinstance(event, TextDelta)] == ["Hello"]
    final = next(event.message for event in events if isinstance(event, ModelComplete))
    assert final.content == "Hello"
    assert final.stop_reason == "stop"
    assert final.metadata["provider"] == "openai-responses"
    assert final.metadata["usage"] == {
        "input_tokens": 7,
        "output_tokens": 2,
        "total_tokens": 9,
    }
    assert final.metadata["openai_responses"]["output"][0]["id"] == "msg-1"
    assert client_was_closed is False


def test_openai_responses_replays_provider_output_and_tool_results() -> None:
    captured_input: list[object] = []
    provider_output = [
        {
            "id": "reasoning-1",
            "type": "reasoning",
            "encrypted_content": "opaque",
            "summary": [],
        },
        {
            "id": "message-1",
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": "Checking", "annotations": []}],
        },
        {
            "id": "item-1",
            "type": "function_call",
            "call_id": "call-1",
            "name": "read_file",
            "arguments": '{"path":"README.md"}',
            "status": "completed",
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        captured_input.extend(json.loads(request.content)["input"])
        return httpx.Response(
            200,
            text=(
                'data: {"type":"response.completed","response":{'
                '"id":"resp-2","status":"completed","incomplete_details":null,'
                '"output":[{"id":"message-2","type":"message","role":"assistant",'
                '"status":"completed","content":[{"type":"output_text",'
                '"text":"Done","annotations":[]}]}]}}'
            ),
        )

    async def scenario() -> None:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        model = responses.OpenAIResponsesModel(
            model="test-model",
            api_key="test-key",
            client=client,
        )
        _ = [
            event
            async for event in model.stream(
                AgentContext(
                    messages=[
                        UserMessage(content="Read it"),
                        AssistantMessage(
                            content="Checking",
                            tool_calls=[
                                ToolCall(
                                    id="call-1",
                                    name="read_file",
                                    arguments={"path": "README.md"},
                                )
                            ],
                            stop_reason="tool_use",
                            metadata={
                                "provider": "openai-responses",
                                "model": "test-model",
                                "openai_responses": {
                                    "schema_version": 1,
                                    "response_id": "resp-1",
                                    "status": "completed",
                                    "output": provider_output,
                                    "incomplete_details": None,
                                    "compatibility_id": (
                                        responses._provider_compatibility_id(
                                            model="test-model",
                                            base_url="https://api.openai.com/v1",
                                        )
                                    ),
                                },
                            },
                        ),
                        ToolResultMessage(
                            content="contents",
                            tool_call_id="call-1",
                            tool_name="read_file",
                        ),
                        UserMessage(content="Summarize"),
                    ]
                )
            )
        ]
        await client.aclose()

    asyncio.run(scenario())

    assert captured_input == [
        {"role": "user", "content": "Read it"},
        *provider_output,
        {
            "type": "function_call_output",
            "call_id": "call-1",
            "output": "contents",
        },
        {"role": "user", "content": "Summarize"},
    ]


def test_openai_responses_reconstructs_provider_neutral_assistant_history() -> None:
    captured_input: list[object] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_input.extend(json.loads(request.content)["input"])
        return httpx.Response(
            200,
            text=(
                'data: {"type":"response.completed","response":{'
                '"id":"resp-2","status":"completed","incomplete_details":null,'
                '"output":[{"type":"message","role":"assistant","content":[]}]}}'
            ),
        )

    async def scenario() -> None:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        model = responses.OpenAIResponsesModel(
            model="test-model",
            api_key="test-key",
            client=client,
        )
        _ = [
            event
            async for event in model.stream(
                AgentContext(
                    messages=[
                        AssistantMessage(
                            content="I will read it",
                            tool_calls=[
                                ToolCall(
                                    id="call-1",
                                    name="read_file",
                                    arguments={"path": "README.md"},
                                )
                            ],
                            stop_reason="tool_use",
                        ),
                        ToolResultMessage(
                            content="contents",
                            tool_call_id="call-1",
                            tool_name="read_file",
                        ),
                    ]
                )
            )
        ]
        await client.aclose()

    asyncio.run(scenario())

    assert captured_input == [
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "I will read it"}],
        },
        {
            "type": "function_call",
            "call_id": "call-1",
            "name": "read_file",
            "arguments": '{"path": "README.md"}',
        },
        {
            "type": "function_call_output",
            "call_id": "call-1",
            "output": "contents",
        },
    ]


@pytest.mark.parametrize(
    ("stored_model", "stored_base_url"),
    [
        ("other-model", "https://api.openai.com/v1"),
        ("test-model", "https://other.example/v1"),
    ],
)
def test_openai_responses_reconstructs_state_from_incompatible_candidate(
    stored_model: str,
    stored_base_url: str,
) -> None:
    captured_input: list[object] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_input.extend(json.loads(request.content)["input"])
        return httpx.Response(
            200,
            text=(
                'data: {"type":"response.completed","response":{'
                '"id":"resp-2","status":"completed","incomplete_details":null,'
                '"output":[{"type":"message","role":"assistant","content":[]}]}}'
            ),
        )

    async def scenario() -> None:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        model = responses.OpenAIResponsesModel(
            model="test-model",
            api_key="test-key",
            client=client,
        )
        _ = [
            event
            async for event in model.stream(
                AgentContext(
                    messages=[
                        AssistantMessage(
                            content="portable text",
                            stop_reason="stop",
                            metadata={
                                "provider": "openai-responses",
                                "model": stored_model,
                                "openai_responses": {
                                    "schema_version": 1,
                                    "response_id": "resp-old",
                                    "status": "completed",
                                    "output": [
                                        {
                                            "type": "reasoning",
                                            "id": "reasoning-old",
                                            "summary": [],
                                        }
                                    ],
                                    "incomplete_details": None,
                                    "compatibility_id": (
                                        responses._provider_compatibility_id(
                                            model=stored_model,
                                            base_url=stored_base_url,
                                        )
                                    ),
                                },
                            },
                        )
                    ]
                )
            )
        ]
        await client.aclose()

    asyncio.run(scenario())

    assert captured_input == [
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "portable text"}],
        }
    ]


def test_openai_responses_streams_parallel_function_calls() -> None:
    body = "\n\n".join(
        [
            (
                'data: {"type":"response.output_item.added","output_index":0,'
                '"item":{"id":"item-1","type":"function_call","call_id":"call-1",'
                '"name":"read_file","arguments":"","status":"in_progress"}}'
            ),
            (
                'data: {"type":"response.function_call_arguments.delta",'
                '"item_id":"item-1","output_index":0,"delta":"{\\"path\\":"}'
            ),
            (
                'data: {"type":"response.function_call_arguments.delta",'
                '"item_id":"item-1","output_index":0,'
                '"delta":"\\"README.md\\"}"}'
            ),
            (
                'data: {"type":"response.output_item.added","output_index":1,'
                '"item":{"id":"item-2","type":"function_call","call_id":"call-2",'
                '"name":"list_dir","arguments":"","status":"in_progress"}}'
            ),
            (
                'data: {"type":"response.function_call_arguments.delta",'
                '"item_id":"item-2","output_index":1,"delta":"{}"}'
            ),
            (
                'data: {"type":"response.completed","response":{'
                '"id":"resp-tools","status":"completed","incomplete_details":null,'
                '"output":[{"id":"item-1","type":"function_call","call_id":"call-1",'
                '"name":"read_file","arguments":"{\\"path\\":\\"README.md\\"}",'
                '"status":"completed"},{"id":"item-2","type":"function_call",'
                '"call_id":"call-2","name":"list_dir","arguments":"{}",'
                '"status":"completed"}]}}'
            ),
        ]
    )

    async def scenario() -> list[object]:
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, text=body))
        )
        model = responses.OpenAIResponsesModel(
            model="test-model",
            api_key="test-key",
            client=client,
        )
        events = [event async for event in model.stream(AgentContext())]
        await client.aclose()
        return events

    events = asyncio.run(scenario())

    deltas = [event for event in events if isinstance(event, ToolCallDelta)]
    assert [(delta.index, delta.tool_call_id, delta.tool_name) for delta in deltas[:2]] == [
        (0, "call-1", "read_file"),
        (0, None, None),
    ]
    final = next(event.message for event in events if isinstance(event, ModelComplete))
    assert final.stop_reason == "tool_use"
    assert [(call.id, call.name, call.arguments) for call in final.tool_calls] == [
        ("call-1", "read_file", {"path": "README.md"}),
        ("call-2", "list_dir", {}),
    ]


def test_openai_responses_rejects_malformed_persisted_provider_state_before_http() -> None:
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(500)

    async def scenario() -> ModelError:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        model = responses.OpenAIResponsesModel(
            model="test-model",
            api_key="test-key",
            client=client,
        )
        try:
            _ = [
                event
                async for event in model.stream(
                    AgentContext(
                        messages=[
                            AssistantMessage(
                                content="old",
                                stop_reason="stop",
                                metadata={
                                    "provider": "openai-responses",
                                    "openai_responses": {
                                        "schema_version": 2,
                                        "output": [],
                                    },
                                },
                            )
                        ]
                    )
                )
            ]
        except ModelError as exc:
            return exc
        finally:
            await client.aclose()
        raise AssertionError("expected ModelError")

    error = asyncio.run(scenario())

    assert error.info.kind == "protocol"
    assert error.info.code == "invalid_provider_state"
    assert called is False


def test_openai_responses_rejects_non_json_provider_state_before_http() -> None:
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(500)

    async def scenario() -> ModelError:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        model = responses.OpenAIResponsesModel(
            model="test-model",
            api_key="test-key",
            client=client,
        )
        try:
            _ = [
                event
                async for event in model.stream(
                    AgentContext(
                        messages=[
                            AssistantMessage(
                                content="old",
                                stop_reason="stop",
                                metadata={
                                    "provider": "openai-responses",
                                    "openai_responses": {
                                        "schema_version": 1,
                                        "response_id": "resp-old",
                                        "status": "completed",
                                        "output": [
                                            {
                                                "type": "reasoning",
                                                "opaque": object(),
                                            }
                                        ],
                                        "incomplete_details": None,
                                    },
                                },
                            )
                        ]
                    )
                )
            ]
        except ModelError as exc:
            return exc
        finally:
            await client.aclose()
        raise AssertionError("expected ModelError")

    error = asyncio.run(scenario())

    assert error.info.kind == "protocol"
    assert error.info.code == "invalid_provider_state"
    assert called is False


def test_openai_responses_preserves_refusal_and_incomplete_results() -> None:
    refusal_body = "\n\n".join(
        [
            (
                'data: {"type":"response.refusal.delta","item_id":"msg-1",'
                '"output_index":0,"content_index":0,"delta":"Cannot comply"}'
            ),
            (
                'data: {"type":"response.completed","response":{'
                '"id":"resp-refusal","status":"completed","incomplete_details":null,'
                '"output":[{"id":"msg-1","type":"message","role":"assistant",'
                '"status":"completed","content":[{"type":"refusal",'
                '"refusal":"Cannot comply"}]}]}}'
            ),
        ]
    )
    incomplete_body = "\n\n".join(
        [
            (
                'data: {"type":"response.output_text.delta","item_id":"msg-2",'
                '"output_index":0,"content_index":0,"delta":"Partial"}'
            ),
            (
                'data: {"type":"response.incomplete","response":{'
                '"id":"resp-incomplete","status":"incomplete",'
                '"incomplete_details":{"reason":"max_output_tokens"},'
                '"output":[{"id":"msg-2","type":"message","role":"assistant",'
                '"status":"incomplete","content":[{"type":"output_text",'
                '"text":"Partial","annotations":[]}]}]}}'
            ),
        ]
    )

    async def collect(body: str) -> tuple[list[str], AssistantMessage]:
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, text=body)
            )
        )
        model = responses.OpenAIResponsesModel(
            model="test-model",
            api_key="test-key",
            client=client,
        )
        events = [event async for event in model.stream(AgentContext())]
        await client.aclose()
        deltas = [event.delta for event in events if isinstance(event, TextDelta)]
        message = next(
            event.message for event in events if isinstance(event, ModelComplete)
        )
        return deltas, message

    refusal_deltas, refusal = asyncio.run(collect(refusal_body))
    incomplete_deltas, incomplete = asyncio.run(collect(incomplete_body))

    assert refusal_deltas == ["Cannot comply"]
    assert refusal.content == "Cannot comply"
    assert refusal.stop_reason == "stop"
    assert refusal.metadata["refusal"] is True
    assert incomplete_deltas == ["Partial"]
    assert incomplete.content == "Partial"
    assert incomplete.stop_reason == "length"
    assert incomplete.metadata["openai_responses"]["incomplete_details"] == {
        "reason": "max_output_tokens"
    }


def test_openai_responses_failed_event_uses_structured_error_classification() -> None:
    body = (
        'data: {"type":"response.failed","response":{'
        '"id":"resp-failed","status":"failed",'
        '"error":{"code":"server_error","message":"generation failed"},'
        '"output":[]}}'
    )

    async def scenario() -> ModelError:
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    text=body,
                    headers={"retry-after": "3", "x-request-id": "request-1"},
                )
            )
        )
        model = responses.OpenAIResponsesModel(
            model="test-model",
            api_key="test-key",
            client=client,
        )
        try:
            _ = [event async for event in model.stream(AgentContext())]
        except ModelError as exc:
            return exc
        finally:
            await client.aclose()
        raise AssertionError("expected ModelError")

    error = asyncio.run(scenario())

    assert error.info.kind == "server"
    assert error.info.code == "server_error"
    assert error.info.retryable is True
    assert error.info.retry_after == 3
    assert error.info.request_id == "request-1"


def test_openai_responses_stream_error_uses_structured_error_classification() -> None:
    body = (
        'data: {"type":"error","code":"rate_limit_error",'
        '"message":"slow down"}'
    )

    async def scenario() -> ModelError:
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, text=body)
            )
        )
        model = responses.OpenAIResponsesModel(
            model="test-model",
            api_key="test-key",
            client=client,
        )
        try:
            _ = [event async for event in model.stream(AgentContext())]
        except ModelError as exc:
            return exc
        finally:
            await client.aclose()
        raise AssertionError("expected ModelError")

    error = asyncio.run(scenario())

    assert error.info.kind == "rate_limited"
    assert error.info.code == "rate_limit_error"


@pytest.mark.parametrize("item_type", ["web_search_call", "program"])
def test_openai_responses_rejects_unsupported_executable_output_item(
    item_type: str,
) -> None:
    body = (
        'data: {"type":"response.completed","response":{'
        '"id":"resp-unsupported","status":"completed","incomplete_details":null,'
        f'"output":[{{"id":"unsupported-1","type":"{item_type}",'
        '"status":"completed"}]}}'
    )

    async def scenario() -> ModelError:
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, text=body)
            )
        )
        model = responses.OpenAIResponsesModel(
            model="test-model",
            api_key="test-key",
            client=client,
        )
        try:
            _ = [event async for event in model.stream(AgentContext())]
        except ModelError as exc:
            return exc
        finally:
            await client.aclose()
        raise AssertionError("expected ModelError")

    error = asyncio.run(scenario())

    assert error.info.kind == "protocol"
    assert error.info.code == "unsupported_response_item"


def test_openai_responses_rejects_terminal_status_mismatch() -> None:
    body = (
        'data: {"type":"response.completed","response":{'
        '"id":"resp-mismatch","status":"incomplete","incomplete_details":null,'
        '"output":[]}}'
    )

    async def scenario() -> ModelError:
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, text=body)
            )
        )
        model = responses.OpenAIResponsesModel(
            model="test-model",
            api_key="test-key",
            client=client,
        )
        try:
            _ = [event async for event in model.stream(AgentContext())]
        except ModelError as exc:
            return exc
        finally:
            await client.aclose()
        raise AssertionError("expected ModelError")

    error = asyncio.run(scenario())

    assert error.info.kind == "protocol"
    assert error.info.code == "terminal_status_mismatch"


def test_openai_responses_rejects_output_items_without_a_type() -> None:
    body = (
        'data: {"type":"response.completed","response":{'
        '"id":"resp-invalid-item","status":"completed","incomplete_details":null,'
        '"output":[{"id":"item-without-type"}]}}'
    )

    async def scenario() -> ModelError:
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, text=body)
            )
        )
        model = responses.OpenAIResponsesModel(
            model="test-model",
            api_key="test-key",
            client=client,
        )
        try:
            _ = [event async for event in model.stream(AgentContext())]
        except ModelError as exc:
            return exc
        finally:
            await client.aclose()
        raise AssertionError("expected ModelError")

    error = asyncio.run(scenario())

    assert error.info.kind == "protocol"
    assert error.info.code == "invalid_terminal_response"


def test_openai_responses_rejects_non_json_safe_terminal_state() -> None:
    body = (
        'data: {"type":"response.completed","response":{'
        '"id":"resp-invalid-state","status":"completed","incomplete_details":null,'
        '"output":[{"id":"reasoning-1","type":"reasoning","score":NaN}]}}'
    )

    async def scenario() -> ModelError:
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, text=body)
            )
        )
        model = responses.OpenAIResponsesModel(
            model="test-model",
            api_key="test-key",
            client=client,
        )
        try:
            _ = [event async for event in model.stream(AgentContext())]
        except ModelError as exc:
            return exc
        finally:
            await client.aclose()
        raise AssertionError("expected ModelError")

    error = asyncio.run(scenario())

    assert error.info.kind == "protocol"
    assert error.info.code == "invalid_terminal_response"


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ('data: {"delta":"missing type"}', "invalid_stream_event"),
        (
            "\n\n".join(
                [
                    (
                        'data: {"type":"response.completed","response":{'
                        '"id":"resp-terminal","status":"completed",'
                        '"incomplete_details":null,"output":[]}}'
                    ),
                    (
                        'data: {"type":"response.failed","response":{'
                        '"id":"resp-failed","status":"failed",'
                        '"error":{"code":"server_error","message":"failed"},'
                        '"output":[]}}'
                    ),
                ]
            ),
            "duplicate_terminal_event",
        ),
    ],
)
def test_openai_responses_rejects_invalid_or_conflicting_stream_events(
    body: str,
    code: str,
) -> None:
    async def scenario() -> ModelError:
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, text=body)
            )
        )
        model = responses.OpenAIResponsesModel(
            model="test-model",
            api_key="test-key",
            client=client,
        )
        try:
            _ = [event async for event in model.stream(AgentContext())]
        except ModelError as exc:
            return exc
        finally:
            await client.aclose()
        raise AssertionError("expected ModelError")

    error = asyncio.run(scenario())

    assert error.info.kind == "protocol"
    assert error.info.code == code


@pytest.mark.parametrize(
    ("status", "body", "kind"),
    [
        (401, {"error": {"message": "bad key"}}, "authentication"),
        (403, {"error": {"message": "denied"}}, "permission"),
        (
            400,
            {
                "error": {
                    "code": "context_length_exceeded",
                    "message": "context window exceeded",
                }
            },
            "context_overflow",
        ),
        (
            429,
            {
                "error": {
                    "code": "insufficient_quota",
                    "message": "quota exceeded",
                }
            },
            "quota_exhausted",
        ),
        (429, {"error": {"message": "slow down"}}, "rate_limited"),
        (503, {"error": {"message": "unavailable"}}, "server"),
    ],
)
def test_openai_responses_reuses_provider_neutral_http_errors(
    status: int,
    body: dict[str, object],
    kind: str,
) -> None:
    async def scenario() -> ModelError:
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    status,
                    json=body,
                    headers={"retry-after": "2", "x-request-id": "request-http"},
                )
            )
        )
        model = responses.OpenAIResponsesModel(
            model="test-model",
            api_key="test-key",
            client=client,
        )
        try:
            _ = [event async for event in model.stream(AgentContext())]
        except ModelError as exc:
            return exc
        finally:
            await client.aclose()
        raise AssertionError("expected ModelError")

    error = asyncio.run(scenario())

    assert error.info.kind == kind
    assert error.info.retry_after == 2
    assert error.info.request_id == "request-http"


@pytest.mark.parametrize(
    ("body", "kind", "code"),
    [
        ("data: {not-json}\n\n", "protocol", "invalid_sse_json"),
        (
            (
                'data: {"type":"response.output_text.delta","item_id":"msg-1",'
                '"output_index":0,"content_index":0,"delta":"partial"}'
            ),
            "connection",
            "premature_stream_eof",
        ),
    ],
)
def test_openai_responses_rejects_invalid_or_unfinished_streams(
    body: str,
    kind: str,
    code: str,
) -> None:
    async def scenario() -> ModelError:
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, text=body)
            )
        )
        model = responses.OpenAIResponsesModel(
            model="test-model",
            api_key="test-key",
            client=client,
        )
        try:
            _ = [event async for event in model.stream(AgentContext())]
        except ModelError as exc:
            return exc
        finally:
            await client.aclose()
        raise AssertionError("expected ModelError")

    error = asyncio.run(scenario())

    assert error.info.kind == kind
    assert error.info.code == code


def test_openai_responses_retry_discards_partial_attempt_from_context() -> None:
    calls = 0
    first = "\n\n".join(
        [
            (
                'data: {"type":"response.output_text.delta","item_id":"msg-1",'
                '"output_index":0,"content_index":0,"delta":"partial"}'
            ),
            (
                'data: {"type":"error","code":"server_error",'
                '"message":"try again"}'
            ),
        ]
    )
    second = (
        'data: {"type":"response.completed","response":{'
        '"id":"resp-success","status":"completed","incomplete_details":null,'
        '"output":[{"id":"msg-2","type":"message","role":"assistant",'
        '"status":"completed","content":[{"type":"output_text",'
        '"text":"success","annotations":[]}]}]}}'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, text=first if calls == 1 else second)

    async def scenario() -> tuple[AssistantMessage, list[CoreEvent], list[object]]:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        model = responses.OpenAIResponsesModel(
            model="test-model",
            api_key="test-key",
            client=client,
        )
        events: list[CoreEvent] = []
        agent = Agent(
            model=model,
            retry_config=ModelRetryConfig(
                enabled=True,
                max_retries=1,
                base_delay=0,
            ),
        )
        agent.subscribe(events.append)
        answer = await agent.prompt("go")
        messages = list(agent.messages)
        await client.aclose()
        return answer, events, messages

    answer, events, messages = asyncio.run(scenario())

    assert calls == 2
    assert answer.content == "success"
    failed = [
        event.data["message"]
        for event in events
        if event.type == "message_end"
        and getattr(event.data.get("message"), "stop_reason", None) == "error"
    ]
    assert failed[0].content == "partial"
    assert all(getattr(message, "content", None) != "partial" for message in messages)


def test_openai_responses_agent_continues_function_call_with_tool_output() -> None:
    requests: list[dict[str, object]] = []
    tool_response = "\n\n".join(
        [
            (
                'data: {"type":"response.output_item.added","output_index":0,'
                '"item":{"id":"item-1","type":"function_call","call_id":"call-1",'
                '"name":"read_file","arguments":"","status":"in_progress"}}'
            ),
            (
                'data: {"type":"response.function_call_arguments.delta",'
                '"item_id":"item-1","output_index":0,'
                '"delta":"{\\"path\\":\\"README.md\\"}"}'
            ),
            (
                'data: {"type":"response.completed","response":{'
                '"id":"resp-tool","status":"completed","incomplete_details":null,'
                '"output":[{"id":"item-1","type":"function_call","call_id":"call-1",'
                '"name":"read_file","arguments":"{\\"path\\":\\"README.md\\"}",'
                '"status":"completed"}]}}'
            ),
        ]
    )
    summary_response = (
        'data: {"type":"response.completed","response":{'
        '"id":"resp-summary","status":"completed","incomplete_details":null,'
        '"output":[{"id":"msg-2","type":"message","role":"assistant",'
        '"status":"completed","content":[{"type":"output_text",'
        '"text":"summary","annotations":[]}]}]}}'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        body = tool_response if len(requests) == 1 else summary_response
        return httpx.Response(200, text=body)

    async def scenario() -> AssistantMessage:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        model = responses.OpenAIResponsesModel(
            model="test-model",
            api_key="test-key",
            client=client,
        )
        agent = Agent(
            model=model,
            tools=[
                Tool(
                    name="read_file",
                    description="Read one file",
                    parameters={
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                        "required": ["path"],
                    },
                    handler=lambda path: "file contents",
                )
            ],
        )
        answer = await agent.prompt("read")
        await client.aclose()
        return answer

    answer = asyncio.run(scenario())

    assert answer.content == "summary"
    assert len(requests) == 2
    assert requests[1]["input"] == [
        {"role": "user", "content": "read"},
        {
            "id": "item-1",
            "type": "function_call",
            "call_id": "call-1",
            "name": "read_file",
            "arguments": '{"path":"README.md"}',
            "status": "completed",
        },
        {
            "type": "function_call_output",
            "call_id": "call-1",
            "output": "file contents",
        },
    ]


def test_openai_responses_abort_closes_response_not_injected_client() -> None:
    class BlockingBody(httpx.AsyncByteStream):
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.closed = False

        async def __aiter__(self):
            yield (
                b'data: {"type":"response.output_text.delta","item_id":"msg-1",'
                b'"output_index":0,"content_index":0,"delta":"partial"}\n\n'
            )
            self.started.set()
            await asyncio.Event().wait()

        async def aclose(self) -> None:
            self.closed = True

    async def scenario() -> tuple[AssistantMessage, bool, bool]:
        body = BlockingBody()
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    stream=body,
                    headers={"content-type": "text/event-stream"},
                )
            )
        )
        model = responses.OpenAIResponsesModel(
            model="test-model",
            api_key="test-key",
            client=client,
        )
        agent = Agent(model=model)
        task = asyncio.create_task(agent.prompt("start"))
        await body.started.wait()
        agent.abort()
        answer = await task
        result = answer, body.closed, client.is_closed
        await client.aclose()
        return result

    answer, response_closed, client_closed = asyncio.run(scenario())

    assert answer.content == "partial"
    assert answer.stop_reason == "aborted"
    assert response_closed is True
    assert client_closed is False
