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
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from loom.core.harness.middleware import ToolCall, ToolResult, VerifierResult
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
    from loom.core.cognition.skill_gate import SkillGate
    from loom.core.cognition.skill_mutator import SkillMutator
    from loom.core.cognition.skill_promoter import SkillPromoter
    from loom.core.harness.skill_checks import SkillCheckManager
    from loom.core.memory.facade import MemoryFacade
    from loom.core.memory.procedural import ProceduralMemory, SkillGenome
    from loom.core.memory.relational import RelationalMemory
    from loom.core.memory.search import MemorySearch
    from loom.core.memory.semantic import SemanticMemory
    from loom.core.memory.governance import MemoryGovernor
    from loom.core.memory.skill_outcome import SkillOutcomeTracker
    from loom.core.infra.telemetry import AgentTelemetryTracker
    from loom.core.tasks.manager import TaskListManager

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

    async def _verify_write_file(call: ToolCall, result: ToolResult) -> VerifierResult:
        """Re-read the written file and confirm its content matches (Issue #196).

        Catches cases where the write silently produced a different result
        than intended — truncated payloads, encoding issues, wrong path
        resolution masked by a pre-existing file.
        """
        resolved = result.metadata.get("_resolved_path", "")
        path = Path(resolved) if resolved else _resolve_workspace_path(
            call.args.get("path", ""), workspace
        )
        expected = call.args.get("content", "")
        if not path.exists():
            return VerifierResult(
                passed=False,
                reason=f"write_file reported success but {path} does not exist.",
                signal="file_missing",
            )
        try:
            actual = path.read_text(encoding="utf-8")
        except Exception as exc:
            return VerifierResult(
                passed=False,
                reason=f"write_file reported success but file is unreadable: {exc}",
                signal="file_unreadable",
            )
        if actual != expected:
            return VerifierResult(
                passed=False,
                reason=(
                    f"write_file roundtrip mismatch: wrote {len(expected)} chars, "
                    f"read back {len(actual)} chars."
                ),
                signal="content_mismatch",
            )
        return VerifierResult(passed=True)

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
            post_validator=_verify_write_file,
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
            inline_only=True,  # Output is structured + always small; #197
        ),
    ]


# ------------------------------------------------------------------
# Tool definitions ready to be registered
# ------------------------------------------------------------------

from loom.core.harness.registry import ToolDefinition


# ── run_bash semantic verification (Issue #196) ────────────────────────────
# run_bash's `success=True` comes from `exit_code==0`, but many tools silently
# exit 0 while their output screams failure. This validator catches the common
# cases without false-positiving on benign commands that happen to mention
# error-shaped strings (grep, cat, echo).

# Patterns picked for very low false-positive rate. Each checks for markers
# that almost exclusively appear in genuine failure output.

# Python traceback — the "most recent call last" line rarely appears outside
# actual crashes. Pattern matches the line itself, not just the word "error".
_PY_TRACEBACK_PATTERN = re.compile(
    r"^\s*Traceback \(most recent call last\):", re.MULTILINE
)

# pytest summary: "=== 1 failed, 3 passed in 0.12s ===" / "=== 2 failed in ..."
_PYTEST_FAILED_PATTERN = re.compile(
    r"^=+ .*?(\d+) failed[,\s]", re.MULTILINE
)

# jest / vitest: "Tests:       2 failed, 5 passed, 7 total"
_JS_TEST_FAILED_PATTERN = re.compile(
    r"^Tests:\s+(\d+) failed", re.MULTILINE
)

# go test failure marker — "--- FAIL:" precedes every failing test.
_GO_TEST_FAIL_PATTERN = re.compile(r"^--- FAIL:", re.MULTILINE)

# TypeScript compile errors — "error TS1234: ..." is the canonical tsc output
# format; rarely appears as text outside a compile run.
_TSC_ERROR_PATTERN = re.compile(r"\berror TS\d{3,5}:", re.MULTILINE)

# Shell command-not-found / ENOENT — the colon-delimited suffix is specific
# enough to avoid matching e.g. "grep 'command not found'" in a logfile.
_CMD_NOT_FOUND_PATTERN = re.compile(
    r": (?:command not found|No such file or directory)$", re.MULTILINE
)


async def _verify_run_bash(call: ToolCall, result: ToolResult) -> VerifierResult:
    """Post-validator for run_bash: detect silent failures despite exit 0.

    Scans combined stdout+stderr (run_bash merges them) for patterns that
    strongly indicate the command failed to achieve its purpose even though
    the shell returned 0 — Python tracebacks, test framework FAILED summaries,
    tsc error lines, missing commands. Conservative by design: only flags
    patterns with a very low false-positive rate.

    Returns VerifierResult(passed=False, reason=..., signal=<tag>) on match,
    VerifierResult(passed=True) otherwise.
    """
    output = str(result.output or "")
    if not output:
        return VerifierResult(passed=True)

    if _PY_TRACEBACK_PATTERN.search(output):
        return VerifierResult(
            passed=False,
            reason="Command exit=0 but Python traceback detected in output — "
                   "the interpreter likely caught the exception at top level.",
            signal="python_traceback",
        )

    m = _PYTEST_FAILED_PATTERN.search(output)
    if m:
        return VerifierResult(
            passed=False,
            reason=f"pytest reports {m.group(1)} failing test(s) despite exit=0 "
                   f"(check the summary line in output).",
            signal="pytest_failed",
        )

    m = _JS_TEST_FAILED_PATTERN.search(output)
    if m:
        return VerifierResult(
            passed=False,
            reason=f"JS test runner reports {m.group(1)} failing test(s) "
                   f"despite exit=0.",
            signal="js_test_failed",
        )

    if _GO_TEST_FAIL_PATTERN.search(output):
        return VerifierResult(
            passed=False,
            reason="go test output contains '--- FAIL:' markers despite exit=0.",
            signal="go_test_failed",
        )

    if _TSC_ERROR_PATTERN.search(output):
        return VerifierResult(
            passed=False,
            reason="TypeScript compiler reported errors (error TS####) "
                   "despite exit=0.",
            signal="tsc_error",
        )

    if _CMD_NOT_FOUND_PATTERN.search(output):
        return VerifierResult(
            passed=False,
            reason="Output contains shell 'command not found' / ENOENT — "
                   "the invoked command may not be installed.",
            signal="cmd_not_found",
        )

    return VerifierResult(passed=True)


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


