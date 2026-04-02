"""
Built-in tools registered for the CLI platform.

These are intentionally simple and cover the most common needs:
read_file, write_file, list_dir, run_bash.

Phase 4B adds memory tools (recall, memorize) via factory functions
that close over the live memory stores. Register them in LoomSession.start().

Each tool is an async function that accepts a ToolCall and returns a ToolResult.
The actual registration happens in main.py via ToolRegistry.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from loom.core.harness.middleware import ToolCall, ToolResult
from loom.core.harness.permissions import TrustLevel

if TYPE_CHECKING:
    from loom.core.memory.search import MemorySearch
    from loom.core.memory.semantic import SemanticMemory


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

# ------------------------------------------------------------------
# Memory tool factories (Phase 4B)
# Close over live memory store instances; call from LoomSession.start().
# ------------------------------------------------------------------

def make_recall_tool(search: "MemorySearch") -> ToolDefinition:
    """
    Create a SAFE ``recall`` tool bound to the given MemorySearch instance.

    The tool performs BM25-ranked retrieval across semantic facts and skills.
    """
    async def _recall(call: ToolCall) -> ToolResult:
        query = call.args.get("query", "").strip()
        mem_type = call.args.get("type", "all")
        limit = min(max(int(call.args.get("limit", 5)), 1), 10)

        if not query:
            return ToolResult(call_id=call.id, tool_name=call.tool_name,
                              success=False, error="'query' argument is required")

        if mem_type not in ("semantic", "skill", "all"):
            mem_type = "all"

        results = await search.recall(query, type=mem_type, limit=limit)  # type: ignore[arg-type]

        if not results:
            return ToolResult(call_id=call.id, tool_name=call.tool_name,
                              success=True, output="No memories stored yet.")

        # If all results are recency-fallback (BM25/embedding had no matches),
        # prepend a header so the agent knows they are unranked.
        if all(r.metadata.get("fallback") for r in results):
            header = f"[No keyword match — showing {len(results)} most recent entries]\n\n"
        else:
            header = ""

        lines = [r.format() for r in results]
        return ToolResult(call_id=call.id, tool_name=call.tool_name,
                          success=True, output=header + "\n\n".join(lines))

    return ToolDefinition(
        name="recall",
        description=(
            "Search long-term memory for relevant facts or skills using ranked retrieval. "
            "Call this before starting a task to surface related context. "
            "Results are ranked by relevance to the query."
        ),
        trust_level=TrustLevel.SAFE,
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural-language search query",
                },
                "type": {
                    "type": "string",
                    "enum": ["semantic", "skill", "all"],
                    "description": "Memory type to search (default: 'all')",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results to return (1–10, default 5)",
                },
            },
            "required": ["query"],
        },
        executor=_recall,
        tags=["memory", "search", "recall"],
    )


def make_memorize_tool(semantic: "SemanticMemory") -> ToolDefinition:
    """
    Create a GUARDED ``memorize`` tool bound to the given SemanticMemory instance.

    Stores a key→value fact in semantic memory for future sessions.
    """
    from loom.core.memory.semantic import SemanticEntry

    async def _memorize(call: ToolCall) -> ToolResult:
        key = call.args.get("key", "").strip()
        value = call.args.get("value", "").strip()
        confidence = float(call.args.get("confidence", 0.8))
        confidence = max(0.0, min(1.0, confidence))

        if not key or not value:
            return ToolResult(call_id=call.id, tool_name=call.tool_name,
                              success=False, error="Both 'key' and 'value' are required")

        entry = SemanticEntry(key=key, value=value, confidence=confidence, source="agent")
        await semantic.upsert(entry)

        return ToolResult(call_id=call.id, tool_name=call.tool_name,
                          success=True, output=f"Memorized: {key!r}")

    return ToolDefinition(
        name="memorize",
        description=(
            "Persist a fact to long-term semantic memory under a unique key. "
            "Use this to remember discoveries, decisions, or preferences that "
            "should survive across sessions."
        ),
        trust_level=TrustLevel.GUARDED,
        input_schema={
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": "Unique identifier for the fact (e.g. 'project:db_schema')",
                },
                "value": {
                    "type": "string",
                    "description": "The fact in natural language",
                },
                "confidence": {
                    "type": "number",
                    "description": "Confidence score 0–1 (default 0.8)",
                },
            },
            "required": ["key", "value"],
        },
        executor=_memorize,
        tags=["memory", "write", "memorize"],
    )


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
