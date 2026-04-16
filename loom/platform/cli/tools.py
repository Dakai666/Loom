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
import json
import re
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from loom.core.harness.middleware import ToolCall, ToolResult
from loom.core.harness.permissions import ToolCapability, TrustLevel
from loom.core.harness.scope import ScopeRequirement, ScopeRequest
from loom.core.security.self_termination_guard import SelfTerminationGuard
from loom.core.security.command_scanner import CommandScanner

import logging

_log = logging.getLogger(__name__)
_self_term_guard = SelfTerminationGuard()
_cmd_scanner = CommandScanner()

if TYPE_CHECKING:
    from collections.abc import Callable
    from loom.core.harness.skill_checks import SkillCheckManager
    from loom.core.memory.procedural import ProceduralMemory, SkillGenome
    from loom.core.memory.relational import RelationalMemory
    from loom.core.memory.search import MemorySearch
    from loom.core.memory.semantic import SemanticMemory
    from loom.core.memory.governance import MemoryGovernor
    from loom.core.memory.skill_outcome import SkillOutcomeTracker
    from loom.core.tasks.manager import TaskGraphManager

_WEB_TIMEOUT = 10.0       # seconds for all HTTP calls
_CONTENT_LIMIT = 2000     # max chars returned to agent
_SEARCH_RESULTS = 5       # default Brave results


async def _race_abort(coro, abort_signal):
    """
    Run *coro*, cancelling it if *abort_signal* fires first.

    Returns ``(result, aborted)``.  When ``aborted=True``, ``result`` is
    ``None`` and the caller should return an "aborted" ToolResult.
    Any exception raised by *coro* propagates normally.
    """
    if abort_signal is None:
        return await coro, False
    if abort_signal.is_set():
        coro.close()  # prevent "coroutine was never awaited" ResourceWarning
        return None, True
    task = asyncio.ensure_future(coro)
    wait_task = asyncio.ensure_future(abort_signal.wait())
    done, pending = await asyncio.wait(
        [task, wait_task], return_when=asyncio.FIRST_COMPLETED
    )
    for t in pending:
        t.cancel()
        try:
            await t
        except (asyncio.CancelledError, Exception):
            pass
    if wait_task in done and task not in done:
        return None, True
    return task.result(), False


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
    """Resolve a raw path string strictly relative to workspace.

    - Escaping paths (e.g. ../../Windows) are rerouted back inside workspace
      to safely contain prompt injection and path traversal attempts.
    """
    p = Path(raw)
    resolved = (workspace / p).resolve() if not p.is_absolute() else p.resolve()
    
    try:
        resolved.relative_to(workspace)
        return resolved  # Check passed, securely inside workspace
    except ValueError:
        # Reroute: strip root/drive and forcibly place under workspace
        # e.g. /etc/passwd → workspace/etc/passwd, C:\Windows → workspace\Windows
        parts = resolved.parts[1:]
        return (workspace / Path(*parts)).resolve()


# ------------------------------------------------------------------
# Scope resolvers (Issue #45 Phase B)
#
# Each resolver converts a ToolCall's arguments into a ScopeRequest
# so BlastRadiusMiddleware can do resource-level authorization.
#
# Review note: resolvers MUST return canonical paths (no .., no .)
# in ScopeRequirement.selector.  PathMatcher normalizes as safety net
# but resolvers should do this at source for clean diff displays.
# ------------------------------------------------------------------


def _make_write_file_resolver(workspace: Path):
    """Return a scope_resolver for write_file bound to *workspace*."""
    import os.path

    _workspace_resolved = workspace.resolve()

    def _resolve(call: ToolCall) -> ScopeRequest:
        raw = call.args.get("path", "")
        p = Path(raw)
        resolved = (workspace / p).resolve() if not p.is_absolute() else p.resolve()
        # Produce a workspace-relative selector so it matches scope grants
        # written as relative paths in loom.toml (e.g. "outputs/self_check").
        # Fall back to the absolute parent only for paths outside the workspace.
        try:
            selector = str(resolved.parent.relative_to(_workspace_resolved))
        except ValueError:
            selector = os.path.normpath(str(resolved.parent))
        return ScopeRequest(
            tool_name=call.tool_name,
            capabilities=call.capabilities,
            requirements=[
                ScopeRequirement(
                    resource="path",
                    action="write",
                    selector=selector,
                    tool_name=call.tool_name,
                    capabilities=call.capabilities,
                ),
            ],
        )
    return _resolve


def _make_run_bash_resolver(workspace: Path):
    """
    Return a scope_resolver for run_bash bound to *workspace*.

    Limitations (documented in Phase A plan):
    - Pipes, subshells, variable expansion → scope unknown → fallback to CONFIRM
    - Only does token-level path extraction (aligned with exec_escape_fn)
    """
    import os.path

    _SCOPE_UNKNOWN_PATTERNS = re.compile(r'[\|`]|\$\(|\$\{|\$[A-Za-z]|<<|&&|\|\|')

    def _resolve(call: ToolCall) -> ScopeRequest:
        command = call.args.get("command", "")

        # If command contains patterns we can't analyze, mark scope unknown
        if _SCOPE_UNKNOWN_PATTERNS.search(command):
            return ScopeRequest(
                tool_name=call.tool_name,
                capabilities=call.capabilities,
                requirements=[
                    ScopeRequirement(
                        resource="exec",
                        action="execute",
                        selector="workspace",
                        constraints={"scope_unknown": True},
                        tool_name=call.tool_name,
                        capabilities=call.capabilities,
                    ),
                ],
                metadata={"scope_unknown": True},
            )

        # Check for absolute paths outside workspace
        has_absolute = False
        for token in re.findall(
            r'(?:^|(?<=\s))[/\\][^\s;|&>\'\"]*|[A-Za-z]:[/\\][^\s;|&>\'\"]*',
            command,
        ):
            try:
                candidate = Path(token).resolve()
                candidate.relative_to(workspace)
            except (ValueError, Exception):
                has_absolute = True
                break

        return ScopeRequest(
            tool_name=call.tool_name,
            capabilities=call.capabilities,
            requirements=[
                ScopeRequirement(
                    resource="exec",
                    action="execute",
                    selector="workspace",
                    constraints={"has_absolute_paths": has_absolute},
                    tool_name=call.tool_name,
                    capabilities=call.capabilities,
                ),
            ],
        )
    return _resolve


