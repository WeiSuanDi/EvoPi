"""Streaming OpenAI-compatible Chat Completions adapter."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from evopi.core.cancellation import AbortSignal
from evopi.ai.api.base import iter_sse_data, raise_for_model_status
from evopi.ai.auth.resolve import resolve_api_key
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


class OpenAICompatibleModel:
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
    ) -> None:
        self.model = model
        self.api_key = resolve_api_key(api_key, "OPENAI_API_KEY")
        self.base_url = base_url.rstrip("/")
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.timeout = timeout
        self.headers = dict(headers or {})
        self._client = client

    @property
    def name(self) -> str:
        return self.model

    async def stream(
        self,
        context: AgentContext,
        *,
        signal: AbortSignal | None = None,
    ) -> AsyncIterator[ModelStreamEvent]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [_convert_message(message) for message in context.messages],
            "stream": True,
            "stream_options": {"include_usage": True},
            "max_tokens": self.max_tokens,
        }
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        if context.tools:
            payload["tools"] = [tool.definition() for tool in context.tools]

        headers = {"Authorization": f"Bearer {self.api_key}", **self.headers}
        client = self._client or httpx.AsyncClient(timeout=self.timeout)
        owns_client = self._client is None
        builder = AssistantMessageBuilder()
        finish_reason = "stop"
        try:
            async with client.stream(
                "POST",
                f"{self.base_url}/chat/completions",
                headers=headers,
                json=payload,
            ) as response:
                await raise_for_model_status(response)
                async for chunk in iter_sse_data(response):
                    if signal is not None and signal.aborted:
                        return
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    choice = choices[0]
                    delta = choice.get("delta") or {}
                    text = delta.get("content")
                    if text:
                        builder.add_text(text)
                        yield TextDelta(delta=text)

                    for part in delta.get("tool_calls") or []:
                        index = int(part.get("index", 0))
                        function = part.get("function") or {}
                        args_delta = function.get("arguments") or ""
                        builder.add_tool_call_delta(
                            index=index,
                            arguments_delta=args_delta,
                            tool_call_id=part.get("id"),
                            tool_name=function.get("name"),
                        )
                        yield ToolCallDelta(
                            index=index,
                            arguments_delta=args_delta,
                            tool_call_id=part.get("id"),
                            tool_name=function.get("name"),
                        )
                    if choice.get("finish_reason"):
                        finish_reason = choice["finish_reason"]
        finally:
            if owns_client:
                await client.aclose()

        stop_map: dict[str, StopReason] = {
            "tool_calls": "tool_use",
            "length": "length",
            "stop": "stop",
        }
        yield ModelComplete(
            message=builder.build(
                stop_reason=stop_map.get(finish_reason, "stop"),
                metadata={"provider": "openai-compatible", "model": self.model},
            )
        )


def _convert_message(message: object) -> dict[str, Any]:
    if isinstance(message, SystemMessage):
        return {"role": "system", "content": message.content}
    if isinstance(message, UserMessage):
        return {"role": "user", "content": message.content}
    if isinstance(message, AssistantMessage):
        value: dict[str, Any] = {"role": "assistant", "content": message.content or None}
        if message.tool_calls:
            value["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments, ensure_ascii=False),
                    },
                }
                for call in message.tool_calls
            ]
        return value
    if isinstance(message, ToolResultMessage):
        return {
            "role": "tool",
            "tool_call_id": message.tool_call_id,
            "content": message.content,
        }
    raise TypeError(f"Unsupported message: {type(message).__name__}")


__all__ = ["OpenAICompatibleModel"]
