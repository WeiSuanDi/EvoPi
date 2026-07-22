from pathlib import Path

from evopi.core.tool import Tool
from evopi.tools.builtins import (
    create_list_dir_tool,
    create_read_file_tool,
    create_shell_command_tool,
    create_write_file_tool,
)


def coding_tools(workspace: str | Path) -> list[Tool]:
    return [
        create_list_dir_tool(workspace),
        create_read_file_tool(workspace),
        create_write_file_tool(workspace),
        create_shell_command_tool(workspace),
    ]


__all__ = ["coding_tools"]