def _fetch_url_resolver(call: ToolCall) -> ScopeRequest:
    """Scope resolver for fetch_url — extracts destination domain."""
    from urllib.parse import urlparse
    url = call.args.get("url", "").strip()
    try:
        domain = urlparse(url).netloc or url
    except Exception:
        domain = url
    return ScopeRequest(
        tool_name=call.tool_name,
        capabilities=call.capabilities,
        requirements=[
            ScopeRequirement(
                resource="network",
                action="connect",
                selector=domain,
                tool_name=call.tool_name,
                capabilities=call.capabilities,
            ),
        ],
    )


def _web_search_resolver(call: ToolCall) -> ScopeRequest:
    """Scope resolver for web_search — destination is always Brave API."""
    return ScopeRequest(
        tool_name=call.tool_name,
        capabilities=call.capabilities,
        requirements=[
            ScopeRequirement(
                resource="network",
                action="connect",
                selector="api.search.brave.com",
                tool_name=call.tool_name,
                capabilities=call.capabilities,
            ),
        ],
    )


def _spawn_agent_resolver(call: ToolCall) -> ScopeRequest:
    """Scope resolver for spawn_agent — tracks spawn budget."""
    return ScopeRequest(
        tool_name=call.tool_name,
        capabilities=call.capabilities,
        requirements=[
            ScopeRequirement(
                resource="agent",
                action="spawn",
                selector="default",
                constraints={"spawn_count": 1},
                tool_name=call.tool_name,
                capabilities=call.capabilities,
            ),
        ],
    )


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
            # Capture original content for rollback (Issue #42)
            original_content: str | None = None
            file_existed = path.exists()
            if file_existed:
                try:
                    original_content = path.read_text(encoding="utf-8")
                except Exception:
                    pass  # binary file or unreadable — rollback will delete
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            return ToolResult(
                call_id=call.id, tool_name=call.tool_name,
                success=True,
                output=f"Written {len(content)} chars to {path}",
                metadata={
                    "_original_content": original_content,
                    "_file_existed": file_existed,
                    "_resolved_path": str(path),
                },
            )
        except Exception as exc:
            return ToolResult(call_id=call.id, tool_name=call.tool_name,
                              success=False, error=str(exc))

    async def _write_file_rollback(call: ToolCall, result: ToolResult) -> ToolResult:
        """Restore original file content (or delete if newly created)."""
        resolved = result.metadata.get("_resolved_path", "")
        path = Path(resolved) if resolved else _resolve_workspace_path(
            call.args.get("path", ""), workspace
        )
        original = result.metadata.get("_original_content")
        existed = result.metadata.get("_file_existed", True)
        try:
            if not existed:
                path.unlink(missing_ok=True)
                msg = f"Rolled back: deleted newly created {path}"
            elif original is not None:
                path.write_text(original, encoding="utf-8")
                msg = f"Rolled back: restored original content of {path}"
            else:
                msg = f"Rollback: no original content captured for {path}"
            return ToolResult(call_id=call.id, tool_name="write_file",
                              success=True, output=msg)
        except Exception as exc:
            return ToolResult(call_id=call.id, tool_name="write_file",
                              success=False, error=f"Rollback failed: {exc}")

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
            impact_scope="filesystem",
        ),
        ToolDefinition(
            name="write_file",
            description=(
                "Write content to a file inside the workspace. Creates directories as needed. "
                "Relative paths resolve inside the workspace."
            ),
            trust_level=TrustLevel.GUARDED,
            capabilities=ToolCapability.MUTATES,
            input_schema={
                "type": "object",
                "properties": {
                    "path":    {"type": "string",
                                "description": "Destination path (relative to workspace or absolute)"},
                    "content": {"type": "string", "description": "Text content to write"},
                    "justification": {"type": "string", "description": "簡短說明為何在目前的脈絡下執行此工具是合理且必要的（給人類審核看）。"},
                },
                "required": ["path", "content", "justification"],
            },
            executor=_write_file,
            tags=["filesystem", "write"],
            impact_scope="filesystem",
            rollback_fn=_write_file_rollback,
            preconditions=["target directory must be writable"],
            scope_descriptions=["writes under requested workspace path"],
            scope_resolver=_make_write_file_resolver(workspace),
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
            impact_scope="filesystem",
        ),
    ]


# ------------------------------------------------------------------
# Tool definitions ready to be registered
# ------------------------------------------------------------------

from loom.core.harness.registry import ToolDefinition


def make_exec_escape_fn(workspace: Path):
    """
    Return an escape-detector callable for use with BlastRadiusMiddleware.

    The returned function inspects a run_bash ToolCall's `command` argument and
    returns True if any token looks like an absolute path that resolves outside
    *workspace*.  False positives are acceptable (they cause an extra confirm
    prompt); false negatives would silently bypass the sandbox.

    Only injected when strict_sandbox=True.  When strict_sandbox=False the
    detector is not wired — the user has explicitly opted out of workspace
    confinement, so there is nothing meaningful to check.
    """
    def _would_escape(call: "ToolCall") -> bool:
        command = call.args.get("command", "")
        # Match tokens that start with / or a Windows drive letter (C:\)
        for token in re.findall(r'(?:^|(?<=\s))[/\\][^\s;|&>\'\"]*|[A-Za-z]:[/\\][^\s;|&>\'\"]*', command):
            try:
                candidate = Path(token).resolve()
                candidate.relative_to(workspace)
                # Relative_to succeeded → inside workspace, fine
            except ValueError:
                return True   # resolves outside workspace
            except Exception:
                pass          # can't resolve — ignore
        return False
    return _would_escape


