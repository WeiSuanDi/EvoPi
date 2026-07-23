"""Provider-neutral tool definitions and execution results."""

from __future__ import annotations

import asyncio
import inspect
import json
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, TypeAlias

from evopi.core.types import JsonObject, Metadata
from evopi.core.cancellation import AbortSignal

class ToolValidationError(ValueError):
    """Raised when model-supplied arguments do not match a tool schema."""


@dataclass(slots=True, kw_only=True)
class ToolCall:
    id: str
    name: str
    arguments: JsonObject = field(default_factory=dict)


@dataclass(slots=True, kw_only=True)
class ToolResult:
    content: str
    is_error: bool = False
    # A batch-level hint: skip the next model call only when every final result agrees.
    terminate: bool = False
    metadata: Metadata = field(default_factory=dict)


ToolHandlerResult: TypeAlias = (
    ToolResult | str | int | float | bool | None | JsonObject | list[Any]
)
ToolHandler: TypeAlias = Callable[..., Awaitable[ToolHandlerResult] | ToolHandlerResult]


@dataclass(slots=True, kw_only=True)
class Tool:
    name: str
    description: str
    parameters: JsonObject
    handler: ToolHandler

    def definition(self) -> JsonObject:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    async def execute(
        self,
        arguments: JsonObject,
        *,
        signal: AbortSignal | None = None,
    ) -> ToolResult:
        try:
            _validate_arguments(self.name, self.parameters, arguments)
            if signal is not None and signal.aborted:
                return _aborted_result("Operation aborted before tool execution")
            value = self.handler(**arguments)
            if inspect.isawaitable(value):
                task = asyncio.ensure_future(value)
                if signal is None:
                    value = await task
                else:
                    abort_task = asyncio.create_task(signal.wait())
                    done, _ = await asyncio.wait(
                        {task, abort_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if abort_task in done:
                        if task.done():
                            value = await task
                            return _completed_after_abort(_normalize_result(value))
                        task.cancel()
                        with suppress(asyncio.CancelledError):
                            await task
                        return _aborted_result("Operation aborted during tool execution")
                    abort_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await abort_task
                    value = await task
            result = _normalize_result(value)
            if signal is not None and signal.aborted:
                return _completed_after_abort(result)
            return result
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:  # Tool failures belong in the model transcript.
            return ToolResult(content=f"{type(exc).__name__}: {exc}", is_error=True)


def _aborted_result(content: str) -> ToolResult:
    return ToolResult(
        content=content,
        is_error=True,
        metadata={"aborted": True},
    )


def _completed_after_abort(result: ToolResult) -> ToolResult:
    return ToolResult(
        content=result.content,
        is_error=True,
        terminate=False,
        metadata={
            **result.metadata,
            "aborted": True,
            "completed_after_abort": True,
        },
    )


def _normalize_result(value: ToolHandlerResult) -> ToolResult:
    if isinstance(value, ToolResult):
        return value
    if isinstance(value, str):
        return ToolResult(content=value)
    if value is None:
        return ToolResult(content="")
    if isinstance(value, (dict, list)):
        return ToolResult(content=json.dumps(value, ensure_ascii=False))
    return ToolResult(content=str(value))


def _validate_arguments(name: str, schema: JsonObject, arguments: JsonObject) -> None:
    if not isinstance(arguments, dict):
        raise ToolValidationError(f"Arguments for tool '{name}' must be an object")

    properties = schema.get("properties", {})
    required = schema.get("required", [])
    missing = [key for key in required if key not in arguments]
    if missing:
        raise ToolValidationError(
            f"Missing required arguments for tool '{name}': {', '.join(missing)}"
        )

    if schema.get("additionalProperties") is False:
        unexpected = [key for key in arguments if key not in properties]
        if unexpected:
            raise ToolValidationError(
                f"Unexpected arguments for tool '{name}': {', '.join(unexpected)}"
            )

    expected_python_types: dict[str, type[Any] | tuple[type[Any], ...]] = {
        "string": str,
        "integer": int,
        "number": (int, float),
        "boolean": bool,
        "object": dict,
        "array": list,
    }
    for key, value in arguments.items():
        spec = properties.get(key, {})
        expected = expected_python_types.get(spec.get("type"))
        if expected is not None and not isinstance(value, expected):
            raise ToolValidationError(
                f"Argument '{key}' for tool '{name}' must be {spec['type']}"
            )


__all__ = ["Tool", "ToolCall", "ToolResult", "ToolValidationError"]
