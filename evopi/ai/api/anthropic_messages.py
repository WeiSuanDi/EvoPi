"""Streaming Anthropic Messages API adapter."""

from __future__ import annotations

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
from evopi.ai.auth.resolve import resolve_api_key
from evopi.core.cancellation import AbortSignal
from evopi.core.model_errors import ModelError
from evopi.core.context import AgentContext
from evopi.core.messages import (
    AssistantMessage,
    StopReason,
    SystemMessage,
    ToolResultMessage,
    UserMessage,
)
from evopi.core.stream import (
    AssistantMessageBuilder,
    ModelComplete,
    ModelStreamEvent,
    TextDelta,
    ToolCallDelta,
)


class AnthropicMessagesModel:
    def __init__(
        self,
        *,
        model: str,
        api_key: str | None = None,
        base_url: str = "https://api.anthropic.com",
        max_tokens: int = 4096,
        temperature: float | None = None,
        timeout: float = 120.0,
        headers: dict[str, str] | None = None,
        client: httpx.AsyncClient | None = None,
        context_window: int = 0,
    ) -> None:
        self.model = model
        self.api_key = resolve_api_key(api_key, "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY")
        self.base_url = base_url.rstrip("/")
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.timeout = timeout
        self.headers = dict(headers or {})
        self._context_window = context_window
        self._client = client

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
            "messages": _convert_messages(context),
            "max_tokens": self.max_tokens,
            "stream": True,
        }
        if system:
            payload["system"] = system
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        if context.tools:
            payload["tools"] = [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.parameters,
                }
                for tool in context.tools
            ]

        headers = {
            "x-api-key": self.api_key,
            "Authorization": f"Bearer {self.api_key}",
            "anthropic-version": "2023-06-01",
            **self.headers,
        }
        client = self._client or httpx.AsyncClient(timeout=self.timeout)
        owns_client = self._client is None
        builder = AssistantMessageBuilder()
        stop_reason = "stop"
        completed = False
        try:
            try:
                async with client.stream(
                    "POST",
                    f"{self.base_url}/v1/messages",
                    headers=headers,
                    json=payload,
                    timeout=self.timeout,
                ) as response:
                    await raise_for_model_status(response, provider="anthropic")
                    async for event in iter_sse_data(response, provider="anthropic"):
                        if signal is not None and signal.aborted:
                            return
                        event_type = event.get("type")
                        if event_type == "error":
                            error = event.get("error")
                            payload_error = error if isinstance(error, dict) else event
                            raise model_error_from_payload(
                                payload_error,
                                provider="anthropic",
                                retry_after=parse_retry_after(
                                    response.headers.get("retry-after")
                                ),
                                request_id=response.headers.get("request-id"),
                            )
                        if event_type == "content_block_start":
                            index = int(event.get("index", 0))
                            block = event.get("content_block") or {}
                            if block.get("type") == "tool_use":
                                builder.add_tool_call_delta(
                                    index=index,
                                    tool_call_id=block.get("id"),
                                    tool_name=block.get("name"),
                                )
                                yield ToolCallDelta(
                                    index=index,
                                    tool_call_id=block.get("id"),
                                    tool_name=block.get("name"),
                                )
                        elif event_type == "content_block_delta":
                            index = int(event.get("index", 0))
                            delta = event.get("delta") or {}
                            if delta.get("type") == "text_delta":
                                text = delta.get("text", "")
                                builder.add_text(text)
                                yield TextDelta(delta=text)
                            elif delta.get("type") == "input_json_delta":
                                value = delta.get("partial_json", "")
                                builder.add_tool_call_delta(index=index, arguments_delta=value)
                                yield ToolCallDelta(
                                    index=index,
                                    arguments_delta=value,
                                )
                        elif event_type == "message_delta":
                            stop_reason = (
                                (event.get("delta") or {}).get("stop_reason")
                                or stop_reason
                            )
                        elif event_type == "message_stop":
                            completed = True
            except ModelError:
                raise
            except Exception as exc:
                raise normalize_model_exception(exc, provider="anthropic") from exc
        finally:
            if owns_client:
                await client.aclose()

        if not completed:
            raise ModelRequestError(
                "Anthropic stream ended without message_stop",
                kind="connection",
                provider="anthropic",
                code="premature_stream_eof",
            )

        stop_map: dict[str, StopReason] = {
            "tool_use": "tool_use",
            "max_tokens": "length",
            "end_turn": "stop",
        }
        yield ModelComplete(
            message=builder.build(
                stop_reason=stop_map.get(stop_reason, "stop"),
                metadata={"provider": "anthropic", "model": self.model},
            )
        )


def _convert_messages(context: AgentContext) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    for message in context.messages:
        if isinstance(message, SystemMessage):
            continue
        if isinstance(message, UserMessage):
            converted.append({"role": "user", "content": message.content})
        elif isinstance(message, AssistantMessage):
            content: list[dict[str, Any]] = []
            if message.content:
                content.append({"type": "text", "text": message.content})
            content.extend(
                {
                    "type": "tool_use",
                    "id": call.id,
                    "name": call.name,
                    "input": call.arguments,
                }
                for call in message.tool_calls
            )
            converted.append({"role": "assistant", "content": content})
        elif isinstance(message, ToolResultMessage):
            block = {
                "type": "tool_result",
                "tool_use_id": message.tool_call_id,
                "content": message.content,
                "is_error": message.is_error,
            }
            if converted and converted[-1]["role"] == "user" and isinstance(
                converted[-1]["content"], list
            ):
                converted[-1]["content"].append(block)
            else:
                converted.append({"role": "user", "content": [block]})
    return converted


__all__ = ["AnthropicMessagesModel"]
