"""
Built-in tools registered for the CLI platform.

These are intentionally simple and cover the most common needs:
read_file, write_file, list_dir, run_bash.

Each tool is an async function that accepts a ToolCall and returns a ToolResult.
The actual registration happens in main.py via ToolRegistry.
"""

import asyncio
import subprocess
from pathlib import Path

from loom.core.harness.middleware import ToolCall, ToolResult
from loom.core.harness.permissions import TrustLevel


async def _read_file(call: ToolCall) -> ToolResult:
    path = Path(call.args.get("path", ""))
    try:
        content = path.read_text(encoding="utf-8")
        return ToolResult(call_id=call.id, tool_name=call.tool_name,
                          success=True, output=content)
    except Exception as exc:
        return ToolResult(call_id=call.id, tool_name=call.tool_name,
                          success=False, error=str(exc))


async def _write_file(call: ToolCall) -> ToolResult:
    path = Path(call.args.get("path", ""))
    content = call.args.get("content", "")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return ToolResult(call_id=call.id, tool_name=call.tool_name,
                          success=True, output=f"Written {len(content)} chars to {path}")
    except Exception as exc:
        return ToolResult(call_id=call.id, tool_name=call.tool_name,
                          success=False, error=str(exc))


async def _list_dir(call: ToolCall) -> ToolResult:
    path = Path(call.args.get("path", "."))
    try:
        entries = sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name))
        lines = [
            f"{'[dir] ' if e.is_dir() else '      '}{e.name}"
            for e in entries
        ]
        return ToolResult(call_id=call.id, tool_name=call.tool_name,
                          success=True, output="\n".join(lines))
    except Exception as exc:
        return ToolResult(call_id=call.id, tool_name=call.tool_name,
                          success=False, error=str(exc))


async def _run_bash(call: ToolCall) -> ToolResult:
    command = call.args.get("command", "")
    timeout = call.args.get("timeout", 30)
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return ToolResult(call_id=call.id, tool_name=call.tool_name,
                              success=False, error=f"Command timed out after {timeout}s")

        output = stdout.decode("utf-8", errors="replace")
        success = proc.returncode == 0
        return ToolResult(
            call_id=call.id, tool_name=call.tool_name,
            success=success, output=output,
            error=None if success else f"Exit code {proc.returncode}",
            metadata={"exit_code": proc.returncode},
        )
    except Exception as exc:
        return ToolResult(call_id=call.id, tool_name=call.tool_name,
                          success=False, error=str(exc))


# ------------------------------------------------------------------
# Tool definitions ready to be registered
# ------------------------------------------------------------------

from loom.core.harness.registry import ToolDefinition

BUILTIN_TOOLS: list[ToolDefinition] = [
    ToolDefinition(
        name="read_file",
        description="Read the contents of a file at the given path.",
        trust_level=TrustLevel.SAFE,
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute or relative file path"},
            },
            "required": ["path"],
        },
        executor=_read_file,
        tags=["filesystem", "read"],
    ),
    ToolDefinition(
        name="write_file",
        description="Write content to a file. Creates directories as needed.",
        trust_level=TrustLevel.GUARDED,
        input_schema={
            "type": "object",
            "properties": {
                "path":    {"type": "string", "description": "Destination file path"},
                "content": {"type": "string", "description": "Text content to write"},
            },
            "required": ["path", "content"],
        },
        executor=_write_file,
        tags=["filesystem", "write"],
    ),
    ToolDefinition(
        name="list_dir",
        description="List files and directories at the given path.",
        trust_level=TrustLevel.SAFE,
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory path (default: current dir)"},
            },
            "required": [],
        },
        executor=_list_dir,
        tags=["filesystem", "read"],
    ),
    ToolDefinition(
        name="run_bash",
        description="Execute a shell command and return stdout/stderr.",
        trust_level=TrustLevel.GUARDED,
        input_schema={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to run"},
                "timeout": {"type": "integer", "description": "Timeout in seconds (default 30)"},
            },
            "required": ["command"],
        },
        executor=_run_bash,
        tags=["shell"],
    ),
]
