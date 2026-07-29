"""Streaming OpenAI Responses API adapter."""

from __future__ import annotations

import json
import hashlib
from collections.abc import AsyncIterator
from typing import Any

import httpx

from evopi.ai.api.base import (
    ModelRequestError,
    iter_sse_data,
    model_error_from_payload,
    normalize_model_exception,
    parse_retry_after,
    raise_for_model_status,
)
from evopi.ai.api.openai_chat_completions import OpenAICompatibleModel
from evopi.ai.auth.resolve import resolve_api_key
from evopi.core.cancellation import AbortSignal
from evopi.core.context import AgentContext
from evopi.core.messages import (
    AssistantMessage,
    SystemMessage,
    ToolResultMessage,
    UserMessage,
)
from evopi.core.model_errors import ModelError
from evopi.core.stream import (
    AssistantMessageBuilder,
    ModelComplete,
    ModelStreamEvent,
    TextDelta,
    ToolCallDelta,
)

_PROVIDER = "openai-responses"
_SUPPORTED_OUTPUT_ITEMS = {"message", "function_call", "reasoning"}


class OpenAIResponsesModel:
    """Native OpenAI Responses API model."""

    def __init__(
        self,
        *,
        model: str,
        api_key: str | None = None,
        base_url: str = "https://api.openai.com/v1",
        max_tokens: int = 4096,
        temperature: float | None = None,
        timeout: float = 120.0,
        headers: dict[str, str] | None = None,
        client: httpx.AsyncClient | None = None,
        context_window: int = 0,
    ) -> None:
        self.model = model
        self.api_key = resolve_api_key(api_key, "OPENAI_API_KEY")
        self.base_url = base_url.rstrip("/")
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.timeout = timeout
        self.headers = dict(headers or {})
        self._client = client
        self._context_window = context_window
        self._state_compatibility_id = _provider_compatibility_id(
            model=self.model,
            base_url=self.base_url,
        )

    @property
    def name(self) -> str:
        return self.model

    @property
    def context_window(self) -> int:
        return self._context_window

    async def stream(
        self,
        context: AgentContext,
        *,
        signal: AbortSignal | None = None,
    ) -> AsyncIterator[ModelStreamEvent]:
        system = "\n\n".join(message.content for message in context.system_messages)
        payload: dict[str, Any] = {
            "model": self.model,
            "input": _convert_input(
                context,
                compatibility_id=self._state_compatibility_id,
            ),
            "stream": True,
            "store": False,
            "max_output_tokens": self.max_tokens,
        }
        if system:
            payload["instructions"] = system
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        if context.tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                    "strict": False,
                }
                for tool in context.tools
            ]

        headers = {"Authorization": f"Bearer {self.api_key}", **self.headers}
        client = self._client or httpx.AsyncClient(timeout=self.timeout)
        owns_client = self._client is None
        terminal: dict[str, Any] | None = None
        terminal_type: str | None = None
        try:
            try:
                async with client.stream(
                    "POST",
                    f"{self.base_url}/responses",
                    headers=headers,
                    json=payload,
                    timeout=self.timeout,
                ) as response:
                    await raise_for_model_status(response, provider=_PROVIDER)
                    async for event in iter_sse_data(response, provider=_PROVIDER):
                        if signal is not None and signal.aborted:
                            return
                        event_type = event.get("type")
                        if not isinstance(event_type, str) or not event_type:
                            raise _protocol_error(
                                "OpenAI Responses stream event type must be a "
                                "non-empty string",
                                code="invalid_stream_event",
                            )
                        if event_type == "response.output_text.delta":
                            delta = _require_string(event, "delta", event_type)
                            if delta:
                                yield TextDelta(delta=delta)
                        elif event_type == "response.refusal.delta":
                            delta = _require_string(event, "delta", event_type)
                            if delta:
                                yield TextDelta(delta=delta)
                        elif event_type == "response.output_item.added":
                            item = _require_mapping(event, "item", event_type)
                            if item.get("type") == "function_call":
                                yield ToolCallDelta(
                                    index=_require_int(event, "output_index", event_type),
                                    tool_call_id=_require_string(
                                        item, "call_id", event_type
                                    ),
                                    tool_name=_require_string(item, "name", event_type),
                                )
                        elif event_type == "response.function_call_arguments.delta":
                            yield ToolCallDelta(
                                index=_require_int(event, "output_index", event_type),
                                arguments_delta=_require_string(
                                    event, "delta", event_type
                                ),
                            )
                        elif event_type in {
                            "response.completed",
                            "response.incomplete",
                        }:
                            if terminal is not None:
                                raise _protocol_error(
                                    "OpenAI Responses stream emitted multiple terminal events",
                                    code="duplicate_terminal_event",
                                )
                            terminal = _require_mapping(event, "response", event_type)
                            terminal_type = event_type
                        elif event_type == "response.failed":
                            if terminal is not None:
                                raise _protocol_error(
                                    "OpenAI Responses stream emitted multiple "
                                    "terminal events",
                                    code="duplicate_terminal_event",
                                )
                            failed = _require_mapping(
                                event, "response", event_type
                            )
                            error = failed.get("error")
                            payload_error = error if isinstance(error, dict) else failed
                            raise model_error_from_payload(
                                payload_error,
                                provider=_PROVIDER,
                                retry_after=parse_retry_after(
                                    response.headers.get("retry-after")
                                ),
                                request_id=(
                                    response.headers.get("x-request-id")
                                    or _optional_string(failed.get("id"))
                                ),
                            )
                        elif event_type == "error":
                            if terminal is not None:
                                raise _protocol_error(
                                    "OpenAI Responses stream emitted an error "
                                    "after a terminal event",
                                    code="duplicate_terminal_event",
                                )
                            raise model_error_from_payload(
                                event,
                                provider=_PROVIDER,
                                retry_after=parse_retry_after(
                                    response.headers.get("retry-after")
                                ),
                                request_id=response.headers.get("x-request-id"),
                            )
            except ModelError:
                raise
            except Exception as exc:
                raise normalize_model_exception(exc, provider=_PROVIDER) from exc
        finally:
            if owns_client:
                await client.aclose()

        if terminal is None:
            raise ModelRequestError(
                "OpenAI Responses stream ended without a terminal event",
                kind="connection",
                provider=_PROVIDER,
                code="premature_stream_eof",
            )

        yield ModelComplete(
            message=_message_from_terminal(
                terminal,
                model=self.model,
                compatibility_id=self._state_compatibility_id,
                incomplete=terminal_type == "response.incomplete",
            )
        )