def make_run_bash_tool(
    workspace: Path,
    strict_sandbox: bool = False,
    jobstore: Any = None,
    scratchpad: Any = None,
) -> ToolDefinition:
    """
    Return the ``run_bash`` tool definition.

    When *strict_sandbox* is True (set via ``[harness] strict_sandbox = true``
    in loom.toml), the subprocess is launched with ``cwd=workspace`` so that
    relative paths and shell builtins stay inside the project folder.  This
    does not prevent absolute-path escapes at the OS level — for full
    confinement use an OS sandbox (e.g. Docker).

    Issue #154: when ``async_mode=True`` is passed in call.args and a jobstore
    is available, the shell invocation is submitted as a background Job and
    the tool returns immediately with ``{"job_id": "..."}``.  Results land in
    the Scratchpad; harness injects status updates at turn boundaries.
    """
    async def _run_bash(call: ToolCall) -> ToolResult:
        command = call.args.get("command", "")
        async_mode = bool(call.args.get("async_mode", False))

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

        # Issue #100 / #165: shell injection scanner. This is a defense-in-depth
        # tripwire + audit signal, NOT a security boundary — see the module
        # docstring in loom/core/security/command_scanner.py. Every hit emits a
        # structured log line so downstream tooling (Discord notify, audit
        # pipelines) can subscribe to "command_scanner_*" without re-parsing.
        scan_verdict = _cmd_scanner.check(command)
        if scan_verdict.verdict == "block":
            _log.warning(
                "Command scanner blocked: %s", scan_verdict.description,
                extra={
                    "event": "command_scanner_block",
                    "pattern_key": scan_verdict.pattern_key,
                    "tool_name": call.tool_name,
                    "session_id": getattr(call, "session_id", ""),
                    "origin": getattr(call, "origin", ""),
                },
            )
            return ToolResult(
                call_id=call.id, tool_name=call.tool_name,
                success=False,
                error=f"[Security] Blocked by command scanner: {scan_verdict.description}",
                failure_type="security_block",
            )
        if scan_verdict.verdict == "warn":
            _log.warning(
                "Command scanner warning: %s", scan_verdict.description,
                extra={
                    "event": "command_scanner_warn",
                    "pattern_key": scan_verdict.pattern_key,
                    "tool_name": call.tool_name,
                    "session_id": getattr(call, "session_id", ""),
                    "origin": getattr(call, "origin", ""),
                },
            )

        timeout = call.args.get("timeout", 30)
        cwd = str(workspace) if strict_sandbox else None

        if async_mode and jobstore is not None and scratchpad is not None:
            job_id = jobstore.submit(
                "run_bash",
                {"command": command, "timeout": timeout},
                lambda: _run_bash_job(command, cwd, timeout, scratchpad),
            )
            return ToolResult(
                call_id=call.id, tool_name=call.tool_name,
                success=True,
                output=f"Submitted as {job_id}. Poll with jobs_status or jobs_await.",
                metadata={"job_id": job_id, "async": True},
            )

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=cwd,
            )
            try:
                try:
                    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
                    return ToolResult(call_id=call.id, tool_name=call.tool_name,
                                      success=False, error=f"Command timed out after {timeout}s",
                                      failure_type="timeout")
            finally:
                # Issue #222: cancellation (Discord cancel-and-relaunch,
                # _abort.abort()) bypasses the TimeoutError branch — kill
                # the subprocess so we don't orphan Playwright / shell
                # children. returncode is None iff still running.
                if proc.returncode is None:
                    proc.kill()
                    try:
                        await proc.wait()
                    except BaseException:
                        pass

            output = stdout.decode("utf-8", errors="replace")
            success = proc.returncode == 0

            # Issue #254: Whitelist legitimate non-zero shell exits so they
            # aren't flagged as tool failures (polluting telemetry) and
            # don't trigger is_error=True in the LLM.
            if not success:
                cmd_stripped = command.strip()
                # Use split to check the last command in a pipeline, or just look for the tool name
                # in the command string if it's simple enough.
                is_grep = "grep " in cmd_stripped or "egrep " in cmd_stripped or "fgrep " in cmd_stripped
                is_test = "test " in cmd_stripped or "[ " in cmd_stripped
                is_git_diff = "git diff" in cmd_stripped
                is_gh_jq = "gh api" in cmd_stripped and "--jq" in cmd_stripped

                if proc.returncode == 1 and (is_grep or is_test or is_git_diff):
                    success = True
                elif proc.returncode == 5 and is_gh_jq:
                    success = True

            if success and proc.returncode != 0:
                # We converted a non-zero exit to a success. Inject the exit
                # code into the output so the agent still receives the boolean
                # signal (e.g., test -f returning false).
                marker = f"[Command exited with {proc.returncode}]"
                output = f"{output}\n{marker}" if output else marker

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
    async_note = (
        " Pass async_mode=True to run in the background and receive a job_id; "
        "poll via jobs_status / jobs_await; read output with scratchpad_read."
    ) if jobstore is not None else ""
    return ToolDefinition(
        name="run_bash",
        description=f"Execute a shell command and return stdout/stderr.{sandbox_note}{async_note}",
        trust_level=TrustLevel.GUARDED,
        capabilities=ToolCapability.EXEC,
        input_schema={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to run"},
                "timeout": {"type": "integer", "description": "Timeout in seconds (default 30)"},
                "async_mode": {"type": "boolean", "description": "Run in background; return job_id immediately (default false)."},
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
        post_validator=_verify_run_bash,
    )


async def _run_bash_job(
    command: str,
    cwd: str | None,
    timeout: int,
    scratchpad: Any,
) -> tuple[str | None, str | None, str | None]:
    """Execute run_bash in background; write stdout to Scratchpad."""
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=cwd,
        )
        try:
            try:
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return None, None, f"Command timed out after {timeout}s"
        finally:
            # Issue #222: kill on cancellation so async jobs don't orphan.
            if proc.returncode is None:
                proc.kill()
                try:
                    await proc.wait()
                except BaseException:
                    pass
        output = stdout.decode("utf-8", errors="replace")
        ref = f"bash_{uuid.uuid4().hex[:8]}"
        scratchpad.write(ref, output)
        if proc.returncode != 0:
            return ref, f"exit {proc.returncode}, {len(output)} chars", None
        return ref, f"exit 0, {len(output)} chars", None
    except Exception as exc:
        return None, None, f"{type(exc).__name__}: {exc}"


# ------------------------------------------------------------------
# Memory tool factories (Phase 4B)
# Close over live memory store instances; call from LoomSession.start().
# ------------------------------------------------------------------

def make_recall_tool(memory: "MemoryFacade") -> ToolDefinition:
    """
    Create a SAFE ``recall`` tool bound to the given MemoryFacade.

    The tool performs BM25-ranked retrieval across semantic facts and
    skills via :meth:`MemoryFacade.search`.
    """
    from loom.core.memory.ontology import DOMAINS, TEMPORALS

    async def _recall(call: ToolCall) -> ToolResult:
        query = call.args.get("query", "").strip()
        mem_type = call.args.get("type", "all")
        limit = min(max(int(call.args.get("limit", 5)), 1), 10)
        domain = (call.args.get("domain") or "").strip().lower() or None
        temporal = (call.args.get("temporal") or "").strip().lower() or None

        if not query:
            return ToolResult(call_id=call.id, tool_name=call.tool_name,
                              success=False, error="'query' argument is required")

        if mem_type not in ("semantic", "skill", "all"):
            mem_type = "all"
        # Drop unknown axis filters silently rather than 400 — keeps the
        # recall hot path forgiving.
        if domain and domain not in DOMAINS:
            domain = None
        if temporal and temporal not in TEMPORALS:
            temporal = None

        results = await memory.search(  # type: ignore[arg-type]
            query, kind=mem_type, limit=limit,
            domain=domain, temporal=temporal,
        )

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
                "domain": {
                    "type": "string",
                    "enum": ["self", "user", "project", "knowledge"],
                    "description": (
                        "Optional Memory-Ontology axis filter — return only "
                        "facts in this domain. Skills are unaffected."
                    ),
                },
                "temporal": {
                    "type": "string",
                    "enum": ["ephemeral", "recent", "milestone", "archived"],
                    "description": (
                        "Optional temporal filter — e.g. 'milestone' to find "
                        "permanent anchors only."
                    ),
                },
            },
            "required": ["query"],
        },
        executor=_recall,
        tags=["memory", "search", "recall"],
        impact_scope="memory",
    )


def make_memorize_tool(
    memory: "MemoryFacade",
    *,
    on_reject: "Callable[[str, str, int], None] | None" = None,
) -> ToolDefinition:
    """
    Create a GUARDED ``memorize`` tool bound to the given MemoryFacade.

    Stores a key→value fact in semantic memory for future sessions.
    The facade routes the write through ``MemoryGovernor`` when one is
    wired (trust classification + contradiction detection); otherwise it
    falls back to a direct semantic upsert.  Either way the result shape
    (``GovernedWriteResult``) is uniform, and embedding-write failures
    are surfaced through a structured WARN log inside the facade.

    Parameters
    ----------
    on_reject : callable, optional
        Fired when the governor blocks a write. Signature:
        ``(key, trust_tier, contradictions_found) -> None``. Used by the
        platform layer to surface a harness inline message — accept events
        stay silent (PR-C4 design: governor only speaks when it stops
        something).
    """
    from loom.core.memory.ontology import (
        DEFAULT_TEMPORAL,
        normalize_domain,
        normalize_temporal,
    )
    from loom.core.memory.semantic import SemanticEntry

    async def _memorize(call: ToolCall) -> ToolResult:
        key = call.args.get("key", "").strip()
        value = call.args.get("value", "").strip()
        confidence = float(call.args.get("confidence", 0.8))
        confidence = max(0.0, min(1.0, confidence))
        # Memory Ontology v0.1 (issue #281): three-axis classification.
        # `domain` is optional in the schema — if missing, governor will
        # apply the heuristic classifier (loom/core/memory/classifier.py).
        domain_raw = (call.args.get("domain") or "").strip().lower() or None
        temporal_raw = (call.args.get("temporal") or DEFAULT_TEMPORAL).strip().lower()

        if not key or not value:
            return ToolResult(call_id=call.id, tool_name=call.tool_name,
                              success=False, error="Both 'key' and 'value' are required")

        entry = SemanticEntry(
            key=key,
            value=value,
            confidence=confidence,
            source="memorize",
            domain=normalize_domain(domain_raw),
            temporal=normalize_temporal(temporal_raw),
        )
        gov_result = await memory.memorize(entry)

        if not gov_result.written:
            msg = (
                f"Memorize skipped for {key!r}: existing entry has higher trust "
                f"(tier={gov_result.trust_tier}, contradictions={gov_result.contradictions_found})"
            )
            if on_reject is not None:
                try:
                    on_reject(key, gov_result.trust_tier, gov_result.contradictions_found)
                except Exception:
                    pass
            return ToolResult(call_id=call.id, tool_name=call.tool_name,
                              success=True, output=msg)

        if gov_result.contradictions_found > 0:
            msg = (
                f"Memorized: {key!r} (resolved {gov_result.contradictions_found} "
                f"contradiction(s) — {gov_result.resolution})"
            )
        elif gov_result.resolution == "replaced":
            msg = f"Memorized: {key!r} (overwrote previous value — history preserved)"
        else:
            msg = f"Memorized: {key!r}"

        return ToolResult(call_id=call.id, tool_name=call.tool_name,
                          success=True, output=msg,
                          metadata={"_memorized_key": key})

    async def _verify_memorize(call: ToolCall, result: ToolResult) -> VerifierResult:
        """Roundtrip check — confirm the memorized key is actually readable (Issue #196).

        The memorize tool has a legitimate "skipped" path (governor rejects
        lower-trust overwrites) which still returns success=True; that case
        is not a verifier failure. Only flag when the tool reported writing
        but the entry isn't retrievable afterward.
        """
        key = result.metadata.get("_memorized_key")
        if not key:
            # Skipped path or missing metadata — nothing to verify.
            return VerifierResult(passed=True)
        try:
            entry = await memory.semantic.get(key)
        except Exception as exc:
            return VerifierResult(
                passed=False,
                reason=f"memorize succeeded but readback raised: {exc}",
                signal="readback_error",
            )
        if entry is None:
            return VerifierResult(
                passed=False,
                reason=f"memorize succeeded but key {key!r} is not retrievable.",
                signal="key_not_found",
            )
        return VerifierResult(passed=True)

    async def _memorize_rollback(call: ToolCall, result: ToolResult) -> ToolResult:
        """Delete the key that was just memorized."""
        key = result.metadata.get("_memorized_key") or call.args.get("key", "").strip()
        if not key:
            return ToolResult(call_id=call.id, tool_name="memorize",
                              success=False, error="No key to rollback")
        try:
            await memory.semantic.delete(key)
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
                "domain": {
                    "type": "string",
                    "enum": ["self", "user", "project", "knowledge"],
                    "description": (
                        "Semantic territory of the fact: 'self' (agent identity / "
                        "principles), 'user' (preferences / relationship), 'project' "
                        "(architecture / config / workflow), 'knowledge' (external "
                        "facts / tool usage). Optional — if omitted, inferred from key."
                    ),
                },
                "temporal": {
                    "type": "string",
                    "enum": ["ephemeral", "recent", "milestone", "archived"],
                    "description": (
                        "Lifecycle state: 'ephemeral' (session-scoped), 'recent' "
                        "(default — active within ~7d), 'milestone' (permanent "
                        "anchor; use sparingly), 'archived' (rare on write)."
                    ),
                },
            },
            "required": ["key", "value"],
        },
        executor=_memorize,
        tags=["memory", "write", "memorize"],
        impact_scope="memory",
        rollback_fn=_memorize_rollback,
        post_validator=_verify_memorize,
        inline_only=True,  # Returns short structured ack; #197
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


