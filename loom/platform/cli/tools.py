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
import re
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from loom.core.harness.middleware import ToolCall, ToolResult
from loom.core.harness.permissions import TrustLevel

if TYPE_CHECKING:
    from loom.core.memory.relational import RelationalMemory
    from loom.core.memory.search import MemorySearch
    from loom.core.memory.semantic import SemanticMemory

_WEB_TIMEOUT = 10.0       # seconds for all HTTP calls
_CONTENT_LIMIT = 2000     # max chars returned to agent
_SEARCH_RESULTS = 5       # default Brave results


def _html_to_text(html: str) -> tuple[str, str]:
    """Extract title and clean body text from raw HTML."""
    # Extract title
    title_match = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    title = re.sub(r"\s+", " ", title_match.group(1)).strip() if title_match else ""

    # Remove script/style/nav/header/footer blocks
    cleaned = re.sub(r"<(script|style|nav|header|footer|aside)[^>]*>.*?</\1>",
                     "", html, flags=re.I | re.S)
    # Strip remaining tags
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    # Decode common HTML entities
    for entity, char in [("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                         ("&quot;", '"'), ("&#39;", "'"), ("&nbsp;", " ")]:
        cleaned = cleaned.replace(entity, char)
    # Collapse whitespace
    cleaned = re.sub(r"\s{2,}", "\n", cleaned).strip()
    return title, cleaned


def _resolve_workspace_path(raw: str, workspace: Path) -> Path:
    """Resolve a raw path string relative to workspace.

    - Relative paths → workspace / raw
    - Absolute paths inside workspace → kept as-is
    - Absolute paths OUTSIDE workspace → re-rooted under workspace
      (strips leading / so the agent can't escape the sandbox)
    """
    p = Path(raw)
    if not p.is_absolute():
        return (workspace / p).resolve()
    resolved = p.resolve()
    try:
        resolved.relative_to(workspace)
        return resolved  # already inside workspace
    except ValueError:
        # Reroute: strip root and place under workspace
        # e.g. ~/tmp/foo.md → workspace/tmp/foo.md
        parts = resolved.parts[1:]  # drop drive/root
        return (workspace / Path(*parts)).resolve()


def make_filesystem_tools(workspace: Path) -> list["ToolDefinition"]:
    """Return read_file, write_file, list_dir tools bound to *workspace*."""

    async def _read_file(call: ToolCall) -> ToolResult:
        raw = call.args.get("path", "")
        path = _resolve_workspace_path(raw, workspace)
        try:
            content = path.read_text(encoding="utf-8")
            return ToolResult(call_id=call.id, tool_name=call.tool_name,
                              success=True, output=content)
        except Exception as exc:
            return ToolResult(call_id=call.id, tool_name=call.tool_name,
                              success=False, error=str(exc))

    async def _write_file(call: ToolCall) -> ToolResult:
        raw = call.args.get("path", "")
        content = call.args.get("content", "")
        path = _resolve_workspace_path(raw, workspace)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            return ToolResult(call_id=call.id, tool_name=call.tool_name,
                              success=True,
                              output=f"Written {len(content)} chars to {path}")
        except Exception as exc:
            return ToolResult(call_id=call.id, tool_name=call.tool_name,
                              success=False, error=str(exc))

    async def _list_dir(call: ToolCall) -> ToolResult:
        raw = call.args.get("path", "")
        path = _resolve_workspace_path(raw or ".", workspace)
        try:
            entries = sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name))
            lines = [
                f"{'[dir] ' if e.is_dir() else '      '}{e.name}"
                for e in entries
            ]
            header = f"[workspace: {workspace}]\n" if path == workspace else ""
            return ToolResult(call_id=call.id, tool_name=call.tool_name,
                              success=True, output=header + "\n".join(lines))
        except Exception as exc:
            return ToolResult(call_id=call.id, tool_name=call.tool_name,
                              success=False, error=str(exc))

    return [
        ToolDefinition(
            name="read_file",
            description=(
                "Read the contents of a file. Relative paths resolve inside the workspace. "
                "Use this to read code, configs, or documents."
            ),
            trust_level=TrustLevel.SAFE,
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string",
                             "description": "File path (relative to workspace or absolute)"},
                },
                "required": ["path"],
            },
            executor=_read_file,
            tags=["filesystem", "read"],
        ),
        ToolDefinition(
            name="write_file",
            description=(
                "Write content to a file inside the workspace. Creates directories as needed. "
                "Relative paths resolve inside the workspace."
            ),
            trust_level=TrustLevel.GUARDED,
            input_schema={
                "type": "object",
                "properties": {
                    "path":    {"type": "string",
                                "description": "Destination path (relative to workspace or absolute)"},
                    "content": {"type": "string", "description": "Text content to write"},
                },
                "required": ["path", "content"],
            },
            executor=_write_file,
            tags=["filesystem", "write"],
        ),
        ToolDefinition(
            name="list_dir",
            description=(
                "List files and directories. Defaults to workspace root. "
                "Relative paths resolve inside the workspace."
            ),
            trust_level=TrustLevel.SAFE,
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string",
                             "description": "Directory path (default: workspace root)"},
                },
                "required": [],
            },
            executor=_list_dir,
            tags=["filesystem", "read"],
        ),
    ]


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
                              success=False, error=f"Command timed out after {timeout}s",
                              failure_type="timeout")

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
        conflicted = await semantic.upsert(entry)

        if conflicted:
            msg = f"Memorized: {key!r} (overwrote previous value — history preserved)"
        else:
            msg = f"Memorized: {key!r}"
        return ToolResult(call_id=call.id, tool_name=call.tool_name,
                          success=True, output=msg)

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


