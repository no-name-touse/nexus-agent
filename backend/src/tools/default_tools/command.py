"""Command launch and follow-up tools."""

from ..base import Tool
from ..command import WorkspaceCommand
from .schema import object_schema


def _options(wait: int) -> dict:
    return {
        "yield_time_ms": {
            "type": "integer",
            "minimum": 0,
            "maximum": 300000,
            "default": wait,
            "description": "Milliseconds to wait, without terminating the command when this wait ends.",
        },
        "max_output_tokens": {
            "type": "integer",
            "minimum": 1,
            "default": 2000,
            "description": "Token budget for returned output; excess middle content is omitted.",
        },
    }


def command_tool(commands: WorkspaceCommand) -> Tool:
    environment = commands.execution_environment()
    return Tool(
        "run_command",
        "Start a workspace command. Return current output and session_id if still running; follow with write_stdin. "
        f"Terminal: {environment['terminal_type']}; host OS: {environment['host_os']}; "
        f"executable: {environment['executable']} ({environment['executable_resolution']}); "
        f"command cwd: {environment['cwd']}. Executable resolution does not guarantee sandbox admission.",
        commands.run,
        object_schema(
            {
                "cmd": {"type": "string", "description": environment["syntax_guidance"]},
                **_options(10000),
            },
            ["cmd"],
        ),
        requires_confirmation=True,
        read_only=False,
        workspace_confined=True,
        context_handler=commands.run_with_context,
        execution_environment=environment,
    )


def stdin_tool(commands: WorkspaceCommand) -> Tool:
    return Tool(
        "write_stdin",
        "Read new command output with empty chars, send input, or send Ctrl+C. Sessions belong to the current Turn.",
        commands.write_stdin,
        object_schema(
            {
                "session_id": {
                    "type": "string",
                    "description": "Command session_id returned by run_command in the current Turn.",
                },
                "chars": {
                    "type": "string",
                    "default": "",
                    "description": "Input to send; empty reads only, and Ctrl+C interrupts the command.",
                },
                **_options(60000),
            },
            ["session_id"],
        ),
        requires_confirmation=True,
        read_only=False,
        workspace_confined=True,
        context_handler=commands.write_with_context,
    )