def _provider_compatibility_id(*, model: str, base_url: str) -> str:
    value = f"{base_url.rstrip('/')}\n{model}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _convert_input(
    context: AgentContext,
    *,
    compatibility_id: str,
) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    for message in context.messages:
        if isinstance(message, SystemMessage):
            continue
        if isinstance(message, UserMessage):
            converted.append({"role": "user", "content": message.content})
            continue
        if isinstance(message, AssistantMessage):
            provider_output = _provider_output(
                message,
                compatibility_id=compatibility_id,
            )
            if provider_output is not None:
                converted.extend(provider_output)
                continue
            if message.content:
                converted.append(
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {"type": "output_text", "text": message.content}
                        ],
                    }
                )
            converted.extend(
                {
                    "type": "function_call",
                    "call_id": call.id,
                    "name": call.name,
                    "arguments": json.dumps(call.arguments, ensure_ascii=False),
                }
                for call in message.tool_calls
            )
            continue
        if isinstance(message, ToolResultMessage):
            converted.append(
                {
                    "type": "function_call_output",
                    "call_id": message.tool_call_id,
                    "output": message.content,
                }
            )
    return converted


def _provider_output(
    message: AssistantMessage,
    *,
    compatibility_id: str,
) -> list[dict[str, Any]] | None:
    state = message.metadata.get("openai_responses")
    if state is None or message.metadata.get("provider") != _PROVIDER:
        return None
    if not isinstance(state, dict) or state.get("schema_version") != 1:
        raise _protocol_error(
            "Stored OpenAI Responses provider state has an unsupported schema",
            code="invalid_provider_state",
        )
    response_id = state.get("response_id")
    status = state.get("status")
    output = state.get("output")
    incomplete_details = state.get("incomplete_details")
    stored_compatibility_id = state.get("compatibility_id")
    if (
        not isinstance(response_id, str)
        or not response_id
        or status not in {"completed", "incomplete"}
        or not isinstance(output, list)
        or not all(isinstance(item, dict) for item in output)
        or (
            incomplete_details is not None
            and not isinstance(incomplete_details, dict)
        )
    ):
        raise _protocol_error(
            "Stored OpenAI Responses provider state is malformed",
            code="invalid_provider_state",
        )
    _ensure_json_safe(
        state,
        message="Stored OpenAI Responses provider state is not JSON-safe",
        code="invalid_provider_state",
    )
    if stored_compatibility_id is None:
        return None
    if not isinstance(stored_compatibility_id, str):
        raise _protocol_error(
            "Stored OpenAI Responses compatibility identity is malformed",
            code="invalid_provider_state",
        )
    if stored_compatibility_id != compatibility_id:
        return None
    return [dict(item) for item in output]