def make_relate_tool(relational: "RelationalMemory") -> ToolDefinition:
    """
    Create a GUARDED ``relate`` tool bound to the given RelationalMemory instance.

    Stores a (subject, predicate, object) triple — e.g.
    relate(subject="user", predicate="prefers", object="concise responses").
    """
    from loom.core.memory.relational import RelationalEntry

    async def _relate(call: ToolCall) -> ToolResult:
        subject = call.args.get("subject", "").strip()
        predicate = call.args.get("predicate", "").strip()
        obj = call.args.get("object", "").strip()
        confidence = float(call.args.get("confidence", 1.0))
        confidence = max(0.0, min(1.0, confidence))

        if not subject or not predicate or not obj:
            return ToolResult(call_id=call.id, tool_name=call.tool_name,
                              success=False, error="'subject', 'predicate', and 'object' are required")

        entry = RelationalEntry(
            subject=subject,
            predicate=predicate,
            object=obj,
            confidence=confidence,
            source="agent",
        )
        await relational.upsert(entry)
        return ToolResult(call_id=call.id, tool_name=call.tool_name,
                          success=True, output=f"Stored: {subject!r} {predicate!r} {obj!r}")

    return ToolDefinition(
        name="relate",
        description=(
            "Store a relationship triple (subject, predicate, object) in relational memory. "
            "Use this to record durable facts about preferences, constraints, or associations. "
            "Example: relate(subject='user', predicate='prefers', object='concise answers'). "
            "Upserting with the same subject+predicate replaces the previous object."
        ),
        trust_level=TrustLevel.GUARDED,
        input_schema={
            "type": "object",
            "properties": {
                "subject":   {"type": "string", "description": "The entity (e.g. 'user', 'project:loom')"},
                "predicate": {"type": "string", "description": "The relationship (e.g. 'prefers', 'uses', 'avoids')"},
                "object":    {"type": "string", "description": "The value of the relationship"},
                "confidence": {"type": "number", "description": "Confidence 0–1 (default 1.0)"},
            },
            "required": ["subject", "predicate", "object"],
        },
        executor=_relate,
        tags=["memory", "write", "relational"],
    )


def make_query_relations_tool(relational: "RelationalMemory") -> ToolDefinition:
    """
    Create a SAFE ``query_relations`` tool bound to the given RelationalMemory instance.

    Returns all triples matching the given subject and/or predicate filters.
    """
    async def _query_relations(call: ToolCall) -> ToolResult:
        subject = call.args.get("subject", "").strip() or None
        predicate = call.args.get("predicate", "").strip() or None

        if not subject and not predicate:
            return ToolResult(call_id=call.id, tool_name=call.tool_name,
                              success=False, error="At least one of 'subject' or 'predicate' is required")

        entries = await relational.query(subject=subject, predicate=predicate)
        if not entries:
            return ToolResult(call_id=call.id, tool_name=call.tool_name,
                              success=True, output="No matching relationships found.")

        lines = [
            f"[{e.subject}] {e.predicate} → {e.object}  (conf: {e.confidence:.2f})"
            for e in entries
        ]
        return ToolResult(call_id=call.id, tool_name=call.tool_name,
                          success=True, output="\n".join(lines))

    return ToolDefinition(
        name="query_relations",
        description=(
            "Query the relational memory store for (subject, predicate, object) triples. "
            "Filter by subject to get all known facts about an entity, or by predicate "
            "to find all entities with that relationship. "
            "Example: query_relations(subject='user') returns all user preferences."
        ),
        trust_level=TrustLevel.SAFE,
        input_schema={
            "type": "object",
            "properties": {
                "subject":   {"type": "string", "description": "Filter by subject entity"},
                "predicate": {"type": "string", "description": "Filter by predicate (relationship type)"},
            },
        },
        executor=_query_relations,
        tags=["memory", "search", "relational"],
    )