def make_run_bash_tool(workspace: Path, strict_sandbox: bool = False) -> ToolDefinition:
    """
    Return the ``run_bash`` tool definition.

    When *strict_sandbox* is True (set via ``[harness] strict_sandbox = true``
    in loom.toml), the subprocess is launched with ``cwd=workspace`` so that
    relative paths and shell builtins stay inside the project folder.  This
    does not prevent absolute-path escapes at the OS level — for full
    confinement use an OS sandbox (e.g. Docker).
    """
    async def _run_bash(call: ToolCall) -> ToolResult:
        command = call.args.get("command", "")

        # Issue #98: self-termination guard — before ANY other processing
        verdict = _self_term_guard.check(command)
        if verdict.verdict == "block":
            _log.warning("Self-termination guard blocked: %s", verdict.description)
            return ToolResult(
                call_id=call.id, tool_name=call.tool_name,
                success=False,
                error=f"[Security] Blocked by self-termination guard: {verdict.description}",
                failure_type="security_block",
            )
        if verdict.verdict == "warn":
            _log.warning("Self-termination guard warning: %s", verdict.description)

        # Issue #100: shell injection command scanner
        scan_verdict = _cmd_scanner.check(command)
        if scan_verdict.verdict == "block":
            _log.warning("Command scanner blocked: %s", scan_verdict.description)
            return ToolResult(
                call_id=call.id, tool_name=call.tool_name,
                success=False,
                error=f"[Security] Blocked by command scanner: {scan_verdict.description}",
                failure_type="security_block",
            )
        if scan_verdict.verdict == "warn":
            _log.warning("Command scanner warning: %s", scan_verdict.description)

        timeout = call.args.get("timeout", 30)
        cwd = str(workspace) if strict_sandbox else None
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=cwd,
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

    sandbox_note = " Shell is confined to the workspace directory." if strict_sandbox else ""
    return ToolDefinition(
        name="run_bash",
        description=f"Execute a shell command and return stdout/stderr.{sandbox_note}",
        trust_level=TrustLevel.GUARDED,
        capabilities=ToolCapability.EXEC,
        input_schema={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to run"},
                "timeout": {"type": "integer", "description": "Timeout in seconds (default 30)"},
                "justification": {"type": "string", "description": "簡短說明為何在目前的脈絡下執行此工具是合理且必要的（給人類審核看）。"},
            },
            "required": ["command", "justification"],
        },
        executor=_run_bash,
        tags=["shell"],
        impact_scope="shell",
        scope_descriptions=[
            "executes shell commands within workspace sandbox",
            "scope unknown for pipes, subshells, or variable expansion",
        ],
        scope_resolver=_make_run_bash_resolver(workspace),
    )


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
        impact_scope="memory",
    )