def _message_from_terminal(
    response: dict[str, Any],
    *,
    model: str,
    compatibility_id: str,
    incomplete: bool,
) -> AssistantMessage:
    response_id = response.get("id")
    status = response.get("status")
    expected_status = "incomplete" if incomplete else "completed"
    if status != expected_status:
        raise _protocol_error(
            "OpenAI Responses terminal event and response status disagree",
            code="terminal_status_mismatch",
        )
    output = response.get("output")
    incomplete_details = response.get("incomplete_details")
    if (
        not isinstance(response_id, str)
        or not response_id
        or status not in {"completed", "incomplete"}
        or not isinstance(output, list)
        or not all(isinstance(item, dict) for item in output)
        or (
            incomplete_details is not None
            and not isinstance(incomplete_details, dict)
        )
    ):
        raise _protocol_error(
            "OpenAI Responses terminal payload is malformed",
            code="invalid_terminal_response",
        )

    output_items = [dict(item) for item in output]
    builder = AssistantMessageBuilder()
    refusal = False
    has_tool_call = False
    for index, item in enumerate(output_items):
        item_type = item.get("type")
        if not isinstance(item_type, str) or not item_type:
            raise _protocol_error(
                "OpenAI Responses output item type must be a non-empty string",
                code="invalid_terminal_response",
            )
        if item_type not in _SUPPORTED_OUTPUT_ITEMS:
            raise _protocol_error(
                f"Unsupported OpenAI Responses output item: {item_type}",
                code="unsupported_response_item",
            )
        if item_type == "message":
            content = item.get("content")
            if not isinstance(content, list):
                raise _protocol_error(
                    "OpenAI Responses message content must be an array",
                    code="invalid_terminal_response",
                )
            for part in content:
                if not isinstance(part, dict):
                    raise _protocol_error(
                        "OpenAI Responses message content item must be an object",
                        code="invalid_terminal_response",
                    )
                if part.get("type") == "output_text":
                    builder.add_text(_require_string(part, "text", "output_text"))
                elif part.get("type") == "refusal":
                    builder.add_text(_require_string(part, "refusal", "refusal"))
                    refusal = True
        elif item_type == "function_call":
            has_tool_call = True
            builder.add_tool_call_delta(
                index=index,
                tool_call_id=_require_string(item, "call_id", item_type),
                tool_name=_require_string(item, "name", item_type),
                arguments_delta=_require_string(item, "arguments", item_type),
            )

    provider_state = {
        "schema_version": 1,
        "response_id": response_id,
        "status": status,
        "output": output_items,
        "incomplete_details": incomplete_details,
        "compatibility_id": compatibility_id,
    }
    _ensure_json_safe(
        provider_state,
        message="OpenAI Responses terminal state is not JSON-safe",
        code="invalid_terminal_response",
    )
    metadata: dict[str, Any] = {
        "provider": _PROVIDER,
        "model": model,
        "refusal": refusal,
        "openai_responses": provider_state,
    }
    usage = response.get("usage")
    if usage is not None:
        if not isinstance(usage, dict):
            raise _protocol_error(
                "OpenAI Responses usage must be an object",
                code="invalid_terminal_response",
            )
        _ensure_json_safe(
            usage,
            message="OpenAI Responses usage is not JSON-safe",
            code="invalid_terminal_response",
        )
        metadata["usage"] = usage
    return builder.build(
        stop_reason=(
            "length" if incomplete else "tool_use" if has_tool_call else "stop"
        ),
        metadata=metadata,
    )


def _require_mapping(
    value: dict[str, Any],
    key: str,
    event_type: object,
) -> dict[str, Any]:
    result = value.get(key)
    if not isinstance(result, dict):
        raise _protocol_error(
            f"{event_type!s} field '{key}' must be an object",
            code="invalid_stream_event",
        )
    return result


def _require_string(
    value: dict[str, Any],
    key: str,
    event_type: object,
) -> str:
    result = value.get(key)
    if not isinstance(result, str):
        raise _protocol_error(
            f"{event_type!s} field '{key}' must be a string",
            code="invalid_stream_event",
        )
    return result


def _require_int(
    value: dict[str, Any],
    key: str,
    event_type: object,
) -> int:
    result = value.get(key)
    if not isinstance(result, int) or isinstance(result, bool):
        raise _protocol_error(
            f"{event_type!s} field '{key}' must be an integer",
            code="invalid_stream_event",
        )
    return result


def _protocol_error(message: str, *, code: str) -> ModelRequestError:
    return ModelRequestError(
        message,
        kind="protocol",
        provider=_PROVIDER,
        code=code,
    )


def _ensure_json_safe(value: object, *, message: str, code: str) -> None:
    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise _protocol_error(message, code=code) from exc


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) else None


__all__ = ["OpenAICompatibleModel", "OpenAIResponsesModel"]