def make_agent_health_tool(tracker: "AgentTelemetryTracker") -> ToolDefinition:
    """
    Create a SAFE ``agent_health`` tool (Issue #142, #285).

    Wraps ``AgentTelemetryTracker`` so the agent can pull its own
    observability state on demand. Keep it pull-only: anomalies are already
    pushed at turn boundaries — this tool is for the cases where the agent
    wants to look without being prompted.

    Progressive disclosure (Issue #285):
      - default            → one-line summary across all dimensions (cheap)
      - ``dimension=X``    → detail for one dimension (drill-down)
      - ``full=true``      → full detail across all dimensions (expensive)

    Anomalies (if any) are appended in all modes so the agent never needs a
    second call to decide whether to act.
    """
    async def _agent_health(call: ToolCall) -> ToolResult:
        dimension = call.args.get("dimension")
        full = bool(call.args.get("full", False))
        if dimension:
            output = tracker.report_detail(dimension) or "(no telemetry data yet)"
        elif full:
            output = tracker.report_detail() or "(no telemetry data yet)"
        else:
            output = tracker.report_minimal() or "(no telemetry data yet)"
        alert = tracker.anomaly_report()
        if alert:
            output = f"{output}\n\n{alert}"
        return ToolResult(
            call_id=call.id,
            tool_name=call.tool_name,
            success=True,
            output=output,
        )

    return ToolDefinition(
        name="agent_health",
        description=(
            "Inspect your own observability telemetry: tool call success/latency, "
            "context token layout, memory-compression yield, runtime identity, "
            "context budget, session turns, and loaded skills. "
            "Default is a one-line summary across all dimensions. "
            "Pass `dimension=<name>` to drill into one, or `full=true` to dump "
            "every dimension's detail at once (expensive — only use when "
            "investigating). Use this to self-check before a risky action or "
            "when suspecting behavioral drift."
        ),
        trust_level=TrustLevel.SAFE,
        input_schema={
            "type": "object",
            "properties": {
                "dimension": {
                    "type": "string",
                    "enum": ["tool_call", "context_layout", "memory_compression", "runtime_identity", "context_budget", "session_turns", "loaded_skills"],
                    "description": "Specific dimension to inspect.",
                },
                "full": {
                    "type": "boolean",
                    "description": "Dump every dimension's full detail. Default false (summary only).",
                    "default": False,
                },
            },
        },
        executor=_agent_health,
        tags=["telemetry", "health", "diagnostic"],
        impact_scope="observability",
    )



def make_probe_file_tool() -> ToolDefinition:
    """
    Create a SAFE ``probe_file`` tool (Issues #283, #288).

    Fallback marker for cases where LegitimacyGuard cannot infer the
    probed file from the tool call itself — e.g. shell pipelines
    (``cat a.py | grep foo``), variable expansion, or custom scripts
    that print file contents. Standard ``read_file`` and simple
    ``grep``/``head``/``sed -n`` invocations already auto-register
    the file path; calling ``probe_file`` redundantly in those cases
    just burns context.
    """

    async def _probe_file(call: ToolCall) -> ToolResult:
        path = call.args.get("path", "")
        return ToolResult(
            call_id=call.id,
            tool_name="probe_file",
            success=True,
            output=f"File marked as probed: {path}",
        )

    return ToolDefinition(
        name="probe_file",
        description=(
            "Mark a specific file as probed when LegitimacyGuard cannot infer "
            "the path from your tool call (complex bash pipelines, custom "
            "scripts, etc.). Do NOT call this after standard read_file or "
            "simple grep/head/sed -n invocations — those already register "
            "the path automatically and a redundant probe_file just burns "
            "context."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file that was probed.",
                },
            },
            "required": ["path"],
        },
        executor=_probe_file,
        trust_level=TrustLevel.SAFE,
        tags=["io", "guard"],
        impact_scope="file",
    )