def make_fetch_url_tool() -> ToolDefinition:
    """Return a SAFE tool that fetches a URL and returns cleaned text."""

    async def _fetch_url(call: ToolCall) -> ToolResult:
        url = call.args.get("url", "").strip()
        if not url:
            return ToolResult(call_id=call.id, tool_name=call.tool_name,
                              success=False, error="'url' argument is required")
        try:
            async with httpx.AsyncClient(follow_redirects=True,
                                         timeout=_WEB_TIMEOUT) as client:
                resp = await client.get(url, headers={"User-Agent": "Loom/0.3"})
                resp.raise_for_status()
                content_type = resp.headers.get("content-type", "")
                if "html" in content_type:
                    title, body = _html_to_text(resp.text)
                    body = body[:_CONTENT_LIMIT]
                    output = f"Title: {title}\n\n{body}" if title else body
                else:
                    output = resp.text[:_CONTENT_LIMIT]
        except httpx.HTTPStatusError as exc:
            return ToolResult(call_id=call.id, tool_name=call.tool_name,
                              success=False, error=f"HTTP {exc.response.status_code}: {url}")
        except Exception as exc:
            return ToolResult(call_id=call.id, tool_name=call.tool_name,
                              success=False, error=str(exc))
        return ToolResult(call_id=call.id, tool_name=call.tool_name,
                          success=True, output=output)

    return ToolDefinition(
        name="fetch_url",
        description=(
            "Fetch a URL and return the page title and cleaned body text (scripts/styles removed). "
            "Use this to read web pages, documentation, or articles. "
            "Output is truncated to 2000 chars."
        ),
        trust_level=TrustLevel.SAFE,
        input_schema={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Full URL to fetch (http/https)"},
            },
            "required": ["url"],
        },
        executor=_fetch_url,
        tags=["web", "fetch"],
    )


def make_web_search_tool(brave_api_key: str) -> ToolDefinition:
    """Return a GUARDED tool that searches the web via Brave Search API."""

    async def _web_search(call: ToolCall) -> ToolResult:
        query = call.args.get("query", "").strip()
        count = min(max(int(call.args.get("count", _SEARCH_RESULTS)), 1), 10)
        if not query:
            return ToolResult(call_id=call.id, tool_name=call.tool_name,
                              success=False, error="'query' argument is required")
        try:
            async with httpx.AsyncClient(timeout=_WEB_TIMEOUT) as client:
                resp = await client.get(
                    "https://api.search.brave.com/res/v1/web/search",
                    params={"q": query, "count": count},
                    headers={
                        "Accept": "application/json",
                        "Accept-Encoding": "gzip",
                        "X-Subscription-Token": brave_api_key,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPStatusError as exc:
            return ToolResult(call_id=call.id, tool_name=call.tool_name,
                              success=False,
                              error=f"Brave API error {exc.response.status_code}")
        except Exception as exc:
            return ToolResult(call_id=call.id, tool_name=call.tool_name,
                              success=False, error=str(exc))

        results = data.get("web", {}).get("results", [])
        if not results:
            return ToolResult(call_id=call.id, tool_name=call.tool_name,
                              success=True, output="No results found.")

        lines: list[str] = []
        for i, r in enumerate(results, 1):
            title = r.get("title", "")
            url = r.get("url", "")
            desc = re.sub(r"\s+", " ", r.get("description", "")).strip()
            lines.append(f"{i}. {title}\n   {url}\n   {desc}")
        output = "\n\n".join(lines)
        return ToolResult(call_id=call.id, tool_name=call.tool_name,
                          success=True, output=output)

    return ToolDefinition(
        name="web_search",
        description=(
            "Search the web via Brave Search and return top results with titles, URLs, and descriptions. "
            "Use this to find current information, documentation, or answers that aren't in memory."
        ),
        trust_level=TrustLevel.GUARDED,
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "count": {"type": "integer",
                          "description": "Number of results to return (1–10, default 5)"},
            },
            "required": ["query"],
        },
        executor=_web_search,
        tags=["web", "search"],
    )


# run_bash is workspace-independent (spawns a shell in CWD, which is fine)
# read_file / write_file / list_dir are registered via make_filesystem_tools(workspace)
# in LoomSession.start() so they close over the correct workspace path.
BUILTIN_TOOLS: list[ToolDefinition] = [
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
