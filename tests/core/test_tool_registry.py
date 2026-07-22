import asyncio

from evopi.core.tool import Tool, ToolCall
from evopi.tools.builtins import (
    create_list_dir_tool,
    create_read_file_tool,
    create_write_file_tool,
)
from evopi.tools.executor import ToolExecutor
from evopi.tools.registry import ToolRegistry


def test_registry_rejects_duplicate_names() -> None:
    tool = Tool(name="echo", description="Echo", parameters={}, handler=lambda: "ok")
    registry = ToolRegistry([tool])

    try:
        registry.register(tool)
    except ValueError as exc:
        assert "already registered" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("duplicate tool registration should fail")


def test_builtin_file_tools_stay_inside_workspace(tmp_path) -> None:
    registry = ToolRegistry(
        [
            create_write_file_tool(tmp_path),
            create_read_file_tool(tmp_path),
            create_list_dir_tool(tmp_path),
        ]
    )
    executor = ToolExecutor(registry)

    write_result = asyncio.run(
        executor.execute(
            ToolCall(
                id="write-1",
                name="write_file",
                arguments={"path": "demo/hello.py", "content": "print('hello')\n"},
            )
        )
    )
    read_result = asyncio.run(
        executor.execute(
            ToolCall(
                id="read-1",
                name="read_file",
                arguments={"path": "demo/hello.py"},
            )
        )
    )
    list_result = asyncio.run(
        executor.execute(
            ToolCall(id="list-1", name="list_dir", arguments={"path": "demo"})
        )
    )
    escape_result = asyncio.run(
        executor.execute(
            ToolCall(id="read-2", name="read_file", arguments={"path": "../secret.txt"})
        )
    )

    assert write_result.is_error is False
    assert read_result.content == "print('hello')\n"
    assert list_result.content == "hello.py"
    assert escape_result.is_error is True
    assert "escapes workspace" in escape_result.content