def make_relate_tool(memory: "MemoryFacade") -> ToolDefinition:
    """
    Create a GUARDED ``relate`` tool bound to the given MemoryFacade.

    Stores a (subject, predicate, object) triple via
    :meth:`MemoryFacade.relate` — e.g.
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
        await memory.relate(entry)
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
            await memory.relational.delete(subject, predicate)
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


def make_query_relations_tool(memory: "MemoryFacade") -> ToolDefinition:
    """
    Create a SAFE ``query_relations`` tool bound to the given MemoryFacade.

    Returns all triples matching the given subject and/or predicate
    filters via :meth:`MemoryFacade.query_relations`.
    """
    async def _query_relations(call: ToolCall) -> ToolResult:
        subject = call.args.get("subject", "").strip() or None
        predicate = call.args.get("predicate", "").strip() or None

        if not subject and not predicate:
            return ToolResult(call_id=call.id, tool_name=call.tool_name,
                              success=False, error="At least one of 'subject' or 'predicate' is required")

        entries = await memory.query_relations(subject=subject, predicate=predicate)
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
    skill_gate: "SkillGate | None" = None,
    on_loaded: "Callable[[str], None] | None" = None,
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

        # Issue #120 PR 3: resolve parent vs shadow candidate body.  Decision
        # is deterministic per (session_id, skill_name); audit tag is carried
        # back in the tool result metadata so diagnostics can A/B compare.
        gate_decision = None
        if skill_gate is not None:
            gate_decision = await skill_gate.resolve(skill)
            body = gate_decision.body or "(no instructions)"
        else:
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
        if on_loaded is not None:
            on_loaded(name)
        if outcome_tracker is not None:
            _turn = turn_index_fn() if turn_index_fn else 0
            outcome_tracker.record_activation(name, _turn)

        metadata: dict = {"skill_name": name, "skill_confidence": skill.confidence}
        if gate_decision is not None:
            metadata["skill_source"] = gate_decision.audit_tag()
            metadata["shadow_mode"] = gate_decision.shadow_mode
            if gate_decision.candidate_id is not None:
                metadata["shadow_candidate_id"] = gate_decision.candidate_id
        return ToolResult(
            call_id=call.id, tool_name=call.tool_name,
            success=True, output=output,
            metadata=metadata,
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
        if manager is None:
            return []

        # Harness invariant: every load_skill is a lifecycle event for the
        # manager, even when the new skill declares no checks.  Without this,
        # a skill with no checks would leave the previous skill's checks
        # stranded on tool definitions (Issue #184).
        if not skill.precondition_check_refs:
            manager.activate(name, keep_existing=keep_existing)
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


# ------------------------------------------------------------------
# Skill lifecycle tools (Issue #120 PR 3)
# ------------------------------------------------------------------


def make_skill_promote_tool(promoter: "SkillPromoter") -> ToolDefinition:
    """Create a GUARDED ``promote_skill_candidate`` tool.

    Swaps the parent SKILL.md for the candidate body, archives the old
    body to ``skill_version_history``, and bumps the skill version.  Used
    primarily in mode B (manual promotion) but also available to the
    agent in mode C when it wants to accelerate a pending shadow.
    """
    async def _promote(call: ToolCall) -> ToolResult:
        candidate_id = call.args.get("candidate_id", "").strip()
        reason = (call.args.get("reason") or "").strip() or None
        if not candidate_id:
            return ToolResult(
                call_id=call.id, tool_name=call.tool_name,
                success=False, error="'candidate_id' is required",
            )
        try:
            skill = await promoter.promote(candidate_id, reason=reason)
        except ValueError as exc:
            return ToolResult(
                call_id=call.id, tool_name=call.tool_name,
                success=False, error=str(exc),
            )
        if skill is None:
            return ToolResult(
                call_id=call.id, tool_name=call.tool_name,
                success=False, error=f"Candidate {candidate_id} or its parent not found",
            )
        return ToolResult(
            call_id=call.id, tool_name=call.tool_name,
            success=True,
            output=f"Promoted candidate {candidate_id[:8]} → {skill.name} v{skill.version}",
            metadata={"skill_name": skill.name, "new_version": skill.version},
        )

    return ToolDefinition(
        name="promote_skill_candidate",
        description=(
            "Promote a candidate SKILL.md revision to replace the parent. "
            "Archives the current body to version history and bumps the skill version. "
            "Use when a shadow candidate has proven itself or when manually "
            "accepting a generated proposal."
        ),
        trust_level=TrustLevel.GUARDED,
        input_schema={
            "type": "object",
            "properties": {
                "candidate_id": {
                    "type": "string",
                    "description": "ID of the candidate to promote (see `loom skill candidates`).",
                },
                "reason": {
                    "type": "string",
                    "description": "Optional free-text reason stored in audit notes.",
                },
            },
            "required": ["candidate_id"],
        },
        executor=_promote,
        tags=["skill", "memory", "lifecycle"],
        impact_scope="memory",
    )


def make_skill_rollback_tool(promoter: "SkillPromoter") -> ToolDefinition:
    """Create a GUARDED ``rollback_skill`` tool.

    Restores a previous SKILL.md body from history — by default the most
    recently archived version, i.e. undoing the latest promote.  The
    current body is re-archived so the rollback itself is reversible.
    """
    async def _rollback(call: ToolCall) -> ToolResult:
        name = call.args.get("skill_name", "").strip()
        to_version = call.args.get("to_version")
        reason = (call.args.get("reason") or "").strip() or None
        if not name:
            return ToolResult(
                call_id=call.id, tool_name=call.tool_name,
                success=False, error="'skill_name' is required",
            )
        version_arg: int | None
        if to_version is None or to_version == "":
            version_arg = None
        else:
            try:
                version_arg = int(to_version)
            except (TypeError, ValueError):
                return ToolResult(
                    call_id=call.id, tool_name=call.tool_name,
                    success=False, error=f"'to_version' must be an integer, got {to_version!r}",
                )
        skill = await promoter.rollback(name, to_version=version_arg, reason=reason)
        if skill is None:
            where = f" v{version_arg}" if version_arg is not None else ""
            return ToolResult(
                call_id=call.id, tool_name=call.tool_name,
                success=False,
                error=f"No history entry to roll back to for '{name}'{where}",
            )
        return ToolResult(
            call_id=call.id, tool_name=call.tool_name,
            success=True,
            output=f"Rolled back {name} → v{skill.version} (body restored from history)",
            metadata={"skill_name": name, "new_version": skill.version},
        )

    return ToolDefinition(
        name="rollback_skill",
        description=(
            "Roll a skill back to a previously-archived SKILL.md body. "
            "Without ``to_version`` the latest archived entry is restored "
            "(i.e. undo the most recent promote). The current body is "
            "re-archived before the swap so rollbacks are themselves reversible."
        ),
        trust_level=TrustLevel.GUARDED,
        input_schema={
            "type": "object",
            "properties": {
                "skill_name": {
                    "type": "string",
                    "description": "Name of the skill to roll back.",
                },
                "to_version": {
                    "type": "integer",
                    "description": "Specific historic version to restore. Omit for the most recent archive.",
                },
                "reason": {
                    "type": "string",
                    "description": "Optional free-text reason stored in audit notes.",
                },
            },
            "required": ["skill_name"],
        },
        executor=_rollback,
        tags=["skill", "memory", "lifecycle"],
        impact_scope="memory",
    )


# ------------------------------------------------------------------
# Skill evolution tools (Issue #120 PR 4 — meta-skill-engineer surface)
# ------------------------------------------------------------------


_MATURITY_TAG_VALUES: tuple[str, ...] = ("mature", "needs_improvement")


def make_generate_skill_candidate_from_batch_tool(
    mutator: "SkillMutator",
    procedural: "ProceduralMemory",
    session_id: str | None = None,
) -> ToolDefinition:
    """Create a GUARDED ``generate_skill_candidate_from_batch`` tool.

    Lets the meta-skill-engineer agent feed Grader batch results straight
    into ``SkillMutator.from_batch_diagnostic`` without dropping to Python.
    The agent supplies the aggregated fields (mutation_suggestions,
    instructions_violated, failure_patterns) plus pass_rate /
    previous_pass_rate; the tool assembles a synthetic ``BatchDiagnostic``
    with one representative ``TaskDiagnostic`` carrying those lists,
    persists the candidate, and returns its id + fast_track flag.
    """
    async def _generate(call: ToolCall) -> ToolResult:
        from loom.core.cognition.task_reflector import BatchDiagnostic, TaskDiagnostic

        args = call.args
        skill_name = (args.get("skill_name") or "").strip()
        if not skill_name:
            return ToolResult(
                call_id=call.id, tool_name=call.tool_name,
                success=False, error="'skill_name' is required",
            )

        try:
            pass_rate = float(args.get("pass_rate"))
        except (TypeError, ValueError):
            return ToolResult(
                call_id=call.id, tool_name=call.tool_name,
                success=False, error="'pass_rate' must be a float in [0.0, 1.0]",
            )
        pass_rate = max(0.0, min(1.0, pass_rate))

        prev_raw = args.get("previous_pass_rate")
        previous_pass_rate: float | None = None
        if prev_raw not in (None, ""):
            try:
                previous_pass_rate = max(0.0, min(1.0, float(prev_raw)))
            except (TypeError, ValueError):
                return ToolResult(
                    call_id=call.id, tool_name=call.tool_name,
                    success=False,
                    error="'previous_pass_rate' must be a float in [0.0, 1.0] or null",
                )

        def _as_str_list(v: Any) -> list[str]:
            if not isinstance(v, list):
                return []
            return [str(x).strip() for x in v if str(x).strip()]

        suggestions = _as_str_list(args.get("mutation_suggestions"))
        if not suggestions:
            return ToolResult(
                call_id=call.id, tool_name=call.tool_name,
                success=False,
                error="'mutation_suggestions' must be a non-empty list of strings",
            )
        violations = _as_str_list(args.get("instructions_violated"))
        failures = _as_str_list(args.get("failure_patterns"))

        try:
            avg_quality = float(args.get("avg_quality_score", 3.0))
        except (TypeError, ValueError):
            avg_quality = 3.0
        avg_quality = max(1.0, min(5.0, avg_quality))

        parent = await procedural.get(skill_name)
        if parent is None:
            return ToolResult(
                call_id=call.id, tool_name=call.tool_name,
                success=False, error=f"Skill '{skill_name}' not found",
            )

        synthetic = TaskDiagnostic(
            skill_name=skill_name,
            session_id=session_id or "meta-skill-engineer",
            turn_index=0,
            task_type="workflow_composite",
            task_type_confidence=1.0,
            instructions_followed=[],
            instructions_violated=violations,
            failure_patterns=failures,
            success_patterns=[],
            mutation_suggestions=suggestions,
            quality_score=avg_quality,
        )
        batch = BatchDiagnostic(
            skill_name=skill_name,
            diagnostics=[synthetic],
            pass_rate=pass_rate,
            previous_pass_rate=previous_pass_rate,
        )

        proposal = await mutator.from_batch_diagnostic(
            parent=parent, batch=batch, session_id=session_id,
        )
        if proposal is None:
            return ToolResult(
                call_id=call.id, tool_name=call.tool_name,
                success=False,
                error=(
                    "Mutator produced no candidate — check that "
                    "[mutation].enabled=true, the parent body is non-empty, "
                    "and the LLM rewrite was plausible."
                ),
            )
        await procedural.insert_candidate(proposal.candidate)
        cand = proposal.candidate
        return ToolResult(
            call_id=call.id, tool_name=call.tool_name,
            success=True,
            output=(
                f"Candidate {cand.id[:8]} generated for {skill_name} "
                f"(fast_track={cand.fast_track}, notes={cand.notes})"
            ),
            metadata={
                "candidate_id": cand.id,
                "fast_track": cand.fast_track,
                "mutation_strategy": cand.mutation_strategy,
                "parent_version": cand.parent_version,
            },
        )

    return ToolDefinition(
        name="generate_skill_candidate_from_batch",
        description=(
            "Generate a candidate SKILL.md revision from Grader batch results. "
            "Used by the meta-skill-engineer skill after running a test set: "
            "pass the aggregated mutation_suggestions / instructions_violated / "
            "failure_patterns plus pass_rate (and optionally previous_pass_rate "
            "to enable fast-track promotion when improvement ≥ threshold). "
            "Returns the candidate_id and whether it was flagged for fast-track."
        ),
        trust_level=TrustLevel.GUARDED,
        input_schema={
            "type": "object",
            "properties": {
                "skill_name": {
                    "type": "string",
                    "description": "Name of the parent skill being mutated.",
                },
                "pass_rate": {
                    "type": "number",
                    "description": "Fraction 0.0–1.0 of tests that passed in the current batch.",
                },
                "previous_pass_rate": {
                    "type": "number",
                    "description": (
                        "Pass rate of the previous skill version on the same "
                        "test set. Omit on first-ever run. When provided, "
                        "improvement ≥ fast_track_threshold flags the candidate."
                    ),
                },
                "mutation_suggestions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Aggregated, deduplicated SKILL.md edit suggestions.",
                },
                "instructions_violated": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Aggregated SKILL.md instructions that were ignored or misapplied.",
                },
                "failure_patterns": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Aggregated recurring failure modes observed during the batch.",
                },
                "avg_quality_score": {
                    "type": "number",
                    "description": "Average quality score 1.0–5.0 across the batch (defaults to 3.0).",
                },
            },
            "required": ["skill_name", "pass_rate", "mutation_suggestions"],
        },
        executor=_generate,
        tags=["skill", "memory", "lifecycle", "meta"],
        impact_scope="memory",
    )


def make_set_skill_maturity_tool(
    procedural: "ProceduralMemory",
) -> ToolDefinition:
    """Create a GUARDED ``set_skill_maturity`` tool.

    Wraps ``ProceduralMemory.update_maturity_tag`` so the meta-skill-engineer
    can label a skill ``mature`` (stop running the Grader on it) or
    ``needs_improvement`` (keep iterating) — or clear the tag. This is the
    Stage 7 termination signal in the meta-skill-engineer workflow.
    """
    async def _set_maturity(call: ToolCall) -> ToolResult:
        skill_name = (call.args.get("skill_name") or "").strip()
        if not skill_name:
            return ToolResult(
                call_id=call.id, tool_name=call.tool_name,
                success=False, error="'skill_name' is required",
            )

        tag_raw = call.args.get("tag")
        tag: str | None
        # Normalise before validation so common typos (e.g. "Mature",
        # "NEEDS_IMPROVEMENT", or Python-style "Needs Improvement") don't
        # write dirty values into the DB. Anything else falls through to
        # the validation error below.
        if tag_raw is None:
            tag = None
        else:
            normalised = str(tag_raw).strip().lower().replace(" ", "_").replace("-", "_")
            if normalised in ("", "none", "null", "clear"):
                tag = None
            elif normalised in _MATURITY_TAG_VALUES:
                tag = normalised
            else:
                return ToolResult(
                    call_id=call.id, tool_name=call.tool_name,
                    success=False,
                    error=(
                        f"'tag' must be one of {_MATURITY_TAG_VALUES} "
                        f"or null/'clear' to unset; got {tag_raw!r}"
                    ),
                )

        ok = await procedural.update_maturity_tag(skill_name, tag)
        if not ok:
            return ToolResult(
                call_id=call.id, tool_name=call.tool_name,
                success=False, error=f"Skill '{skill_name}' not found",
            )
        display = tag if tag is not None else "(cleared)"
        return ToolResult(
            call_id=call.id, tool_name=call.tool_name,
            success=True,
            output=f"Skill '{skill_name}' maturity_tag set to {display}",
            metadata={"skill_name": skill_name, "maturity_tag": tag},
        )

    return ToolDefinition(
        name="set_skill_maturity",
        description=(
            "Label a skill's maturity to drive meta-skill-engineer termination. "
            "Tag 'mature' → skill graduates, Grader stops; 'needs_improvement' → "
            "keep iterating; null / 'clear' → unset the tag."
        ),
        trust_level=TrustLevel.GUARDED,
        input_schema={
            "type": "object",
            "properties": {
                "skill_name": {
                    "type": "string",
                    "description": "Name of the skill to tag.",
                },
                "tag": {
                    "type": "string",
                    "description": (
                        "One of 'mature', 'needs_improvement', or 'clear'/null "
                        "to remove the tag."
                    ),
                },
            },
            "required": ["skill_name"],
        },
        executor=_set_maturity,
        tags=["skill", "memory", "lifecycle", "meta"],
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


# ── fetch_url semantic verification (Issue #199) ───────────────────────────
# Wrapper applied by sanitize_untrusted_text() — validator peels it off
# before inspecting content.
_SANITIZE_OPEN = "<untrusted_external_content>\n"
_SANITIZE_CLOSE = "\n</untrusted_external_content>"

# Error-page title patterns. Calibrated to match canonical server/CDN
# error page titles without false-positiving on legitimate articles that
# happen to discuss error codes. Key trick: require the canonical
# "{code} {message}" pairing or exact-match short strings — an article
# titled "404 Error Handling in HTTP" won't match "404 Not Found".
_ERROR_PAGE_TITLE_PATTERNS = [
    re.compile(
        r"^\s*[45]\d{2}\s+(Not Found|Forbidden|Unauthorized|Internal Server Error|"
        r"Service Unavailable|Bad Gateway|Gateway Timeout|Bad Request)",
        re.IGNORECASE,
    ),
    re.compile(r"^\s*Page Not Found\s*$", re.IGNORECASE),
    re.compile(r"^\s*Not Found\s*$", re.IGNORECASE),
    re.compile(r"^\s*Access Denied", re.IGNORECASE),
    re.compile(r"^\s*Forbidden\s*$", re.IGNORECASE),
    re.compile(r"^\s*Unauthorized\s*$", re.IGNORECASE),
    # Cloudflare / bot-wall challenge pages
    re.compile(r"^\s*Just a moment", re.IGNORECASE),
    re.compile(r"^\s*Attention Required", re.IGNORECASE),
    re.compile(r"^\s*Please verify you are", re.IGNORECASE),
]


async def _verify_fetch_url(call: ToolCall, result: ToolResult) -> VerifierResult:
    """Detect HTTP-2xx silent failures from fetch_url (Issue #199).

    fetch_url's executor already calls raise_for_status(), so non-2xx is
    handled by the tool itself. What slips through: 200 responses that are
    actually error-page templates, CDN challenge pages, or suspiciously
    thin cleaned content. This validator inspects the Title-prefixed HTML
    output format produced by _html_to_text and flags known error-page
    shapes.

    Non-HTML responses (JSON APIs, plain text) are passed through unchecked
    — the "short body" heuristic only fires on HTML, since API responses
    can legitimately be very short.

    Signals emitted (PR #202 review):
      - ``html_error_page``: title matches a canonical error/challenge
        pattern. Strong signal — usually means retry the URL or accept
        the resource is unavailable.
      - ``thin_html_content``: HTML page has a title but cleaned body is
        nearly empty. Often a JS-rendered SPA (use a headless browser
        instead) or a stripped error template (escalate as html_error_page
        if title is unhelpful).

    These signal tags are stable strings — telemetry consumers and any
    future learning loop (#200) can dispatch on them without parsing
    ``reason``.
    """
    # Async mode returns a job_id header, not content; no verification.
    if (result.metadata or {}).get("async") is True:
        return VerifierResult(passed=True)

    output = str(result.output or "")
    if not output:
        return VerifierResult(passed=True)

    # Peel the sanitize_untrusted_text wrapper if present.
    content = output
    if content.startswith(_SANITIZE_OPEN) and content.endswith(_SANITIZE_CLOSE):
        content = content[len(_SANITIZE_OPEN):-len(_SANITIZE_CLOSE)]

    # Non-HTML fetches have no "Title: " prefix — skip body/title heuristics.
    if not content.startswith("Title: "):
        return VerifierResult(passed=True)

    first_line, _, body = content.partition("\n\n")
    title = first_line[len("Title: "):].strip()

    for pattern in _ERROR_PAGE_TITLE_PATTERNS:
        if pattern.match(title):
            return VerifierResult(
                passed=False,
                reason=f"HTTP 2xx but page title indicates error: {title!r}",
                signal="html_error_page",
            )

    # HTML page with a title but almost no cleaned body → typically an
    # SPA serving content via JS (captured as empty after script stripping)
    # or a minimalist error page. Either way, the agent didn't receive
    # usable content.
    #
    # Threshold note: 100 chars is conservative — any real article body
    # after script/style/nav stripping reliably exceeds this. If a future
    # site is observed under 100 chars legitimately, either narrow by
    # domain heuristic or expose this as a per-tool config knob; do not
    # raise the global floor without telemetry support.
    if len(body.strip()) < 100:
        return VerifierResult(
            passed=False,
            reason=(
                f"HTML body is only {len(body.strip())} chars after cleaning — "
                f"likely a challenge/captcha page, JS-rendered SPA, or "
                f"stripped error template."
            ),
            signal="thin_html_content",
        )

    return VerifierResult(passed=True)


def make_fetch_url_tool(
    jobstore: Any = None,
    scratchpad: Any = None,
) -> ToolDefinition:
    """Return a SAFE tool that fetches a URL and returns cleaned text.

    Issue #154: when ``async_mode=True`` is passed and a jobstore is
    available, the fetch is submitted as a background Job; the tool
    returns a job_id immediately and the body lands in Scratchpad.
    """

    async def _fetch_url(call: ToolCall) -> ToolResult:
        url = call.args.get("url", "").strip()
        async_mode = bool(call.args.get("async_mode", False))
        abort = call.abort_signal
        if not url:
            return ToolResult(call_id=call.id, tool_name=call.tool_name,
                              success=False, error="'url' argument is required")

        if async_mode and jobstore is not None and scratchpad is not None:
            job_id = jobstore.submit(
                "fetch_url",
                {"url": url},
                lambda: _fetch_url_job(url, scratchpad),
            )
            return ToolResult(
                call_id=call.id, tool_name=call.tool_name,
                success=True,
                output=f"Submitted as {job_id}. Poll with jobs_status or jobs_await.",
                metadata={"job_id": job_id, "async": True},
            )

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

    async_note = (
        " Pass async_mode=True to fetch in the background and receive a job_id; "
        "the full body lands in Scratchpad."
    ) if jobstore is not None else ""
    return ToolDefinition(
        name="fetch_url",
        description=(
            "Fetch a URL and return the page title and cleaned body text (scripts/styles removed). "
            "Use this to read web pages, documentation, or articles. "
            f"Synchronous output is truncated to 2000 chars.{async_note}"
        ),
        trust_level=TrustLevel.SAFE,
        capabilities=ToolCapability.NETWORK,
        input_schema={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Full URL to fetch (http/https)"},
                "async_mode": {"type": "boolean", "description": "Fetch in background; return job_id (default false)."},
            },
            "required": ["url"],
        },
        executor=_fetch_url,
        tags=["web", "fetch"],
        impact_scope="network",
        scope_descriptions=["connects to the requested URL domain"],
        scope_resolver=_fetch_url_resolver,
        post_validator=_verify_fetch_url,
    )


async def _fetch_url_job(
    url: str,
    scratchpad: Any,
) -> tuple[str | None, str | None, str | None]:
    """Fetch a URL in the background; write the body to Scratchpad."""
    try:
        async with httpx.AsyncClient(follow_redirects=True,
                                     timeout=_WEB_TIMEOUT) as client:
            r = await client.get(url, headers={"User-Agent": "Loom/0.3"})
            r.raise_for_status()
        content_type = r.headers.get("content-type", "")
        if "html" in content_type:
            title, body = _html_to_text(r.text)
            raw = f"Title: {title}\n\n{body}" if title else body
        else:
            raw = r.text
        clean = sanitize_untrusted_text(raw)
        ref = f"fetch_{uuid.uuid4().hex[:8]}"
        scratchpad.write(ref, clean)
        return ref, f"{len(clean)} chars from {url}", None
    except httpx.HTTPStatusError as exc:
        return None, None, f"HTTP {exc.response.status_code}: {url}"
    except Exception as exc:
        return None, None, f"{type(exc).__name__}: {exc}"


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
        # Issue #167: GUARDED trust (NETWORK side-effect) but read-only in
        # intent — explicitly mark it as a probe so LegitimacyGuard counts it
        # toward the probe-first heuristic. SAFE tools get this for free.
        capabilities=ToolCapability.NETWORK | ToolCapability.READ_PROBE,
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


async def _verify_spawn_agent(call: ToolCall, result: ToolResult) -> VerifierResult:
    """Catch hollow sub-agent successes (Issue #196 Phase 1, PR #198 review).

    spawn_agent's failure path already surfaces rich context via #192/#193,
    but its *success* path can still be pathological — zero executed turns,
    near-empty output. This validator checks structured signals stashed in
    metadata by the spawn_agent executor, not the output string.
    """
    meta = result.metadata or {}
    turns = meta.get("subagent_turns_used", -1)
    output_len = meta.get("subagent_output_len", -1)

    if turns == 0:
        return VerifierResult(
            passed=False,
            reason="Sub-agent reported success but executed 0 turns — "
                   "the task likely wasn't attempted.",
            signal="no_execution",
        )
    # 50 chars is a deliberate floor — shorter than a typical one-sentence
    # reply. Legitimate sub-agents answering simple questions exceed this;
    # suspiciously empty returns sit well below.
    if 0 <= output_len < 50:
        return VerifierResult(
            passed=False,
            reason=f"Sub-agent output suspiciously short ({output_len} chars) "
                   f"despite {turns} turn(s) — likely didn't complete the task.",
            signal="empty_output",
        )
    return VerifierResult(passed=True)


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
                episodic=parent_session._memory.episodic,
                semantic=parent_session._memory.semantic,
                procedural=parent_session._memory.procedural,
                tool_registry=parent_session.registry,
                parent_session_id=parent_session.session_id,
                workspace=parent_session.workspace,
                parent_grants=parent_session.perm.grants,
                scratchpad=getattr(parent_session, "_scratchpad", None),
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
            # Surface structured signals for _verify_spawn_agent (Issue #196).
            metadata = {
                "subagent_agent_id": result.agent_id,
                "subagent_turns_used": result.turns_used,
                "subagent_tool_calls": result.tool_calls,
                "subagent_output_len": len(result.output or ""),
            }
            return ToolResult(call_id=call.id, tool_name=call.tool_name,
                              success=True, output=header + result.output,
                              metadata=metadata)
        else:
            # Issue #192 P0 / #193 P1: attach failure context, code, and
            # recovery hint so the parent can choose a next action without
            # parsing strings. Full trace at scratchpad_read(subagent_failure:...).
            prefix = f"[{result.failure_code}] " if result.failure_code else ""
            parts = [prefix + (result.error or "Sub-agent failed")]
            if result.last_tool_name:
                parts.append(
                    f"last_tool={result.last_tool_name}"
                    + (f" error={result.last_tool_error!r}" if result.last_tool_error else "")
                )
            if result.recovery_suggestion:
                parts.append(f"hint: {result.recovery_suggestion}")
            # Issue #225: if the sub-agent committed a best-effort result via
            # result_write, surface it inline so the parent can act on it
            # without a follow-up scratchpad_read. Truncate aggressively —
            # full content stays in the scratchpad payload.
            if result.result_slot:
                slot_preview = result.result_slot
                if len(slot_preview) > 800:
                    slot_preview = slot_preview[:800] + " […truncated]"
                parts.append(f"result_slot: {slot_preview}")
            parts.append(
                f"Full failure context at scratchpad ref: subagent_failure:{result.agent_id} "
                f"(read via scratchpad_read)."
            )
            metadata = {
                "subagent_agent_id": result.agent_id,
                "subagent_turns_used": result.turns_used,
                "subagent_tool_calls": result.tool_calls,
                "subagent_failure_code": result.failure_code or "",
                "subagent_has_result_slot": bool(result.result_slot),
            }
            return ToolResult(call_id=call.id, tool_name=call.tool_name,
                              success=False, error=" | ".join(parts),
                              failure_type="execution_error",
                              metadata=metadata)

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
        post_validator=_verify_spawn_agent,
    )


# run_bash is registered via make_run_bash_tool(workspace, strict_sandbox) in
# LoomSession.start() so the sandbox setting can be wired in from loom.toml.
# read_file / write_file / list_dir are registered via make_filesystem_tools(workspace).


# ── Issue #205: Single-tool TaskList — task_write replaces 5 tools ─────────
#
# Replaces task_plan / task_status / task_modify / task_done / task_read.
# Rationale: task_done was a "verb" — calling it felt like reporting progress
# to the framework, even when no artifact existed. The cognitive substitution
# (call → "I'm done") was the root cause of false-completion failures in long
# multi-step tasks. task_write is an "edit" — agent rewrites the whole list,
# changing status fields. No verb, no ceremony, no illusion of being tracked.
#
# All real outputs go to disk via write_file; the TaskList only tracks
# "have I forgotten this step?".

def make_task_write_tool(manager: "TaskListManager") -> ToolDefinition:
    """Create the task_write tool — replace-the-whole-list semantics."""

    async def _task_write(call: ToolCall) -> ToolResult:
        todos = call.args.get("todos")
        if todos is None or not isinstance(todos, list):
            return ToolResult(
                call_id=call.id, tool_name=call.tool_name,
                success=False, error="'todos' is required and must be a list",
            )
        try:
            summary = manager.write(todos)
        except ValueError as exc:
            return ToolResult(
                call_id=call.id, tool_name=call.tool_name,
                success=False, error=str(exc),
            )
        return ToolResult(
            call_id=call.id, tool_name=call.tool_name,
            success=True,
            output=json.dumps(summary, ensure_ascii=False),
        )

    return ToolDefinition(
        name="task_write",
        description=(
            "Maintain your todo list for multi-step work. Pass the FULL "
            "intended list every time — this replaces the previous list. "
            "Each todo is {id, content, status} where status is "
            "'pending' | 'in_progress' | 'completed'. To 'complete' a "
            "step, rewrite the list with that item's status changed.\n\n"
            "This is your sticky-note board — a reminder of what's left, "
            "not a state container. Real outputs (reports, code, data) "
            "MUST go to files via write_file. The todo list never holds "
            "the result itself, only the fact that the step exists.\n\n"
            "Use it when a goal has 3+ coordinated steps and forgetting "
            "one would be costly. Skip it for trivial single-turn work. "
            "Pass an empty list to clear."
        ),
        trust_level=TrustLevel.SAFE,
        capabilities=ToolCapability.NONE,
        input_schema={
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "description": "Full intended todo list. Replaces any prior list.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {
                                "type": "string",
                                "description": "Short unique id (e.g. 'research', 'draft', 'audit').",
                            },
                            "content": {
                                "type": "string",
                                "description": "One-line description of the step.",
                            },
                            "status": {
                                "type": "string",
                                "enum": ["pending", "in_progress", "completed"],
                                "description": "Current status. Defaults to 'pending'.",
                            },
                        },
                        "required": ["id", "content"],
                    },
                },
            },
            "required": ["todos"],
        },
        executor=_task_write,
        tags=["task", "planning"],
        impact_scope="agent",
        inline_only=True,  # Output is a structured status JSON; #197
    )


# ------------------------------------------------------------------
# Issue #154: Job inspection & Scratchpad tools
# ------------------------------------------------------------------


def _fmt_job(job: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": job.id,
        "fn": job.fn_name,
        "state": job.state.value,
        "submitted_at": job.submitted_at,
    }
    if job.started_at is not None:
        out["started_at"] = job.started_at
    if job.finished_at is not None:
        out["finished_at"] = job.finished_at
    if job.elapsed_seconds is not None:
        out["elapsed_seconds"] = round(job.elapsed_seconds, 2)
    if job.result_ref:
        out["result_ref"] = f"scratchpad://{job.result_ref}"
    if job.result_summary:
        out["summary"] = job.result_summary
    if job.error:
        out["error"] = job.error
    if job.cancel_reason:
        out["cancel_reason"] = job.cancel_reason
    return out


def make_jobs_list_tool(jobstore: Any) -> ToolDefinition:
    """List all jobs in the current session (active + terminal)."""

    async def _jobs_list(call: ToolCall) -> ToolResult:
        filter_state = (call.args.get("state") or "").strip().lower()
        jobs = jobstore.list_all()
        if filter_state == "active":
            jobs = [j for j in jobs if not j.is_terminal]
        elif filter_state:
            jobs = [j for j in jobs if j.state.value == filter_state]
        payload = {
            "count": len(jobs),
            "jobs": [_fmt_job(j) for j in jobs],
        }
        return ToolResult(
            call_id=call.id, tool_name=call.tool_name,
            success=True, output=json.dumps(payload, indent=2),
        )

    return ToolDefinition(
        name="jobs_list",
        description=(
            "List background jobs in the current session. "
            "Optional 'state' filter: 'active' (running+pending), or a specific "
            "state like 'done'/'failed'/'cancelled'."
        ),
        trust_level=TrustLevel.SAFE,
        capabilities=ToolCapability.NONE,
        input_schema={
            "type": "object",
            "properties": {
                "state": {"type": "string", "description": "Optional filter"},
            },
        },
        executor=_jobs_list,
        tags=["jobs"],
        impact_scope="agent",
    )


def make_jobs_status_tool(jobstore: Any) -> ToolDefinition:
    """Look up a single job by id."""

    async def _jobs_status(call: ToolCall) -> ToolResult:
        job_id = (call.args.get("job_id") or "").strip()
        if not job_id:
            return ToolResult(call_id=call.id, tool_name=call.tool_name,
                              success=False, error="'job_id' is required")
        job = jobstore.get(job_id)
        if job is None:
            return ToolResult(call_id=call.id, tool_name=call.tool_name,
                              success=False, error=f"Unknown job_id: {job_id}")
        return ToolResult(
            call_id=call.id, tool_name=call.tool_name,
            success=True, output=json.dumps(_fmt_job(job), indent=2),
        )

    return ToolDefinition(
        name="jobs_status",
        description="Get the detailed status of a single background job.",
        trust_level=TrustLevel.SAFE,
        capabilities=ToolCapability.NONE,
        input_schema={
            "type": "object",
            "properties": {"job_id": {"type": "string"}},
            "required": ["job_id"],
        },
        executor=_jobs_status,
        tags=["jobs"],
        impact_scope="agent",
    )


def make_jobs_await_tool(jobstore: Any) -> ToolDefinition:
    """Block until given jobs terminate or timeout expires."""

    async def _jobs_await(call: ToolCall) -> ToolResult:
        ids = call.args.get("job_ids") or []
        if isinstance(ids, str):
            ids = [ids]
        if not ids:
            return ToolResult(call_id=call.id, tool_name=call.tool_name,
                              success=False, error="'job_ids' is required (list of job IDs)")
        timeout = call.args.get("timeout")
        try:
            timeout_f = float(timeout) if timeout is not None else None
        except (TypeError, ValueError):
            return ToolResult(call_id=call.id, tool_name=call.tool_name,
                              success=False, error="'timeout' must be a number (seconds)")

        finished, running = await jobstore.await_jobs(ids, timeout=timeout_f)
        payload = {
            "finished": [_fmt_job(j) for j in finished],
            "still_running": [_fmt_job(j) for j in running],
            "timeout_hit": len(running) > 0,
        }
        return ToolResult(
            call_id=call.id, tool_name=call.tool_name,
            success=True, output=json.dumps(payload, indent=2),
        )

    return ToolDefinition(
        name="jobs_await",
        description=(
            "Wait for one or more jobs to terminate, up to a timeout. "
            "Returns finished and still_running lists; unfinished jobs keep running. "
            "Does NOT raise on timeout — check 'timeout_hit' in the result."
        ),
        trust_level=TrustLevel.SAFE,
        capabilities=ToolCapability.NONE,
        input_schema={
            "type": "object",
            "properties": {
                "job_ids": {"type": "array", "items": {"type": "string"}},
                "timeout": {"type": "number", "description": "Seconds; omit to wait indefinitely (not recommended)."},
            },
            "required": ["job_ids"],
        },
        executor=_jobs_await,
        tags=["jobs"],
        impact_scope="agent",
    )


def make_jobs_cancel_tool(jobstore: Any) -> ToolDefinition:
    """Cancel a running or pending job. Reason is mandatory for traceability."""

    async def _jobs_cancel(call: ToolCall) -> ToolResult:
        job_id = (call.args.get("job_id") or "").strip()
        reason = (call.args.get("reason") or "").strip()
        if not job_id:
            return ToolResult(call_id=call.id, tool_name=call.tool_name,
                              success=False, error="'job_id' is required")
        if not reason:
            return ToolResult(call_id=call.id, tool_name=call.tool_name,
                              success=False, error="'reason' is required — cancellation trace must be preserved")
        try:
            jobstore.cancel(job_id, reason=reason)
        except KeyError:
            return ToolResult(call_id=call.id, tool_name=call.tool_name,
                              success=False, error=f"Unknown job_id: {job_id}")
        job = jobstore.get(job_id)
        return ToolResult(
            call_id=call.id, tool_name=call.tool_name,
            success=True, output=json.dumps(_fmt_job(job), indent=2),
        )

    return ToolDefinition(
        name="jobs_cancel",
        description=(
            "Cancel a running/pending job. Requires 'reason' — the trace is "
            "preserved so you (and future turns) can see why it was stopped."
        ),
        trust_level=TrustLevel.SAFE,
        capabilities=ToolCapability.NONE,
        input_schema={
            "type": "object",
            "properties": {
                "job_id": {"type": "string"},
                "reason": {"type": "string", "description": "Why the job is being cancelled."},
            },
            "required": ["job_id", "reason"],
        },
        executor=_jobs_cancel,
        tags=["jobs"],
        impact_scope="agent",
    )


_SCRATCHPAD_DEFAULT_MAX_BYTES = 200_000


# ── Scratchpad ref naming convention (Issue #197 review) ───────────────────
# Different harness mechanisms write into the same scratchpad; the prefix
# tells you why each ref exists. Add a new prefix here when introducing a
# new producer so scratchpad_read's listing keeps surfacing it correctly.
#
#   auto_<tool>_<6hex>          → JITRetrievalMiddleware (Phase 1, #197)
#                                  Original tool output >threshold; cached
#                                  here, agent saw a JIT placeholder.
#   masked_<tool>_<6hex>        → _apply_observation_masking (Phase 2, #197)
#                                  Older superseded tool call folded for
#                                  token budget; agent saw a fold placeholder.
#   subagent_failure:<agent_id> → run_subagent failure path (#192 P0)
#                                  Diagnostic context from a max_turns or
#                                  loop-detected sub-agent failure.
#   <other>                     → ad-hoc agent or job-pipeline writes.
def _categorize_scratchpad_refs(refs: list[str]) -> dict[str, list[str]]:
    """Group scratchpad refs by their producer prefix."""
    by_kind: dict[str, list[str]] = {
        "jit_spilled": [],
        "observation_masked": [],
        "subagent_failure": [],
        "other": [],
    }
    for ref in refs:
        if ref.startswith("auto_"):
            by_kind["jit_spilled"].append(ref)
        elif ref.startswith("masked_"):
            by_kind["observation_masked"].append(ref)
        elif ref.startswith("subagent_failure:"):
            by_kind["subagent_failure"].append(ref)
        else:
            by_kind["other"].append(ref)
    return by_kind


def make_scratchpad_read_tool(scratchpad: Any) -> ToolDefinition:
    """Read content from the session's Scratchpad."""

    async def _scratchpad_read(call: ToolCall) -> ToolResult:
        ref = (call.args.get("ref") or "").strip()
        if not ref:
            refs = scratchpad.list_refs()
            # Issue #197 Phase 2 review: categorize refs so an agent doing
            # discovery can pick the right one without trial-and-error.
            # ``available_refs`` stays a flat list for backward compat;
            # ``by_kind`` is the new categorized view.
            return ToolResult(
                call_id=call.id, tool_name=call.tool_name,
                success=True,
                output=json.dumps(
                    {
                        "available_refs": refs,
                        "by_kind": _categorize_scratchpad_refs(refs),
                    },
                    indent=2,
                ),
            )
        section = call.args.get("section")
        raw_max = call.args.get("max_bytes")
        if raw_max is None:
            max_bytes = _SCRATCHPAD_DEFAULT_MAX_BYTES
        else:
            try:
                max_bytes = int(raw_max)
                if max_bytes <= 0:
                    max_bytes = _SCRATCHPAD_DEFAULT_MAX_BYTES
            except (TypeError, ValueError):
                return ToolResult(
                    call_id=call.id, tool_name=call.tool_name,
                    success=False, error="'max_bytes' must be a positive integer",
                )
        try:
            content = scratchpad.read(ref, section=section, max_bytes=max_bytes)
        except KeyError as exc:
            return ToolResult(call_id=call.id, tool_name=call.tool_name,
                              success=False, error=str(exc))
        return ToolResult(
            call_id=call.id, tool_name=call.tool_name,
            success=True, output=content,
        )

    return ToolDefinition(
        name="scratchpad_read",
        description=(
            "Read content from the session Scratchpad. Omit 'ref' to list "
            "available refs. Supports section filter: 'head', 'tail', 'N-M' "
            f"for line range, or any string to grep matching lines. Output is "
            f"capped at {_SCRATCHPAD_DEFAULT_MAX_BYTES} bytes by default — "
            "raise 'max_bytes' for larger payloads."
        ),
        trust_level=TrustLevel.SAFE,
        capabilities=ToolCapability.NONE,
        input_schema={
            "type": "object",
            "properties": {
                "ref": {"type": "string", "description": "Scratchpad ref (with or without scratchpad:// prefix). Omit to list all refs."},
                "section": {"type": "string", "description": "Optional filter: head/tail/N-M/keyword."},
                "max_bytes": {"type": "integer", "description": f"Byte cap on raw payload before section filter (default {_SCRATCHPAD_DEFAULT_MAX_BYTES})."},
            },
        },
        executor=_scratchpad_read,
        tags=["jobs", "scratchpad"],
        impact_scope="agent",
        inline_only=True,  # Re-spilling scratchpad reads is paradoxical; #197
    )


# ------------------------------------------------------------------
# Issue #276 — agent-side LLM tier control
# ------------------------------------------------------------------

def make_request_model_tier_tool(session: Any) -> ToolDefinition:
    """Agent-callable: explicitly switch the active LLM tier.

    Issue #276. The harness auto-escalates from skill metadata
    (``model_tier: 2``), but the agent itself sometimes recognises
    "this needs deep reasoning" or "we're done with the hard part" before
    a skill activation makes that obvious. This tool is the explicit
    self-call mechanism — agency via tool, not heuristics.

    Wires through ``session._set_sticky_tier`` and queues the resulting
    ``TierChanged`` event onto the session's lifecycle queue so it
    surfaces alongside other stream events without bespoke plumbing.

    Reason is **mandatory** because each tier switch should leave a
    decision-trace in the envelope log — useful for graphify analysis
    of when 絲絲 self-escalates, and what triggered the call.

    Use ``clear_model_tier`` to end a sticky session and return to
    the default tier.
    """
    async def _executor(call: ToolCall) -> ToolResult:
        try:
            tier = int(call.args.get("tier"))
        except (TypeError, ValueError):
            return ToolResult(
                call_id=call.id, tool_name=call.tool_name,
                success=False,
                error="'tier' must be a positive integer (1, 2, …)",
            )
        reason = str(call.args.get("reason", "")).strip()
        if not reason:
            return ToolResult(
                call_id=call.id, tool_name=call.tool_name,
                success=False,
                error="'reason' is required — describe why this switch is warranted",
            )
        # Validate the requested tier exists in the configured table.
        if tier not in session._tier_models:
            available = sorted(session._tier_models.keys())
            return ToolResult(
                call_id=call.id, tool_name=call.tool_name,
                success=False,
                error=(
                    f"Tier {tier} is not configured. Available tiers: {available}. "
                    f"Configure via [cognition.tiers] in loom.toml."
                ),
            )

        ev = session._set_sticky_tier(tier, reason=reason, source="agent")
        if ev is not None:
            # Surface to the stream loop so platforms see TierChanged in
            # the same channel as ToolBegin / EnvelopeUpdated.
            session._lifecycle_events.put_nowait(ev)

        active_tier = session._active_tier()
        active_model = session._active_model()
        msg = f"Active tier: {active_tier} → {active_model}. (sticky on tier {tier})"
        return ToolResult(
            call_id=call.id, tool_name=call.tool_name,
            success=True, output=msg,
            metadata={
                "tier": active_tier,
                "model": active_model,
                "sticky": session._sticky_tier,
            },
        )

    return ToolDefinition(
        name="request_model_tier",
        description=(
            "Switch the active LLM tier (Issue #276). Use when the current "
            "task's reasoning load doesn't match the active engine — e.g. "
            "escalate to Tier 2 before tackling a multi-constraint puzzle, "
            "or step back to Tier 1 when a deep phase has concluded. "
            "To clear a sticky session, use ``clear_model_tier`` instead. "
            "Reason is mandatory: it lands in the envelope log."
        ),
        trust_level=TrustLevel.SAFE,
        capabilities=ToolCapability.NONE,
        input_schema={
            "type": "object",
            "properties": {
                "tier": {
                    "type": "integer",
                    "description": "Target tier (1=daily, 2=deep, …); must be configured in loom.toml.",
                },
                "reason": {
                    "type": "string",
                    "description": "Why the switch is warranted — one short sentence.",
                },
            },
            "required": ["tier", "reason"],
        },
        executor=_executor,
        tags=["cognition", "tier"],
        impact_scope="agent",
    )


def make_clear_model_tier_tool(session: Any) -> ToolDefinition:
    """Agent-callable: explicitly clear a sticky tier session.

    Issue #278. Companion to ``request_model_tier`` — when a deep phase
    is done, the agent calls this to release the sticky tier and return
    to ``default_tier``.  No tier argument needed because the intent is
    unambiguous: "I'm done, go back to normal."

    Wires through ``session._set_sticky_tier(None, ...)`` — same
    underlying state machine as ``request_model_tier``.
    """
    async def _executor(call: ToolCall) -> ToolResult:
        reason = str(call.args.get("reason", "")).strip()
        if not reason:
            return ToolResult(
                call_id=call.id, tool_name=call.tool_name,
                success=False,
                error="'reason' is required — describe why the sticky session is ending",
            )
        sticky_before = session._sticky_tier
        ev = session._set_sticky_tier(None, reason=reason, source="agent")
        if ev is not None:
            session._lifecycle_events.put_nowait(ev)

        active_tier = session._active_tier()
        active_model = session._active_model()
        if sticky_before is None:
            msg = f"No active sticky to clear (already on tier {active_tier} — {active_model})."
        else:
            old_model = session._tier_models.get(sticky_before, "?")
            msg = f"Sticky cleared (was T{sticky_before} {old_model}). Now T{active_tier} {active_model} (default)."
        return ToolResult(
            call_id=call.id, tool_name=call.tool_name,
            success=True, output=msg,
            metadata={
                "tier": active_tier,
                "model": active_model,
                "sticky": session._sticky_tier,
                "previous_sticky": sticky_before,
            },
        )

    return ToolDefinition(
        name="clear_model_tier",
        description=(
            "Clear a sticky tier session (Issue #278). Use when a deep "
            "phase has concluded and the agent should return to the "
            "default tier. No tier argument — the intent is unambiguous: "
            "release the sticky and follow default_tier."
        ),
        trust_level=TrustLevel.SAFE,
        capabilities=ToolCapability.NONE,
        input_schema={
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "Why the sticky session is ending — one short sentence.",
                },
            },
            "required": ["reason"],
        },
        executor=_executor,
        tags=["cognition", "tier"],
        impact_scope="agent",
    )
