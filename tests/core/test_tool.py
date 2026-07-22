import asyncio

from evopi.core.tool import Tool, ToolResult


def test_tool_executes_async_handler_and_normalizes_text() -> None:
    async def greet(name: str) -> str:
        return f"hello {name}"

    tool = Tool(
        name="greet",
        description="Greet someone",
        parameters={
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
            "additionalProperties": False,
        },
        handler=greet,
    )

    result = asyncio.run(tool.execute({"name": "EvoPi"}))

    assert result == ToolResult(content="hello EvoPi")


def test_tool_validation_failure_becomes_error_result() -> None:
    tool = Tool(
        name="greet",
        description="Greet someone",
        parameters={
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
        handler=lambda name: name,
    )

    result = asyncio.run(tool.execute({}))

    assert result.is_error is True
    assert "Missing required arguments" in result.content