def make_memorize_tool(
    semantic: "SemanticMemory",
    governor: "MemoryGovernor | None" = None,
) -> ToolDefinition:
    """
    Create a GUARDED ``memorize`` tool bound to the given SemanticMemory instance.

    Stores a key→value fact in semantic memory for future sessions.
    When a MemoryGovernor is provided, writes go through the governance
    pipeline (trust classification + contradiction detection).
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

        # Agent-invoked memorize is treated as user_explicit trust
        entry = SemanticEntry(key=key, value=value, confidence=confidence, source="memorize")

        if governor is not None:
            gov_result = await governor.governed_upsert(entry)
            if not gov_result.written:
                msg = (
                    f"Memorize skipped for {key!r}: existing entry has higher trust "
                    f"(tier={gov_result.trust_tier}, contradictions={gov_result.contradictions_found})"
                )
                return ToolResult(call_id=call.id, tool_name=call.tool_name,
                                  success=True, output=msg)
            if gov_result.contradictions_found > 0:
                msg = (
                    f"Memorized: {key!r} (resolved {gov_result.contradictions_found} "
                    f"contradiction(s) — {gov_result.resolution})"
                )
            else:
                msg = f"Memorized: {key!r}"
        else:
            conflicted = await semantic.upsert(entry)
            if conflicted:
                msg = f"Memorized: {key!r} (overwrote previous value — history preserved)"
            else:
                msg = f"Memorized: {key!r}"

        return ToolResult(call_id=call.id, tool_name=call.tool_name,
                          success=True, output=msg,
                          metadata={"_memorized_key": key})

    async def _memorize_rollback(call: ToolCall, result: ToolResult) -> ToolResult:
        """Delete the key that was just memorized."""
        key = result.metadata.get("_memorized_key") or call.args.get("key", "").strip()
        if not key:
            return ToolResult(call_id=call.id, tool_name="memorize",
                              success=False, error="No key to rollback")
        try:
            await semantic.delete(key)
            return ToolResult(call_id=call.id, tool_name="memorize",
                              success=True, output=f"Rolled back: deleted key {key!r}")
        except Exception as exc:
            return ToolResult(call_id=call.id, tool_name="memorize",
                              success=False, error=f"Rollback failed: {exc}")

    return ToolDefinition(
        name="memorize",
        description=(
            "Persist a fact to long-term semantic memory under a unique key. "
            "Use this to remember discoveries, decisions, or preferences that "
            "should survive across sessions."
        ),
        trust_level=TrustLevel.GUARDED,
        capabilities=ToolCapability.MUTATES,
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
        impact_scope="memory",
        rollback_fn=_memorize_rollback,
    )


def make_memory_health_tool(governor: "MemoryGovernor") -> ToolDefinition:
    """
    Create a SAFE ``memory_health`` tool for agent self-diagnosis.

    Returns the current and recent-historical health status of all
    memory subsystems so the agent can detect and report issues.
    """
    async def _memory_health(call: ToolCall) -> ToolResult:
        report = governor.health.report()
        summary = report.render_summary()
        return ToolResult(
            call_id=call.id,
            tool_name=call.tool_name,
            success=True,
            output=summary,
        )

    return ToolDefinition(
        name="memory_health",
        description=(
            "Check the health of your memory subsystems. Shows success/failure "
            "rates for embedding writes, semantic search, session compression, "
            "and other memory operations. Use this to self-diagnose when you "
            "suspect memory issues, or periodically to ensure memories are "
            "being saved correctly."
        ),
        trust_level=TrustLevel.SAFE,
        input_schema={
            "type": "object",
            "properties": {},
        },
        executor=_memory_health,
        tags=["memory", "health", "diagnostic"],
        impact_scope="memory",
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
                          success=True, output=f"Stored: {subject!r} {predicate!r} {obj!r}",
                          metadata={"_subject": subject, "_predicate": predicate})

    async def _relate_rollback(call: ToolCall, result: ToolResult) -> ToolResult:
        """Delete the triple that was just stored."""
        subject = result.metadata.get("_subject") or call.args.get("subject", "").strip()
        predicate = result.metadata.get("_predicate") or call.args.get("predicate", "").strip()
        if not subject or not predicate:
            return ToolResult(call_id=call.id, tool_name="relate",
                              success=False, error="No subject/predicate to rollback")
        try:
            await relational.delete(subject, predicate)
            return ToolResult(call_id=call.id, tool_name="relate",
                              success=True,
                              output=f"Rolled back: deleted ({subject!r}, {predicate!r})")
        except Exception as exc:
            return ToolResult(call_id=call.id, tool_name="relate",
                              success=False, error=f"Rollback failed: {exc}")

    return ToolDefinition(
        name="relate",
        description=(
            "Store a relationship triple (subject, predicate, object) in relational memory. "
            "Use this to record durable facts about preferences, constraints, or associations. "
            "Example: relate(subject='user', predicate='prefers', object='concise answers'). "
            "Upserting with the same subject+predicate replaces the previous object."
        ),
        trust_level=TrustLevel.GUARDED,
        capabilities=ToolCapability.MUTATES,
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
        impact_scope="memory",
        rollback_fn=_relate_rollback,
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
        impact_scope="memory",
    )


# ------------------------------------------------------------------
# Skill loading tool (Issue #56 — Agent Skills spec Tier 2)
# ------------------------------------------------------------------

def make_load_skill_tool(
    procedural: "ProceduralMemory",
    skills_dirs: list[Path] | None = None,
    outcome_tracker: "SkillOutcomeTracker | None" = None,
    semantic: "SemanticMemory | None" = None,
    turn_index_fn: "Callable[[], int] | None" = None,
    skill_check_manager: "SkillCheckManager | None" = None,
    relational: "RelationalMemory | None" = None,
    confirm_fn: "Callable | None" = None,
) -> ToolDefinition:
    """
    Create a SAFE ``load_skill`` tool that loads full skill instructions.

    Implements the Agent Skills spec Tier 2: on-demand activation.
    - Returns the full SKILL.md body wrapped in ``<skill_content>`` XML
    - Lists bundled resources (scripts/, references/, assets/)
    - Attaches evolution hints if available
    - Deduplicates: returns a short note on second activation in same session
    - Issue #64 Phase B: mounts skill-declared precondition checks
    """
    from loom.core.memory.procedural import ProceduralMemory  # type: ignore[attr-defined]

    # Per-session dedup tracking (set of already-loaded skill names)
    _loaded_this_session: set[str] = set()
    _dirs = skills_dirs or []

    async def _load_skill(call: ToolCall) -> ToolResult:
        name = call.args.get("name", "").strip()
        keep_existing = call.args.get("keep_existing", False)
        if not name:
            return ToolResult(
                call_id=call.id, tool_name=call.tool_name,
                success=False, error="'name' argument is required",
            )

        # Dedup check
        if name in _loaded_this_session:
            return ToolResult(
                call_id=call.id, tool_name=call.tool_name,
                success=True,
                output=f"Skill '{name}' is already loaded in this session. "
                       f"Refer to the previously loaded instructions.",
            )

        # Try ProceduralMemory first
        skill = await procedural.get(name)
        if skill is None:
            # Also try with underscores converted to hyphens and vice versa
            alt_name = name.replace("-", "_") if "-" in name else name.replace("_", "-")
            skill = await procedural.get(alt_name)
            if skill is not None:
                name = alt_name

        if skill is None:
            # List available skills to help
            active = await procedural.list_active()
            available = ", ".join(s.name for s in active[:10])
            return ToolResult(
                call_id=call.id, tool_name=call.tool_name,
                success=False,
                error=f"Skill '{name}' not found. Available: {available or '(none)'}",
            )

        # Build structured <skill_content> output
        body = skill.body or "(no instructions)"

        # Strip YAML frontmatter from body (it's metadata, already parsed)
        body = _strip_frontmatter(body)

        # Find skill directory and list bundled resources
        skill_dir, resources = _find_skill_resources(name, _dirs)

        lines = [f'<skill_content name="{name}">']

        # Evolution hints (from semantic memory, if available)
        evolution_hints = await _get_evolution_hints(procedural, semantic, name)
        if evolution_hints:
            lines.append("<evolution_hints>")
            for hint in evolution_hints:
                lines.append(f"  {hint}")
            lines.append("</evolution_hints>")
            lines.append("")

        lines.append(body)

        if skill_dir:
            lines.append("")
            lines.append(f"Skill directory: {skill_dir}")
            lines.append(
                "Relative paths in this skill are relative to the skill directory."
            )

        if resources:
            lines.append("")
            lines.append("<skill_resources>")
            for res in resources:
                lines.append(f"  <file>{res}</file>")
            lines.append("</skill_resources>")

        # Issue #64 Phase B: mount skill-declared precondition checks
        checks_summary = await _mount_skill_checks(
            name, skill, skill_dir, skill_check_manager, relational,
            keep_existing=keep_existing,
        )
        if checks_summary:
            lines.append("")
            lines.append("<mounted_precondition_checks>")
            for desc in checks_summary:
                lines.append(f"  {desc}")
            lines.append("</mounted_precondition_checks>")

        lines.append("</skill_content>")

        output = "\n".join(lines)

        # Record activation
        _loaded_this_session.add(name)
        if outcome_tracker is not None:
            _turn = turn_index_fn() if turn_index_fn else 0
            outcome_tracker.record_activation(name, _turn)

        return ToolResult(
            call_id=call.id, tool_name=call.tool_name,
            success=True, output=output,
            metadata={"skill_name": name, "skill_confidence": skill.confidence},
        )

    async def _mount_skill_checks(
        name: str,
        skill: "SkillGenome",
        skill_dir_str: str | None,
        manager: "SkillCheckManager | None",
        rel: "RelationalMemory | None",
        keep_existing: bool = False,
    ) -> list[str]:
        """Resolve and mount precondition checks for a skill. Returns descriptions."""
        if manager is None or not skill.precondition_check_refs:
            return []

        from loom.core.harness.skill_checks import SkillPreconditionRef, SkillCheckManager

        refs = [SkillPreconditionRef.from_dict(d) for d in skill.precondition_check_refs]

        # Approval gate: first-time approval via RelationalMemory
        rel_key = f"skill_checks:{name}"
        approved = False
        if rel is not None:
            entry = await rel.get(rel_key, "approved")
            approved = entry is not None and entry.object == "true"

        if not approved:
            # Build a description of what the skill wants to mount
            check_lines = []
            for ref in refs:
                tools_str = ", ".join(ref.applies_to)
                check_lines.append(f"  {ref.ref} → [{tools_str}]: {ref.description}")
            check_preview = "\n".join(check_lines)

            if confirm_fn is not None:
                # Use the platform-aware confirm callback (works on CLI, TUI, Discord)
                from loom.core.harness.middleware import ToolCall as _ToolCall
                from loom.core.harness.registry import TrustLevel
                synthetic_call = _ToolCall(
                    tool_name=f"load_skill({name})",
                    args={"action": "mount_precondition_checks", "checks": check_preview},
                    trust_level=TrustLevel.GUARDED,
                    session_id="",
                )
                try:
                    user_ok = await confirm_fn(synthetic_call)
                except (EOFError, KeyboardInterrupt):
                    user_ok = False
            else:
                user_ok = False

            if not user_ok:
                return []

            # Persist approval
            if rel is not None:
                from loom.core.memory.relational import RelationalEntry
                await rel.upsert(RelationalEntry(
                    subject=rel_key,
                    predicate="approved",
                    object="true",
                    source="user",
                ))

        # Resolve callables from skill directory
        if not skill_dir_str:
            return []

        skill_dir_path = Path(skill_dir_str)
        try:
            callables = SkillCheckManager.resolve_all(skill_dir_path, refs)
        except (FileNotFoundError, AttributeError, ImportError, ValueError) as exc:
            _log.warning("Failed to resolve checks for skill %r: %s", name, exc)
            return []

        # Mount
        return manager.mount(name, refs, callables, keep_existing=keep_existing)

    return ToolDefinition(
        name="load_skill",
        description=(
            "Load a skill's full instructions into context. Call this when a task "
            "matches a skill listed in <available_skills>. The skill's workflow, "
            "principles, and output format will be returned for you to follow."
        ),
        trust_level=TrustLevel.SAFE,
        input_schema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Name of the skill to load (from <available_skills>)",
                },
                "keep_existing": {
                    "type": "boolean",
                    "description": (
                        "If true, keep the previous skill's precondition checks "
                        "mounted alongside the new skill's. Default: false "
                        "(auto-unmount previous skill's checks)."
                    ),
                    "default": False,
                },
            },
            "required": ["name"],
        },
        executor=_load_skill,
        tags=["skill", "memory", "activation"],
        impact_scope="memory",
    )


def make_unload_skill_tool(
    skill_check_manager: "SkillCheckManager",
) -> ToolDefinition:
    """
    Create a SAFE ``unload_skill`` tool for explicit skill check removal.

    Issue #64 Phase B: allows the agent (or user) to manually unmount
    a skill's precondition checks without loading a replacement skill.
    """
    async def _unload_skill(call: ToolCall) -> ToolResult:
        name = call.args.get("name", "").strip()
        if not name:
            # No name → list currently mounted skills
            mounted = skill_check_manager.mounted_skills()
            if not mounted:
                return ToolResult(
                    call_id=call.id, tool_name=call.tool_name,
                    success=True,
                    output="No skill precondition checks are currently mounted.",
                )
            return ToolResult(
                call_id=call.id, tool_name=call.tool_name,
                success=True,
                output=f"Skills with mounted checks: {', '.join(mounted)}",
            )

        removed = skill_check_manager.unmount(name)
        return ToolResult(
            call_id=call.id, tool_name=call.tool_name,
            success=True,
            output=(
                f"Unmounted {removed} precondition check(s) for skill '{name}'."
                if removed > 0
                else f"Skill '{name}' had no mounted precondition checks."
            ),
        )

    return ToolDefinition(
        name="unload_skill",
        description=(
            "Remove a skill's precondition checks from the tool pipeline. "
            "Call with no name to list skills with mounted checks."
        ),
        trust_level=TrustLevel.SAFE,
        input_schema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Name of the skill to unload checks for",
                },
            },
        },
        executor=_unload_skill,
        tags=["skill", "memory"],
        impact_scope="memory",
    )


def _strip_frontmatter(body: str) -> str:
    """Remove YAML frontmatter (--- delimited) from skill body."""
    if not body.startswith("---"):
        return body
    parts = body.split("---", 2)
    if len(parts) >= 3:
        return parts[2].strip()
    return body


def _find_skill_resources(
    skill_name: str, skills_dirs: list[Path],
) -> tuple[str | None, list[str]]:
    """Locate skill directory and list bundled files (scripts/, references/, assets/)."""
    resource_dirs = ("scripts", "references", "assets")
    # Try both hyphenated and underscored variants
    variants = [skill_name, skill_name.replace("-", "_"), skill_name.replace("_", "-")]

    for base_dir in skills_dirs:
        if not base_dir.is_dir():
            continue
        for variant in variants:
            skill_dir = base_dir / variant
            if not skill_dir.is_dir():
                continue

            resources: list[str] = []
            for res_name in resource_dirs:
                res_dir = skill_dir / res_name
                if res_dir.is_dir():
                    for f in sorted(res_dir.rglob("*")):
                        if f.is_file():
                            resources.append(str(f.relative_to(skill_dir)))

            # Also list top-level non-SKILL.md files
            for f in sorted(skill_dir.iterdir()):
                if f.is_file() and f.name != "SKILL.md":
                    rel = str(f.relative_to(skill_dir))
                    if rel not in resources:
                        resources.append(rel)

            return str(skill_dir), resources

    return None, []


async def _get_evolution_hints(
    procedural: "ProceduralMemory",
    semantic: "SemanticMemory | None",
    skill_name: str,
) -> list[str]:
    """Fetch evolution hints for a skill.

    Reads real evolution hints written by ``SkillEvolutionHook`` from
    SemanticMemory (key pattern ``skill:<name>:evolution_hint:*``).
    Falls back to a confidence-based generic warning if no stored hints.
    """
    hints: list[str] = []

    # Query real evolution hints from semantic memory
    if semantic is not None:
        try:
            entries = await semantic.list_by_prefix(
                f"skill:{skill_name}:evolution_hint:", limit=3,
            )
            for entry in entries:
                hints.append(entry.value)
        except Exception:
            pass  # semantic query failure must never block load_skill

    # Fallback: confidence-based warning if no stored hints
    if not hints:
        skill = await procedural.get(skill_name)
        if (skill is not None
                and skill.confidence < 0.6
                and skill.usage_count >= 3):
            hints.append(
                f"⚠ This skill's confidence is {skill.confidence:.2f} "
                f"(usage: {skill.usage_count}×). "
                f"Consider reviewing recent outcomes and improving the workflow."
            )
    return hints


def sanitize_untrusted_text(text: str) -> str:
    """Sanitize external text to prevent XML-based prompt injection."""
    if not text:
        return ""
    safe_text = text.replace("<", "＜").replace(">", "＞")
    return f"<untrusted_external_content>\n{safe_text}\n</untrusted_external_content>"


def make_fetch_url_tool() -> ToolDefinition:
    """Return a SAFE tool that fetches a URL and returns cleaned text."""

    async def _fetch_url(call: ToolCall) -> ToolResult:
        url = call.args.get("url", "").strip()
        abort = call.abort_signal
        if not url:
            return ToolResult(call_id=call.id, tool_name=call.tool_name,
                              success=False, error="'url' argument is required")

        async def _get():
            async with httpx.AsyncClient(follow_redirects=True,
                                         timeout=_WEB_TIMEOUT) as client:
                r = await client.get(url, headers={"User-Agent": "Loom/0.3"})
                r.raise_for_status()
                return r

        try:
            resp, aborted = await _race_abort(_get(), abort)
            if aborted:
                return ToolResult(call_id=call.id, tool_name=call.tool_name,
                                  success=False, error="Request cancelled",
                                  failure_type="execution_error")
            content_type = resp.headers.get("content-type", "")
            if "html" in content_type:
                title, body = _html_to_text(resp.text)
                body = body[:_CONTENT_LIMIT]
                raw_output = f"Title: {title}\n\n{body}" if title else body
            else:
                raw_output = resp.text[:_CONTENT_LIMIT]
            output = sanitize_untrusted_text(raw_output)
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
        capabilities=ToolCapability.NETWORK,
        input_schema={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Full URL to fetch (http/https)"},
            },
            "required": ["url"],
        },
        executor=_fetch_url,
        tags=["web", "fetch"],
        impact_scope="network",
        scope_descriptions=["connects to the requested URL domain"],
        scope_resolver=_fetch_url_resolver,
    )


def make_web_search_tool(brave_api_key: str) -> ToolDefinition:
    """Return a GUARDED tool that searches the web via Brave Search API."""

    async def _web_search(call: ToolCall) -> ToolResult:
        query = call.args.get("query", "").strip()
        count = min(max(int(call.args.get("count", _SEARCH_RESULTS)), 1), 10)
        abort = call.abort_signal
        if not query:
            return ToolResult(call_id=call.id, tool_name=call.tool_name,
                              success=False, error="'query' argument is required")

        async def _search():
            async with httpx.AsyncClient(timeout=_WEB_TIMEOUT) as client:
                r = await client.get(
                    "https://api.search.brave.com/res/v1/web/search",
                    params={"q": query, "count": count},
                    headers={
                        "Accept": "application/json",
                        "Accept-Encoding": "gzip",
                        "X-Subscription-Token": brave_api_key,
                    },
                )
                r.raise_for_status()
                return r.json()

        try:
            result, aborted = await _race_abort(_search(), abort)
            if aborted:
                return ToolResult(call_id=call.id, tool_name=call.tool_name,
                                  success=False, error="Request cancelled",
                                  failure_type="execution_error")
            data = result
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
        raw_output = "\n\n".join(lines)
        output = sanitize_untrusted_text(raw_output)
        return ToolResult(call_id=call.id, tool_name=call.tool_name,
                          success=True, output=output)

    return ToolDefinition(
        name="web_search",
        description=(
            "Search the web via Brave Search and return top results with titles, URLs, and descriptions. "
            "Use this to find current information, documentation, or answers that aren't in memory."
        ),
        trust_level=TrustLevel.GUARDED,
        capabilities=ToolCapability.NETWORK,
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
        impact_scope="network",
        scope_descriptions=["connects to Brave Search API"],
        scope_resolver=_web_search_resolver,
    )


def make_spawn_agent_tool(parent_session: Any) -> "ToolDefinition":
    """Return a GUARDED tool that spawns an ephemeral sub-agent for a bounded task."""
    from loom.core.agent.subagent import SubAgentConfig, run_subagent

    async def _spawn_agent(call: ToolCall) -> ToolResult:
        task = call.args.get("task", "").strip()
        if not task:
            return ToolResult(call_id=call.id, tool_name=call.tool_name,
                              success=False, error="'task' argument is required",
                              failure_type="validation_error")

        raw_tools = call.args.get("tools", None)
        allowed_tools: list[str] | None = None
        if isinstance(raw_tools, list) and raw_tools:
            allowed_tools = [str(t) for t in raw_tools]
        elif isinstance(raw_tools, str) and raw_tools.strip():
            allowed_tools = [t.strip() for t in raw_tools.split(",") if t.strip()]

        max_turns = min(max(int(call.args.get("max_turns", 10)), 1), 50)

        config = SubAgentConfig(
            task=task,
            model=parent_session.model,
            allowed_tools=allowed_tools,
            max_turns=max_turns,
        )

        try:
            result = await run_subagent(
                config,
                router=parent_session.router,
                episodic=parent_session._episodic,
                semantic=parent_session._semantic,
                procedural=parent_session._procedural,
                tool_registry=parent_session.registry,
                parent_session_id=parent_session.session_id,
                workspace=parent_session.workspace,
                parent_grants=parent_session.perm.grants,
            )
        except Exception as exc:
            return ToolResult(call_id=call.id, tool_name=call.tool_name,
                              success=False, error=f"Sub-agent error: {exc}",
                              failure_type="execution_error")

        if result.success:
            header = (
                f"[sub-agent {result.agent_id}] "
                f"{result.turns_used} turn(s), {result.tool_calls} tool call(s)\n\n"
            )
            return ToolResult(call_id=call.id, tool_name=call.tool_name,
                              success=True, output=header + result.output)
        else:
            return ToolResult(call_id=call.id, tool_name=call.tool_name,
                              success=False, error=result.error or "Sub-agent failed",
                              failure_type="execution_error")

    return ToolDefinition(
        name="spawn_agent",
        description=(
            "Spawn an ephemeral sub-agent to complete a bounded, self-contained task. "
            "The sub-agent runs independently with its own context and returns a result. "
            "Use this to delegate research, file analysis, or parallel investigation tasks. "
            "The sub-agent cannot interact with the user — it works autonomously until done."
        ),
        trust_level=TrustLevel.GUARDED,
        capabilities=ToolCapability.AGENT_SPAN | ToolCapability.MUTATES,
        input_schema={
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "Clear description of the task for the sub-agent to complete",
                },
                "tools": {
                    "type": ["array", "string", "null"],
                    "description": (
                        "Tool whitelist for the sub-agent. "
                        "List of tool names (e.g. ['read_file', 'web_search']) or comma-separated string. "
                        "Omit or null for SAFE-only tools. CRITICAL tools are always blocked."
                    ),
                },
                "max_turns": {
                    "type": "integer",
                    "description": "Maximum turns before the sub-agent is stopped (1–50, default 10). This is a cap, not a fixed run count — the sub-agent exits early on end_turn. Use 20–30 for multi-step research or implementation tasks; reserve 40–50 for long data-heavy searches.",
                },
                "justification": {
                    "type": "string",
                    "description": "簡短說明為何在目前的脈絡下執行此工具是合理且必要的（給人類審核看）。",
                },
            },
            "required": ["task", "justification"],
        },
        executor=_spawn_agent,
        tags=["agent", "spawn"],
        impact_scope="agent",
        scope_descriptions=["spawns one sub-agent"],
        scope_resolver=_spawn_agent_resolver,
    )


# run_bash is registered via make_run_bash_tool(workspace, strict_sandbox) in
# LoomSession.start() so the sandbox setting can be wired in from loom.toml.
# read_file / write_file / list_dir are registered via make_filesystem_tools(workspace).


# ── Issue #128: Agent-driven TaskGraph tools ───────────────────────────────

def make_task_plan_tool(manager: "TaskGraphManager") -> ToolDefinition:
    """Create the task_plan tool for building a TaskGraph from agent specs."""
    from loom.core.tasks.manager import TaskGraphManager  # noqa: F811

    async def _task_plan(call: ToolCall) -> ToolResult:
        tasks = call.args.get("tasks", [])
        if not tasks:
            return ToolResult(
                call_id=call.id, tool_name=call.tool_name,
                success=False, error="'tasks' list is required and must not be empty",
            )
        # Validate each task spec
        for i, t in enumerate(tasks):
            if not t.get("id") or not t.get("content"):
                return ToolResult(
                    call_id=call.id, tool_name=call.tool_name,
                    success=False,
                    error=f"Task at index {i} missing required 'id' or 'content'",
                )
        try:
            graph = manager.create_graph(tasks)
            summary = graph.status_summary()
            plan = graph.compile()
            return ToolResult(
                call_id=call.id, tool_name=call.tool_name,
                success=True,
                output=json.dumps({
                    "status": "graph_created",
                    "total_nodes": summary["total_nodes"],
                    "levels": summary["levels"],
                    "plan": str(plan),
                    "ready_nodes": [n.id for n in manager.get_ready_nodes()],
                }, ensure_ascii=False),
            )
        except ValueError as exc:
            return ToolResult(
                call_id=call.id, tool_name=call.tool_name,
                success=False, error=str(exc),
            )

    return ToolDefinition(
        name="task_plan",
        description=(
            "Build a task execution graph (DAG) for complex multi-step work. "
            "Each task becomes a node; dependencies determine execution order. "
            "Independent tasks at the same level run in parallel. "
            "Use this when the current goal requires multiple coordinated steps."
        ),
        trust_level=TrustLevel.SAFE,
        capabilities=ToolCapability.NONE,
        input_schema={
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array",
                    "description": "List of tasks with dependencies",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {
                                "type": "string",
                                "description": "Short unique ID for this task (e.g. 'a', 'analyze', 'step1')",
                            },
                            "content": {
                                "type": "string",
                                "description": "Clear description of what this task should accomplish (becomes the turn prompt)",
                            },
                            "depends_on": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "IDs of tasks that must complete before this one starts",
                            },
                        },
                        "required": ["id", "content"],
                    },
                },
            },
            "required": ["tasks"],
        },
        executor=_task_plan,
        tags=["task", "planning"],
        impact_scope="agent",
    )


def make_task_status_tool(manager: "TaskGraphManager") -> ToolDefinition:
    """Create the task_status tool for querying the current graph state."""
    from loom.core.tasks.manager import TaskGraphManager  # noqa: F811

    async def _task_status(call: ToolCall) -> ToolResult:
        if not manager.has_graph:
            return ToolResult(
                call_id=call.id, tool_name=call.tool_name,
                success=True, output="No active task graph.",
            )
        try:
            summary = manager.status()
            return ToolResult(
                call_id=call.id, tool_name=call.tool_name,
                success=True,
                output=json.dumps(summary, ensure_ascii=False),
            )
        except Exception as exc:
            return ToolResult(
                call_id=call.id, tool_name=call.tool_name,
                success=False, error=str(exc),
            )

    return ToolDefinition(
        name="task_status",
        description=(
            "Check the current state of the active task graph. "
            "Shows each node's status (pending/in_progress/completed/failed), "
            "dependencies, and result summaries for completed nodes."
        ),
        trust_level=TrustLevel.SAFE,
        capabilities=ToolCapability.NONE,
        input_schema={"type": "object", "properties": {}},
        executor=_task_status,
        tags=["task", "status"],
        impact_scope="agent",
    )


def make_task_modify_tool(manager: "TaskGraphManager") -> ToolDefinition:
    """Create the task_modify tool for mutating the active graph."""
    from loom.core.tasks.manager import TaskGraphManager  # noqa: F811

    async def _task_modify(call: ToolCall) -> ToolResult:
        if not manager.has_graph:
            return ToolResult(
                call_id=call.id, tool_name=call.tool_name,
                success=False, error="No active task graph. Use task_plan first.",
            )
        errors: list[str] = []
        changes: list[str] = []

        # Add new nodes
        add_specs = call.args.get("add", [])
        if add_specs:
            try:
                added = manager.add_nodes(add_specs)
                changes.append(f"Added {len(added)} node(s): {[n.id for n in added]}")
            except ValueError as exc:
                errors.append(f"add: {exc}")

        # Remove nodes
        remove_ids = call.args.get("remove", [])
        if remove_ids:
            try:
                manager.remove_nodes(remove_ids)
                changes.append(f"Removed {len(remove_ids)} node(s): {remove_ids}")
            except ValueError as exc:
                errors.append(f"remove: {exc}")

        # Update nodes
        update_specs = call.args.get("update", [])
        if update_specs:
            try:
                updated = manager.update_nodes(update_specs)
                changes.append(f"Updated {len(updated)} node(s): {[n.id for n in updated]}")
            except ValueError as exc:
                errors.append(f"update: {exc}")

        if errors and not changes:
            return ToolResult(
                call_id=call.id, tool_name=call.tool_name,
                success=False, error="; ".join(errors),
            )

        summary = manager.status()
        output = {
            "changes": changes,
            "ready_nodes": [n.id for n in manager.get_ready_nodes()],
            "graph": summary,
        }
        if errors:
            output["partial_errors"] = errors
        return ToolResult(
            call_id=call.id, tool_name=call.tool_name,
            success=True,
            output=json.dumps(output, ensure_ascii=False),
        )

    return ToolDefinition(
        name="task_modify",
        description=(
            "Modify the active task graph: add new nodes, remove pending nodes, "
            "or update content/dependencies of pending nodes. "
            "Only PENDING nodes can be removed or updated."
        ),
        trust_level=TrustLevel.SAFE,
        capabilities=ToolCapability.MUTATES,
        input_schema={
            "type": "object",
            "properties": {
                "add": {
                    "type": "array",
                    "description": "New tasks to add (same format as task_plan)",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "content": {"type": "string"},
                            "depends_on": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                        },
                        "required": ["id", "content"],
                    },
                },
                "remove": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "IDs of pending nodes to remove",
                },
                "update": {
                    "type": "array",
                    "description": "Pending nodes to update",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "content": {"type": "string"},
                            "depends_on": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                        },
                        "required": ["id"],
                    },
                },
            },
        },
        executor=_task_modify,
        tags=["task", "modify"],
        impact_scope="agent",
    )


def make_task_done_tool(manager: "TaskGraphManager") -> ToolDefinition:
    """Create the task_done tool for marking nodes completed or failed."""
    from loom.core.tasks.manager import TaskGraphManager  # noqa: F811

    async def _task_done(call: ToolCall) -> ToolResult:
        node_id = call.args.get("node_id", "").strip()
        if not node_id:
            return ToolResult(
                call_id=call.id, tool_name=call.tool_name,
                success=False, error="'node_id' is required",
            )
        result_text = call.args.get("result", "").strip()
        error_text = call.args.get("error")
        failed = bool(error_text)

        try:
            if failed:
                node = manager.mark_failed(node_id, error_text)
                # Return status so agent can decide next steps
                status = manager.status()
                return ToolResult(
                    call_id=call.id, tool_name=call.tool_name,
                    success=True,
                    output=json.dumps({
                        "action": "node_failed",
                        "node_id": node.id,
                        "error": error_text,
                        "graph": status,
                        "hint": "Decide: retry (task_modify to update node), skip downstream, or abandon the graph.",
                    }, ensure_ascii=False),
                )
            else:
                if not result_text:
                    return ToolResult(
                        call_id=call.id, tool_name=call.tool_name,
                        success=False,
                        error="'result' is required when completing a node (summarize what you accomplished)",
                    )
                node = manager.mark_completed(node_id, result_text)
                # Auto-advance: check what's ready next
                ready = manager.get_ready_nodes()
                status = manager.status()

                # Build context for each ready node (Pull Model injection)
                ready_with_context = []
                for rn in ready:
                    ctx = manager.build_node_context(rn)
                    ready_with_context.append({
                        "node_id": rn.id,
                        "content": rn.content,
                        "context": ctx,
                    })

                return ToolResult(
                    call_id=call.id, tool_name=call.tool_name,
                    success=True,
                    output=json.dumps({
                        "action": "node_completed",
                        "node_id": node.id,
                        "result_summary": node.result_summary,
                        "graph_state": status["graph_state"],
                        "ready_nodes": ready_with_context,
                        "graph": status,
                    }, ensure_ascii=False),
                )
        except ValueError as exc:
            return ToolResult(
                call_id=call.id, tool_name=call.tool_name,
                success=False, error=str(exc),
            )

    return ToolDefinition(
        name="task_done",
        description=(
            "Mark a task node as completed (with result) or failed (with error). "
            "On completion, automatically checks which downstream nodes are now "
            "ready and returns their context including upstream result summaries. "
            "On failure, returns the graph state so you can decide how to proceed."
        ),
        trust_level=TrustLevel.SAFE,
        capabilities=ToolCapability.MUTATES,
        input_schema={
            "type": "object",
            "properties": {
                "node_id": {
                    "type": "string",
                    "description": "ID of the node to mark",
                },
                "result": {
                    "type": "string",
                    "description": "Summary of what was accomplished (required for completion)",
                },
                "error": {
                    "type": "string",
                    "description": "Error description (provide this instead of result to mark as failed)",
                },
            },
            "required": ["node_id"],
        },
        executor=_task_done,
        tags=["task", "done"],
        impact_scope="agent",
    )


def make_task_read_tool(manager: "TaskGraphManager") -> ToolDefinition:
    """Create the task_read tool for pulling full node results."""
    from loom.core.tasks.manager import TaskGraphManager  # noqa: F811

    async def _task_read(call: ToolCall) -> ToolResult:
        node_id = call.args.get("node_id", "").strip()
        if not node_id:
            return ToolResult(
                call_id=call.id, tool_name=call.tool_name,
                success=False, error="'node_id' is required",
            )
        try:
            result = manager.get_node_result(node_id)
            if result is None:
                node = manager.graph.get(node_id) if manager.graph else None
                status = node.status.value if node else "not found"
                return ToolResult(
                    call_id=call.id, tool_name=call.tool_name,
                    success=False,
                    error=f"Node '{node_id}' has no result (status: {status})",
                )
            return ToolResult(
                call_id=call.id, tool_name=call.tool_name,
                success=True, output=result,
            )
        except ValueError as exc:
            return ToolResult(
                call_id=call.id, tool_name=call.tool_name,
                success=False, error=str(exc),
            )

    return ToolDefinition(
        name="task_read",
        description=(
            "Read the full result of a completed task node. "
            "Use this when the result summary (shown in task_status) "
            "is insufficient and you need the complete output."
        ),
        trust_level=TrustLevel.SAFE,
        capabilities=ToolCapability.NONE,
        input_schema={
            "type": "object",
            "properties": {
                "node_id": {
                    "type": "string",
                    "description": "ID of the completed node to read",
                },
            },
            "required": ["node_id"],
        },
        executor=_task_read,
        tags=["task", "read"],
        impact_scope="agent",
    )
