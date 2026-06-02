    # ==============================================================
    # SECTION 1 — HELPERS (imports / _race_abort)
    # ==============================================================
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
import base64
import json
import mimetypes
import os
import re
import signal
import subprocess
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from loom.core.harness.middleware import ToolCall, ToolResult, VerifierResult
from loom.core.harness.permissions import ToolCapability, TrustLevel
from loom.core.harness.scope import ScopeRequirement, ScopeRequest
from loom.core.timezone import local_zone_name, user_zone
from loom.core.security.self_termination_guard import SelfTerminationGuard
from loom.core.security.command_scanner import CommandScanner
from loom.core.security.sandbox_runtime import (
    SandboxSettings,
    srt_available,
    srt_install_hint,
    wrap_command as _srt_wrap_command,
    write_settings_file as _srt_write_settings_file,
)

import logging

_log = logging.getLogger(__name__)
_self_term_guard = SelfTerminationGuard()
_cmd_scanner = CommandScanner()


def _killpg_quiet(pid: int, sig: int) -> None:
    """Best-effort process-group signal. Swallows ProcessLookupError and
    PermissionError so cancellation/timeout cleanup never raises."""
    try:
        os.killpg(os.getpgid(pid), sig)
    except (ProcessLookupError, PermissionError):
        pass


async def _terminate_proc_group(proc: asyncio.subprocess.Process) -> None:
    """Reap a subprocess that was launched with start_new_session=True.

    SIGTERM the whole process group, give it 2s, then SIGKILL. Bounded
    waits ensure cancellation/timeout cleanup never hangs even if a
    pipe child is wedged on a write to a closed fd.
    """
    if proc.returncode is not None:
        return
    _killpg_quiet(proc.pid, signal.SIGTERM)
    try:
        await asyncio.wait_for(proc.wait(), timeout=2)
    except (asyncio.TimeoutError, BaseException):
        _killpg_quiet(proc.pid, signal.SIGKILL)
        try:
            await proc.wait()
        except BaseException:
            pass

if TYPE_CHECKING:
    from collections.abc import Callable
    from loom.core.ledger import LedgerEmitter, LedgerStore
    from loom.core.harness.skill_checks import SkillCheckManager
    from loom.core.memory.facade import MemoryFacade
    from loom.core.memory.procedural import ProceduralMemory, SkillGenome
    from loom.core.memory.search import MemorySearch
    from loom.core.memory.semantic import SemanticMemory
    from loom.core.memory.governance import MemoryGovernor
    from loom.core.memory.skill_outcome import SkillOutcomeTracker
    from loom.core.infra.telemetry import AgentTelemetryTracker
    from loom.core.tasks.manager import TaskListManager
    from loom.core.tasks.pursuit import PursuitStore

_WEB_TIMEOUT = 10.0       # seconds for all HTTP calls
_CONTENT_LIMIT = 2000     # max chars returned to agent
_SEARCH_RESULTS = 5       # default Brave results
_IMAGE_TIMEOUT = 180.0
_TTS_TIMEOUT = 180.0
_TTS_CODECS = ("mp3", "wav", "pcm", "mulaw", "alaw")
_TTS_CODEC_SUFFIX = {
    "mp3": ".mp3",
    "wav": ".wav",
    "pcm": ".pcm",
    "mulaw": ".ulaw",
    "alaw": ".alaw",
}


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


def _make_openai_image_scope_resolver(workspace: Path):
    """Scope resolver for the OpenAI-backed image generation tool."""
    import os.path

    _workspace_resolved = workspace.resolve()

    def _resolve(call: ToolCall) -> ScopeRequest:
        raw = call.args.get("output_path") or call.args.get("path") or "outputs"
        p = Path(str(raw))
        resolved = (workspace / p).resolve() if not p.is_absolute() else p.resolve()
        try:
            selector = str(resolved.parent.relative_to(_workspace_resolved))
        except ValueError:
            selector = os.path.normpath(str(resolved.parent))
        requirements = [
            ScopeRequirement(
                resource="path",
                action="write",
                selector=selector,
                tool_name=call.tool_name,
                capabilities=call.capabilities,
            ),
            ScopeRequirement(
                resource="network",
                action="connect",
                selector="api.openai.com",
                tool_name=call.tool_name,
                capabilities=call.capabilities,
            ),
            ScopeRequirement(
                resource="network",
                action="connect",
                selector="chatgpt.com",
                tool_name=call.tool_name,
                capabilities=call.capabilities,
            ),
        ]
        for ref in call.args.get("subject_reference") or []:
            if not isinstance(ref, dict) or not ref.get("image_file"):
                continue
            ref_path = _resolve_workspace_path(str(ref["image_file"]), workspace)
            try:
                ref_selector = str(ref_path.relative_to(_workspace_resolved))
            except ValueError:
                ref_selector = os.path.normpath(str(ref_path))
            requirements.append(
                ScopeRequirement(
                    resource="path",
                    action="read",
                    selector=ref_selector,
                    tool_name=call.tool_name,
                    capabilities=call.capabilities,
                )
            )
        return ScopeRequest(
            tool_name=call.tool_name,
            capabilities=call.capabilities,
            requirements=requirements,
        )
    return _resolve


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


def _make_run_bash_resolver(
    workspace: Path, sandbox: SandboxSettings | None = None,
):
    """
    Return a scope_resolver for run_bash bound to *workspace*.

    Limitations (documented in Phase A plan):
    - Pipes, subshells, variable expansion → scope unknown → fallback to CONFIRM
    - Only does token-level path extraction (aligned with exec_escape_fn)
    """
    import os.path

    _SCOPE_UNKNOWN_PATTERNS = re.compile(r'[\|`]|\$\(|\$\{|\$[A-Za-z]|<<|&&|\|\|')

    def _select_profile(command: str, call: ToolCall) -> tuple[str | None, dict]:
        if sandbox is None or sandbox.backend != "srt" or not sandbox.profiles:
            return None, {}
        explicit_profile = (call.args.get("sandbox_profile") or "").strip() or None
        profile_name = sandbox.select_profile_for_command(
            command, explicit=explicit_profile,
        )
        if not profile_name:
            return None, {}
        profile = sandbox.profiles[profile_name]
        return profile_name, {
            "allow_read": list(profile.allow_read),
            "allow_write": list(profile.allow_write),
            "allowed_domains": list(profile.allowed_domains),
            "allowed_sockets": list(profile.allowed_sockets),
        }

    def _profile_requirement(
        call: ToolCall, profile_name: str | None, constraints: dict,
    ) -> list[ScopeRequirement]:
        if not profile_name:
            return []
        return [
            ScopeRequirement(
                resource="sandbox_profile",
                action="use",
                selector=profile_name,
                constraints=constraints,
                tool_name=call.tool_name,
                capabilities=call.capabilities,
            )
        ]

    def _resolve(call: ToolCall) -> ScopeRequest:
        command = call.args.get("command", "")
        profile_name, profile_constraints = _select_profile(command, call)

        # If command contains patterns we can't analyze, mark scope unknown
        if _SCOPE_UNKNOWN_PATTERNS.search(command):
            requirements = [
                ScopeRequirement(
                    resource="exec",
                    action="execute",
                    selector="workspace",
                    constraints={"scope_unknown": True},
                    tool_name=call.tool_name,
                    capabilities=call.capabilities,
                ),
            ]
            requirements.extend(
                _profile_requirement(call, profile_name, profile_constraints)
            )
            metadata = {"scope_unknown": True}
            if profile_name:
                metadata["sandbox_profile"] = profile_name
            return ScopeRequest(
                tool_name=call.tool_name,
                capabilities=call.capabilities,
                requirements=requirements,
                metadata=metadata,
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

        requirements = [
            ScopeRequirement(
                resource="exec",
                action="execute",
                selector="workspace",
                constraints={"has_absolute_paths": has_absolute},
                tool_name=call.tool_name,
                capabilities=call.capabilities,
            ),
        ]
        requirements.extend(
            _profile_requirement(call, profile_name, profile_constraints)
        )
        metadata = {"sandbox_profile": profile_name} if profile_name else {}
        return ScopeRequest(
            tool_name=call.tool_name,
            capabilities=call.capabilities,
            requirements=requirements,
            metadata=metadata,
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
    # ==============================================================
    # SECTION 2 — FILESYSTEM TOOLS (make_filesystem_tools, make_run_bash_tool)
    # ==============================================================

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

    async def _see_image(call: ToolCall) -> ToolResult:
        # Agent-initiated vision: load a local image so the model can
        # actually see its pixels (not just the path). Reuses the vision
        # validation stack (magic-byte MIME, size cap, digest); the image
        # rides back as a canonical block in result.metadata and the
        # session attaches it to the tool_result.
        # Local import: keep the vision module (and any heavy image deps)
        # off the CLI-startup import path; only paid when see_image runs.
        from loom.core.cognition import vision as _vision

        raw = call.args.get("path", "")
        path = _resolve_workspace_path(raw, workspace)
        try:
            vi = _vision.load_vision_input(path, origin="agent")
        except _vision.VisionInputError as exc:
            return ToolResult(call_id=call.id, tool_name=call.tool_name,
                              success=False, error=f"無法載入圖片：{exc}")
        except Exception as exc:
            return ToolResult(call_id=call.id, tool_name=call.tool_name,
                              success=False, error=f"讀取圖片失敗：{exc}")
        block = _vision.make_image_block(vi)
        kb = vi.size_bytes / 1024
        return ToolResult(
            call_id=call.id, tool_name=call.tool_name, success=True,
            output=(
                f"已載入圖片 {raw}（{vi.media_type}, {kb:.0f} KB）。"
                f"圖片內容現在可被你直接看見——請描述或分析你所見。"
            ),
            metadata={"vision_image": block},
        )

    return [
        ToolDefinition(
            name="see_image",
            description=(
                "View a local image file with your native multimodal vision — "
                "you SEE the actual pixels, not just the path. Use this when "
                "you need to look at an image you found, were given a path to, "
                "or generated yourself (rather than routing through an external "
                "image-understanding tool). Relative paths resolve inside the "
                "workspace. Requires a vision-capable model (e.g. MiniMax-M3)."
            ),
            trust_level=TrustLevel.SAFE,
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string",
                             "description": "Image path (relative to workspace or absolute)"},
                },
                "required": ["path"],
            },
            executor=_see_image,
            tags=["filesystem", "vision", "read"],
            impact_scope="filesystem",
        ),
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
            # Issue #302 F1: source files in the 8K–24K char range are
            # nearly always wanted inline (mid-size Python modules, configs).
            # Spilling them forces a redundant scratchpad_read round trip.
            # 24K ≈ ~6K tokens — still safely under typical attention budgets.
            spill_threshold_chars=24000,
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
    sandbox: SandboxSettings | None = None,
) -> ToolDefinition:
    """
    Return the ``run_bash`` tool definition.

    When *strict_sandbox* is True (set via ``[harness] strict_sandbox = true``
    in loom.toml), the subprocess is launched with ``cwd=workspace`` so that
    relative paths and shell builtins stay inside the project folder.  This
    does not prevent absolute-path escapes at the OS level — for full
    confinement turn on the *sandbox* backend below.

    Issue #29 / Quest A Phase 4: when *sandbox* has ``backend == "srt"``, the
    final shell command is wrapped with ``srt -s <settings> -c <cmd>`` so the
    OS kernel enforces filesystem and network restrictions.  Configure via
    ``[security.sandbox]`` in loom.toml.  Fail-closed: if the binary is
    missing we raise at startup rather than silently downgrading.

    Issue #154: when ``async_mode=True`` is passed in call.args and a jobstore
    is available, the shell invocation is submitted as a background Job and
    the tool returns immediately with ``{"job_id": "..."}``.  Results land in
    the Scratchpad; harness injects status updates at turn boundaries.
    """
    _sandbox_active = sandbox is not None and sandbox.backend == "srt"
    _sandbox_settings_path: Path | None = None
    if _sandbox_active:
        if not srt_available():
            raise RuntimeError(
                "[security.sandbox] backend = \"srt\" is enabled but "
                + srt_install_hint()
            )
        _sandbox_settings_path = _srt_write_settings_file(sandbox, workspace)

    def _selected_profile_for_call(call: ToolCall) -> str | None:
        if not _sandbox_active or sandbox is None or not sandbox.profiles:
            return None
        command = call.args.get("command", "")
        explicit = (call.args.get("sandbox_profile") or "").strip() or None
        return sandbox.select_profile_for_command(command, explicit=explicit)

    def _has_profile_requirement(call: ToolCall, profile_name: str) -> bool:
        scope_request = call.metadata.get("scope_request")
        requirements = getattr(scope_request, "requirements", []) or []
        for req in requirements:
            if (
                getattr(req, "resource", None) == "sandbox_profile"
                and getattr(req, "action", None) == "use"
                and getattr(req, "selector", None) == profile_name
            ):
                return True
        return False

    def _profile_authorized_for_call(call: ToolCall, profile_name: str) -> bool:
        if not _has_profile_requirement(call, profile_name):
            return False
        verdict = call.metadata.get("scope_verdict")
        verdict_value = getattr(verdict, "value", verdict)
        return (
            call.metadata.get("once_authorized") is True
            or call.metadata.get("user_decision") is True
            or verdict_value == "allow"
        )

    def _maybe_wrap(cmd: str, call: ToolCall) -> tuple[str, str | None]:
        if not _sandbox_active or _sandbox_settings_path is None:
            return cmd, None
        profile_name = _selected_profile_for_call(call)
        settings_path = _sandbox_settings_path
        if profile_name:
            if sandbox is None or not _profile_authorized_for_call(call, profile_name):
                raise PermissionError(
                    f"sandbox profile '{profile_name}' requires explicit authorization"
                )
            profile = sandbox.profiles[profile_name]
            if profile.bypass_sandbox:
                # Issue #402: profile opts out of srt entirely. Required for
                # CLIs like `gh` that fail TLS through srt's HTTP CONNECT
                # proxy. Authorization above still gates activation.
                return cmd, profile_name
            settings_path = _srt_write_settings_file(
                sandbox.merged_with_profile(profile_name),
                workspace,
            )
        return _srt_wrap_command(cmd, settings_path), profile_name
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
        try:
            wrapped_command, sandbox_profile = _maybe_wrap(command, call)
        except PermissionError as exc:
            return ToolResult(
                call_id=call.id, tool_name=call.tool_name,
                success=False, error=str(exc),
                failure_type="permission_denied",
            )
        except ValueError as exc:
            return ToolResult(
                call_id=call.id, tool_name=call.tool_name,
                success=False, error=str(exc),
                failure_type="validation_error",
            )

        if async_mode and jobstore is not None and scratchpad is not None:
            payload = {"command": command, "timeout": timeout}
            metadata = {"async": True}
            if sandbox_profile:
                payload["sandbox_profile"] = sandbox_profile
                metadata["sandbox_profile"] = sandbox_profile
            job_id = jobstore.submit(
                "run_bash",
                payload,
                lambda: _run_bash_job(wrapped_command, cwd, timeout, scratchpad),
            )
            metadata["job_id"] = job_id
            return ToolResult(
                call_id=call.id, tool_name=call.tool_name,
                success=True,
                output=f"Submitted as {job_id}. Poll with jobs_status or jobs_await.",
                metadata=metadata,
            )

        try:
            # Issue #312: start_new_session=True puts the shell in its own
            # process group so killpg can reap pipe children (e.g.
            # `git log | head`) — proc.kill() alone only signals the shell
            # PID, leaving children holding stdout fds and communicate()
            # blocked indefinitely past cancel/timeout.
            proc = await asyncio.create_subprocess_shell(
                wrapped_command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=cwd,
                start_new_session=True,
            )
            try:
                try:
                    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
                except asyncio.TimeoutError:
                    await _terminate_proc_group(proc)
                    return ToolResult(call_id=call.id, tool_name=call.tool_name,
                                      success=False, error=f"Command timed out after {timeout}s",
                                      failure_type="timeout")
            finally:
                # Issue #222 / #312: cancellation (Discord cancel-and-relaunch,
                # _abort.abort(), CLI /stop) bypasses the TimeoutError branch
                # — reap the whole process group so pipe children don't keep
                # the pipe open and stall communicate().
                await _terminate_proc_group(proc)

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

            metadata = {"exit_code": proc.returncode}
            if sandbox_profile:
                metadata["sandbox_profile"] = sandbox_profile
            return ToolResult(
                call_id=call.id, tool_name=call.tool_name,
                success=success, output=output,
                error=None if success else f"Exit code {proc.returncode}",
                metadata=metadata,
            )
        except Exception as exc:
            return ToolResult(call_id=call.id, tool_name=call.tool_name,
                              success=False, error=str(exc))

    sandbox_note = " Shell is confined to the workspace directory." if strict_sandbox else ""
    if _sandbox_active:
        sandbox_note += (
            " OS-level sandbox (srt) is active: filesystem writes and network "
            "access are restricted to the allowlists in [security.sandbox]; "
            "unlisted domains and writes outside allowed paths will fail with "
            "EPERM or 'Connection blocked'."
        )
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
                "sandbox_profile": {"type": "string", "description": "Optional named sandbox profile to request for this command."},
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
        scope_resolver=_make_run_bash_resolver(workspace, sandbox=sandbox),
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
            start_new_session=True,  # Issue #312: see _run_bash for rationale
        )
        try:
            try:
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                await _terminate_proc_group(proc)
                return None, None, f"Command timed out after {timeout}s"
        finally:
            # Issue #222 / #312: reap the whole process group on cancellation
            # so async jobs don't orphan pipe children.
            await _terminate_proc_group(proc)
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

def _parse_memory_datetime(value: str | None) -> datetime | None:
    """Parse a tool-facing date/datetime string into an aware UTC datetime.

    Naked dates (``YYYY-MM-DD``) and naked datetimes (no offset) are
    interpreted in the user's configured timezone — matching what the
    agent sees in injected prompt timestamps. Strings with an explicit
    offset (``+08:00`` / ``Z``) are honored as written. The return value
    is always normalised to UTC so callers can compare it against DB
    timestamps directly. See issue #388.
    """
    if not value:
        return None
    raw = value.strip()
    if not raw:
        return None
    zone = user_zone()
    try:
        if len(raw) == 10 and raw[4] == "-" and raw[7] == "-":
            return datetime.fromisoformat(raw).replace(tzinfo=zone).astimezone(UTC)
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"Invalid datetime {value!r}; use YYYY-MM-DD or ISO datetime") from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=zone).astimezone(UTC)
    return parsed.astimezone(UTC)


def _format_local_timestamp(
    value: datetime | str | None, *, date_only: bool = False
) -> str:
    """Format a stored UTC datetime (or ISO string) in the user's zone.

    Agent-facing tool output mirrors the same timezone the agent sees in
    injected prompt timestamps, so since/until inputs and emitted
    created_at/updated_at lines match. Returns ``""`` for falsy input.
    See issue #388.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return ""
        try:
            value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return raw[:10] if date_only else raw[:16]
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    local = value.astimezone(user_zone())
    return local.strftime("%Y-%m-%d" if date_only else "%Y-%m-%d %H:%M")


def make_recall_tool(memory: "MemoryFacade") -> ToolDefinition:
    """
    Create a SAFE ``recall`` tool bound to the given MemoryFacade.

    # ==============================================================
    # SECTION 3 — MEMORY + LEDGER RECALL TOOLS
    #   make_recall, make_recall_period, make_memorize, make_memory_health,
    #   make_agent_health, make_probe_file, make_relate, make_query_relations,
    #   make_ledger_recall_tool
    # NOTE: Skill-loading tools moved to loom.platform.cli.skill_tools
    #       (audit-B / PR #399 — was nested here and pretending to be MEMORY).
    # ==============================================================
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
        try:
            since = _parse_memory_datetime(call.args.get("since"))
            until = _parse_memory_datetime(call.args.get("until"))
        except ValueError as exc:
            return ToolResult(call_id=call.id, tool_name=call.tool_name,
                              success=False, error=str(exc))

        if not query:
            return ToolResult(call_id=call.id, tool_name=call.tool_name,
                              success=False, error="'query' argument is required")
        if since is None and until is not None:
            return ToolResult(call_id=call.id, tool_name=call.tool_name,
                              success=False, error="'since' is required when 'until' is provided")
        if since is not None and until is not None and until <= since:
            return ToolResult(call_id=call.id, tool_name=call.tool_name,
                              success=False, error="'until' must be after 'since'")

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
            since=since, until=until,
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
                "since": {
                    "type": "string",
                    "description": (
                        "Optional lower bound for semantic updated_at "
                        "(YYYY-MM-DD or ISO datetime). Naked values are "
                        "interpreted in your configured timezone."
                    ),
                },
                "until": {
                    "type": "string",
                    "description": (
                        "Optional exclusive upper bound for semantic updated_at "
                        "(YYYY-MM-DD or ISO datetime). Naked values are "
                        "interpreted in your configured timezone."
                    ),
                },
            },
            "required": ["query"],
        },
        executor=_recall,
        tags=["memory", "search", "recall"],
        impact_scope="memory",
    )


def _format_period_results(groups: dict[str, list]) -> str:
    """Format grouped period recall evidence for the agent.

    Timestamps are converted to the user's configured timezone so they
    match what the agent sees in injected prompt timestamps and what
    since/until inputs are parsed against.
    """
    sections: list[str] = []
    zone_suffix = f" ({local_zone_name()})"
    semantic = groups.get("semantic", [])
    if semantic:
        lines = [f"Semantic facts{zone_suffix}"]
        for entry in semantic:
            stamp = _format_local_timestamp(entry.updated_at, date_only=True)
            lines.append(f"- [{stamp}] {entry.key}: {entry.value[:240]}")
        sections.append("\n".join(lines))

    episodic = groups.get("episodic", [])
    if episodic:
        lines = [f"Episodic events{zone_suffix}"]
        for entry in episodic:
            stamp = _format_local_timestamp(entry.created_at)
            lines.append(
                f"- [{stamp}] {entry.session_id} {entry.event_type}: "
                f"{entry.content[:240]}"
            )
        sections.append("\n".join(lines))

    sessions = groups.get("sessions", [])
    if sessions:
        lines = [f"Sessions{zone_suffix}"]
        for session in sessions:
            title = session.get("title") or "(untitled)"
            stamp = _format_local_timestamp(session.get("last_active"))
            lines.append(
                f"- [{stamp}] "
                f"{session.get('session_id')}: {title} "
                f"({session.get('turn_count', 0)} turns)"
            )
        sections.append("\n".join(lines))

    messages = groups.get("messages", [])
    if messages:
        lines = [f"Session messages{zone_suffix}"]
        for message in messages:
            stamp = _format_local_timestamp(message.get("created_at"))
            lines.append(
                f"- [{stamp}] "
                f"{message.get('session_id')} {message.get('role')}: "
                f"{str(message.get('content') or '')[:240]}"
            )
        sections.append("\n".join(lines))

    return "\n\n".join(sections) if sections else "No memories found for this period."


def make_recall_period_tool(memory: "MemoryFacade") -> ToolDefinition:
    """Create a SAFE period recall tool across memory layers."""

    async def _recall_period(call: ToolCall) -> ToolResult:
        try:
            since = _parse_memory_datetime(call.args.get("since"))
            until = _parse_memory_datetime(call.args.get("until"))
        except ValueError as exc:
            return ToolResult(call_id=call.id, tool_name=call.tool_name,
                              success=False, error=str(exc))
        if since is None:
            return ToolResult(call_id=call.id, tool_name=call.tool_name,
                              success=False, error="'since' argument is required")
        if until is not None and until <= since:
            return ToolResult(call_id=call.id, tool_name=call.tool_name,
                              success=False, error="'until' must be after 'since'")

        limit = min(max(int(call.args.get("limit", 10)), 1), 20)
        include_episodic = bool(call.args.get("include_episodic", True))
        include_sessions = bool(call.args.get("include_sessions", False))
        session_id = (call.args.get("session_id") or "").strip() or None

        groups = await memory.recall_period(
            since=since,
            until=until,
            session_id=session_id,
            limit=limit,
            include_episodic=include_episodic,
            include_sessions=include_sessions,
        )
        return ToolResult(
            call_id=call.id,
            tool_name=call.tool_name,
            success=True,
            output=_format_period_results(groups),
        )

    return ToolDefinition(
        name="recall_period",
        description=(
            "Retrieve time-window memory evidence. Use for questions like "
            "'what did we discuss yesterday?' Start with semantic facts and "
            "include episodic/session evidence when deeper recall is needed."
        ),
        trust_level=TrustLevel.SAFE,
        input_schema={
            "type": "object",
            "properties": {
                "since": {
                    "type": "string",
                    "description": (
                        "Required lower bound (YYYY-MM-DD or ISO datetime). "
                        "Naked values are interpreted in your configured "
                        "timezone (same as the prompt timestamp prefix)."
                    ),
                },
                "until": {
                    "type": "string",
                    "description": (
                        "Optional exclusive upper bound (YYYY-MM-DD or ISO "
                        "datetime). Naked values are interpreted in your "
                        "configured timezone."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results per layer (1-20, default 10).",
                },
                "include_episodic": {
                    "type": "boolean",
                    "description": "Include episodic events (default true).",
                },
                "include_sessions": {
                    "type": "boolean",
                    "description": "Include matching session metadata (default false).",
                },
                "session_id": {
                    "type": "string",
                    "description": "Optional session scope for episodic events.",
                },
            },
            "required": ["since"],
        },
        executor=_recall_period,
        tags=["memory", "search", "recall", "period"],
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

        # Near-dup hint: surface recent semantically-similar entries so the
        # agent can self-correct duplicate writes. Catches operational-record
        # bloat (memory-hygiene reflection, #398). Never blocks the write;
        # the agent decides whether the new fact is genuinely distinct.
        near_dups: list = []
        try:
            near_dups = await memory.semantic.find_near_duplicates(
                value,
                min_similarity=0.85,
                within_days=7,
                limit=3,
                exclude_key=key,
            )
        except Exception:
            pass  # embedding/db failure must never block memorize

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

        if near_dups:
            dup_lines = []
            for dup_entry, score in near_dups:
                snippet = dup_entry.value[:60] + ("…" if len(dup_entry.value) > 60 else "")
                dup_lines.append(f"  • {dup_entry.key!r} (sim={score:.2f}) — {snippet}")
            msg += (
                "\n\n⚠ near-duplicate(s) in last 7d — verify this is a distinct "
                "insight, not an operational repeat:\n" + "\n".join(dup_lines)
            )

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
    :meth:`MemoryFacade.relate`. Since #451 phase B the triple lands in
    the semantic store via the relational bridge — there is no separate
    relational table any more, and ``recall`` is the sole read path
    (no separate ``query_relations`` tool).
    """
    from loom.core.memory.relational_bridge import (
        RelationalEntry, delete_triple,
    )

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
            await delete_triple(memory.semantic, subject, predicate)
            return ToolResult(call_id=call.id, tool_name="relate",
                              success=True,
                              output=f"Rolled back: deleted ({subject!r}, {predicate!r})")
        except Exception as exc:
            return ToolResult(call_id=call.id, tool_name="relate",
                              success=False, error=f"Rollback failed: {exc}")

    return ToolDefinition(
        name="relate",
        description=(
            "Store a relationship triple (subject, predicate, object) in memory. "
            "Use this to record durable facts about preferences, constraints, or associations. "
            "Example: relate(subject='user', predicate='prefers', object='concise answers'). "
            "Upserting with the same subject+predicate replaces the previous object. "
            "Reading these back is just `recall` — there is no separate query tool."
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


# ------------------------------------------------------------------
# Skill tools — load_skill / unload_skill / skill_review
# Moved to loom.platform.cli.skill_tools (audit-B / #399).
# ------------------------------------------------------------------


# ------------------------------------------------------------------
# ledger_recall tool (Issue #385) — narrative recall over AgentLedger
# ------------------------------------------------------------------


_LEDGER_RECALL_EVENT_CAP = 1000
"""Hard cap on events pulled per ledger_recall call. Renderers truncate
further per their own semantics (narrative trims to N turn slices, raw
trims to N events). Prevents runaway queries from drowning the agent."""


def make_ledger_recall_tool(
    ledger_store: "LedgerStore | None",
    memory: "MemoryFacade | None",
) -> ToolDefinition:
    """Create a SAFE ``ledger_recall`` tool — story-form recall over the ledger.

    Wraps the doc/53 §6.2 Pull API (``LedgerStore.events``) and joins
    against ``memory.session_log`` so each turn's narrative paragraph can
    quote the user's actual message. Issue #385 design summary:
    https://github.com/Dakai666/Loom/issues/385#issuecomment-4471257209.
    """
    from datetime import datetime, timezone

    from loom.core.ledger.recall import (
        group_events_by_turn,
        infer_output_format,
        render_narrative,
        render_raw,
        render_summary,
    )

    _DIMENSION_KEYS = (
        "session_id", "correlation_id", "skill_id",
        "tool_name", "verdict", "since", "until",
    )

    async def _ledger_recall(call: ToolCall) -> ToolResult:
        if ledger_store is None:
            return ToolResult(
                call_id=call.id, tool_name=call.tool_name,
                success=False,
                error="Ledger is not available in this session; cannot recall.",
            )

        args = call.args or {}
        session_id = (args.get("session_id") or "").strip() or None
        correlation_id = (args.get("correlation_id") or "").strip() or None
        skill_id = (args.get("skill_id") or "").strip() or None
        tool_name = (args.get("tool_name") or "").strip() or None
        verdict = (args.get("verdict") or "").strip() or None
        explicit_output = (args.get("output") or "").strip() or None
        verbose = bool(args.get("verbose", False))

        try:
            limit = int(args.get("limit", 20))
        except (TypeError, ValueError):
            return ToolResult(
                call_id=call.id, tool_name=call.tool_name,
                success=False, error="'limit' must be an integer",
            )
        limit = max(1, min(limit, 100))

        try:
            since_dt = _parse_memory_datetime(args.get("since"))
            until_dt = _parse_memory_datetime(args.get("until"))
        except ValueError as exc:
            return ToolResult(
                call_id=call.id, tool_name=call.tool_name,
                success=False, error=str(exc),
            )

        # Require at least one dimension — refuses unbounded ledger scans.
        if not any([
            session_id, correlation_id, skill_id,
            tool_name, verdict, since_dt, until_dt,
        ]):
            return ToolResult(
                call_id=call.id, tool_name=call.tool_name,
                success=False,
                error=(
                    "ledger_recall requires at least one dimension: "
                    + ", ".join(_DIMENSION_KEYS)
                ),
            )

        # Build the EventQuery chain with all user-supplied filters.
        # Fetch cap+1 so we can detect truncation atomically (no separate
        # COUNT query) — anything beyond _LEDGER_RECALL_EVENT_CAP is a
        # signal that aggregates / chronology may be incomplete.
        query = _build_query(
            ledger_store,
            session_id=session_id,
            correlation_id=correlation_id,
            skill_id=skill_id,
            tool_name=tool_name,
            verdict=verdict,
            since_dt=since_dt,
            until_dt=until_dt,
        )
        events = await query.all()
        truncated = len(events) > _LEDGER_RECALL_EVENT_CAP
        if truncated:
            events = events[:_LEDGER_RECALL_EVENT_CAP]

        output_format = infer_output_format(
            session_id=session_id,
            correlation_id=correlation_id,
            skill_id=skill_id,
            tool_name=tool_name,
            verdict=verdict,
            since=args.get("since"),
            until=args.get("until"),
            explicit=explicit_output,
        )

        if output_format == "narrative":
            # Narrative needs full turn context (turn_start, user message
            # window, every tool, turn_end). When narrowing filters
            # (skill_id / tool_name / verdict) sit on top of a scope
            # filter, the initial fetch returns only matching events —
            # turn_start / turn_end / preceding user message get filtered
            # out, breaking the story.
            #
            # Hydrate by turn_id directly rather than re-querying scope:
            # the narrow query already pinpointed the turns we care about,
            # and `idx_turn` makes per-turn lookups cheap. Re-querying
            # scope risks losing matched turns whose timestamps fall
            # outside the broad fetch's _LEDGER_RECALL_EVENT_CAP window
            # (e.g. matched turn at event #1500 of a long session) — see
            # PR #386 review.
            has_narrowing = bool(skill_id or tool_name or verdict)
            has_scope = bool(
                session_id or correlation_id
                or since_dt is not None or until_dt is not None
            )
            if has_narrowing and has_scope:
                # Preserve first-seen turn order from the initial query so
                # `limit` selects the chronologically earliest matches.
                ordered_turn_ids: list[str] = []
                seen_turn_ids: set[str] = set()
                for e in events:
                    if e.turn_id not in seen_turn_ids:
                        seen_turn_ids.add(e.turn_id)
                        ordered_turn_ids.append(e.turn_id)
                ordered_turn_ids = ordered_turn_ids[:limit]

                hydrated: list = []
                for tid in ordered_turn_ids:
                    turn_q = (
                        ledger_store.events
                        .where(turn_id=tid)
                        .order_by("timestamp")
                        .limit(_LEDGER_RECALL_EVENT_CAP + 1)
                    )
                    turn_events = await turn_q.all()
                    if len(turn_events) > _LEDGER_RECALL_EVENT_CAP:
                        truncated = True
                        turn_events = turn_events[
                            :_LEDGER_RECALL_EVENT_CAP
                        ]
                    hydrated.extend(turn_events)
                events = hydrated

            slices = group_events_by_turn(events)[:limit]
            messages_by_turn = await _fetch_user_messages_per_turn(
                memory, slices,
            )
            text = render_narrative(
                slices, messages_by_turn,
                verbose=verbose, truncated=truncated,
            )
        elif output_format == "raw":
            text = render_raw(
                events, max_events=limit, truncated=truncated,
            )
        else:
            text = render_summary(events, truncated=truncated)

        return ToolResult(
            call_id=call.id, tool_name=call.tool_name,
            success=True, output=text,
        )

    def _build_query(
        store: "LedgerStore",
        *,
        session_id: str | None = None,
        correlation_id: str | None = None,
        skill_id: str | None = None,
        tool_name: str | None = None,
        verdict: str | None = None,
        since_dt=None,
        until_dt=None,
    ):
        q = store.events
        if session_id:
            q = q.where(session_id=session_id)
        if correlation_id:
            q = q.where(correlation_id=correlation_id)
        if skill_id:
            q = q.where(skill_id=skill_id)
        if tool_name:
            q = q.where(tool_name=tool_name)
        if verdict:
            q = q.where(verdict=verdict)
        if since_dt is not None:
            q = q.since(since_dt)
        if until_dt is not None:
            q = q.until(until_dt)
        return q.order_by("timestamp").limit(_LEDGER_RECALL_EVENT_CAP + 1)

    async def _fetch_user_messages_per_turn(
        memory: "MemoryFacade | None",
        slices: list,
    ) -> dict[str, list[dict]]:
        """Pull user messages from session_log for each turn's time window.

        Per-turn query rather than a single bulk pull: ledger ``turn_id``
        and session_log ``turn_index`` are different keys, so we map via
        the turn's [started_at, ended_at] window. Bounded by the renderer's
        turn limit (≤100), so per-call query count is small.
        """
        if memory is None or memory.session_log is None:
            return {}
        out: dict[str, list[dict]] = {}
        for sl in slices:
            start_dt = datetime.fromtimestamp(sl.started_at, tz=timezone.utc)
            # If turn never ended in the result set, take the last event's ts
            # as a generous upper bound (events are already sorted ASC).
            end_ts = sl.ended_at if sl.ended_at is not None else (
                sl.events[-1].timestamp if sl.events else sl.started_at
            )
            # +1s slack so an end-aligned message isn't excluded.
            end_dt = datetime.fromtimestamp(end_ts + 1.0, tz=timezone.utc)
            try:
                rows = await memory.session_log.messages_between(
                    start_dt, end_dt,
                    session_id=sl.session_id, limit=10,
                )
            except Exception:
                rows = []
            out[sl.turn_id] = rows
        return out

    return ToolDefinition(
        name="ledger_recall",
        description=(
            "Recall past activity from the event ledger as story-form "
            "narrative. Use when you want to remember 'what happened "
            "in session X' or 'what did I do with skill Y this week'. "
            "Defaults to per-turn narrative for session/correlation "
            "queries; aggregate summary for skill/tool/time-range queries. "
            "Read-only."
        ),
        trust_level=TrustLevel.SAFE,
        input_schema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "correlation_id": {"type": "string"},
                "skill_id": {"type": "string"},
                "tool_name": {"type": "string"},
                "verdict": {
                    "type": "string",
                    "description": "e.g. 'success' / 'error' (from JudgeVerdict).",
                },
                "since": {
                    "type": "string",
                    "description": (
                        "Lower bound (YYYY-MM-DD or ISO datetime). Naked "
                        "values are interpreted in your configured "
                        "timezone (same as the prompt timestamp prefix)."
                    ),
                },
                "until": {
                    "type": "string",
                    "description": (
                        "Exclusive upper bound. Naked values are "
                        "interpreted in your configured timezone."
                    ),
                },
                "output": {
                    "type": "string",
                    "enum": ["narrative", "summary", "raw"],
                    "description": (
                        "Override the auto-inferred format. Default: "
                        "narrative for session/correlation; summary otherwise."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": (
                        "For narrative: max turns. For raw: max events. "
                        "Ignored for summary. 1–100, default 20."
                    ),
                    "default": 20,
                },
                "verbose": {
                    "type": "boolean",
                    "description": (
                        "Narrative only — list every tool call instead of "
                        "compacting when a turn has many actions."
                    ),
                    "default": False,
                },
            },
            "required": [],
        },
        executor=_ledger_recall,
        tags=["memory", "recall", "ledger", "read-only"],
        impact_scope="read-only",
    )


# ------------------------------------------------------------------
# Skill lifecycle tools (Issue #120 PR 3)
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
    # ==============================================================
    # SECTION 5 — VERIFICATION (verify_run_bash, verify_fetch_url, verify_spawn_agent)
    # ==============================================================
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
    # ==============================================================
    # SECTION 6 — WEB TOOLS (make_fetch_url_tool, make_web_search_tool, make_spawn_agent_tool)
    # ==============================================================

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


def _extract_image_b64(data: Any) -> str:
    """Find the newest base64 image field in a Responses payload."""

    found = ""

    def _walk(value: Any) -> None:
        nonlocal found
        if isinstance(value, dict):
            for key, child in value.items():
                if (
                    key in {"partial_image_b64", "image_b64", "b64_json"}
                    and isinstance(child, str)
                    and child
                ):
                    found = child
                else:
                    _walk(child)
        elif isinstance(value, list):
            for child in value:
                _walk(child)

    _walk(data)
    return found


async def _decode_openai_image_response(
    client: httpx.AsyncClient,
    data: dict[str, Any],
) -> bytes:
    """Decode an OpenAI Images API response into image bytes."""

    first = (data.get("data") or [{}])[0]
    if first.get("b64_json"):
        return base64.b64decode(first["b64_json"])
    if first.get("url"):
        img = await client.get(first["url"])
        img.raise_for_status()
        return img.content
    raise RuntimeError("OpenAI image response did not include b64_json or url.")


def _build_openai_subject_reference_content(
    raw_refs: Any,
    workspace: Path,
) -> list[dict[str, Any]]:
    """Convert subject_reference entries into Codex Responses input_image items."""

    if raw_refs in (None, ""):
        return []
    if not isinstance(raw_refs, list):
        raise ValueError("subject_reference must be an array.")

    content: list[dict[str, Any]] = []
    for idx, ref in enumerate(raw_refs):
        if not isinstance(ref, dict):
            raise ValueError(f"subject_reference[{idx}] must be an object.")
        image_file = str(ref.get("image_file", "")).strip()
        if not image_file:
            raise ValueError(f"subject_reference[{idx}].image_file is required.")
        image_path = _resolve_workspace_path(image_file, workspace)
        try:
            image_bytes = image_path.read_bytes()
        except OSError as exc:
            raise ValueError(
                f"Could not read subject_reference[{idx}].image_file: {image_file}"
            ) from exc
        mime_type = (
            mimetypes.guess_type(str(image_path))[0]
            or _detect_image_mime_type(image_bytes)
            or "image/png"
        )
        encoded = base64.b64encode(image_bytes).decode("ascii")
        content.append({
            "type": "input_image",
            "image_url": f"data:{mime_type};base64,{encoded}",
        })
    return content


def _detect_image_mime_type(image_bytes: bytes) -> str | None:
    """Best-effort image MIME detection from magic bytes."""

    if image_bytes.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if image_bytes.startswith(b"RIFF") and image_bytes[8:12] == b"WEBP":
        return "image/webp"
    if image_bytes.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    return None


def make_openai_image_generation_tool(
    workspace: Path,
    *,
    tool_name: str = "image_generate",
    default_model: str = "gpt-image-2",
    default_auth_mode: str = "auto",
) -> ToolDefinition:
    """Build the canonical image_generate tool backed by OpenAI GPT Image."""

    from loom.core.session import _load_env
    from loom.core.cognition.openai_auth import resolve_openai_credential

    async def _openai_image(call: ToolCall) -> ToolResult:
        prompt = str(call.args.get("prompt", "")).strip()
        if not prompt:
            return ToolResult(
                call_id=call.id,
                tool_name=call.tool_name,
                success=False,
                error="Missing required argument: prompt",
            )

        auth_mode = str(call.args.get("auth_mode", default_auth_mode)).strip().lower()
        if auth_mode not in {"auto", "codex", "api_key"}:
            return ToolResult(
                call_id=call.id,
                tool_name=call.tool_name,
                success=False,
                error="auth_mode must be one of: auto, codex, api_key",
            )

        env = _load_env()
        credential = resolve_openai_credential(
            env,
            prefer_codex=auth_mode != "api_key",
            require_codex=auth_mode == "codex",
        )
        if credential is None:
            detail = (
                "Run `codex login` for Codex OAuth or set OPENAI_API_KEY."
                if auth_mode == "auto"
                else "Run `codex login`; no unexpired Codex OAuth token was found."
                if auth_mode == "codex"
                else "Set OPENAI_API_KEY in .env or the environment."
            )
            return ToolResult(
                call_id=call.id,
                tool_name=call.tool_name,
                success=False,
                error=f"No usable OpenAI credential. {detail}",
            )

        raw_output = str(call.args.get("output_path", "")).strip()
        output_path = (
            _resolve_workspace_path(raw_output, workspace)
            if raw_output else
            workspace / "outputs" / f"openai-image-{uuid.uuid4().hex[:8]}.png"
        )
        if output_path.suffix.lower() not in {".png", ".webp", ".jpg", ".jpeg"}:
            output_path = output_path.with_suffix(".png")
        output_path.parent.mkdir(parents=True, exist_ok=True)

        image_model = str(call.args.get("model", default_model)).strip() or default_model
        image_options: dict[str, Any] = {
            "model": image_model,
            "n": int(call.args.get("n", 1) or 1),
        }
        for key in ("size", "quality", "background", "moderation"):
            value = call.args.get(key)
            if isinstance(value, str) and value.strip():
                image_options[key] = value.strip()
        try:
            subject_reference_content = _build_openai_subject_reference_content(
                call.args.get("subject_reference"),
                workspace,
            )
        except ValueError as exc:
            return ToolResult(
                call_id=call.id,
                tool_name=call.tool_name,
                success=False,
                error=str(exc),
            )

        async def _request_openai_images(client: httpx.AsyncClient, bearer: str):
            payload = {"prompt": prompt, **image_options}
            resp = await client.post(
                "https://api.openai.com/v1/images/generations",
                headers={"Authorization": f"Bearer {bearer}"},
                json=payload,
            )
            resp.raise_for_status()
            return resp.json()

        async def _request_codex_responses(client: httpx.AsyncClient, bearer: str) -> str:
            output_format = output_path.suffix.lower().lstrip(".") or "png"
            if output_format == "jpg":
                output_format = "jpeg"
            tool_spec = {
                "type": "image_generation",
                "model": image_model,
                "size": image_options.get("size", "auto"),
                "quality": image_options.get("quality", "auto"),
                "output_format": output_format,
            }
            for key in ("background", "moderation"):
                value = image_options.get(key)
                if isinstance(value, str) and value.strip():
                    tool_spec[key] = value.strip()

            payload = {
                "model": str(call.args.get("codex_model", "gpt-5.5")).strip() or "gpt-5.5",
                "instructions": (
                    "Use image generation to create the requested image. "
                    "Return no extra text unless needed."
                ),
                "input": [{
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": prompt},
                        *subject_reference_content,
                    ],
                }],
                "tools": [tool_spec],
                "stream": True,
                "store": False,
            }
            latest_b64 = ""
            async with client.stream(
                "POST",
                "https://chatgpt.com/backend-api/codex/responses",
                headers={
                    "Authorization": f"Bearer {bearer}",
                    "Content-Type": "application/json",
                    "Accept": "text/event-stream",
                },
                json=payload,
            ) as resp:
                resp.raise_for_status()
                event: str | None = None
                data_lines: list[str] = []
                async for line in resp.aiter_lines():
                    if line.startswith("event: "):
                        event = line[7:]
                    elif line.startswith("data: "):
                        data_lines.append(line[6:])
                    elif line == "" and event:
                        raw = "\n".join(data_lines)
                        try:
                            data = json.loads(raw)
                        except json.JSONDecodeError:
                            data = {}
                        found = _extract_image_b64(data)
                        if found:
                            latest_b64 = found
                        if event in {"response.completed", "response.failed"}:
                            break
                        event = None
                        data_lines = []
            if not latest_b64:
                raise RuntimeError("Codex Responses backend did not return an image payload.")
            return latest_b64

        try:
            async with httpx.AsyncClient(timeout=_IMAGE_TIMEOUT) as client:
                if credential.source == "codex_oauth":
                    try:
                        b64_image = await _request_codex_responses(client, credential.token)
                        image_bytes = base64.b64decode(b64_image)
                        credential_source = credential.source
                    except httpx.HTTPStatusError as exc:
                        fallback = resolve_openai_credential(env, prefer_codex=False)
                        can_fallback = (
                            auth_mode == "auto"
                            and fallback is not None
                            and fallback.source == "api_key"
                            and exc.response.status_code in {401, 403}
                            and not subject_reference_content
                        )
                        if not can_fallback:
                            raise
                        data = await _request_openai_images(client, fallback.token)
                        image_bytes = await _decode_openai_image_response(client, data)
                        credential_source = fallback.source
                else:
                    if subject_reference_content:
                        reason = (
                            "You selected auth_mode=api_key, which does not support subject_reference. "
                            if auth_mode == "api_key" else ""
                        )
                        return ToolResult(
                            call_id=call.id,
                            tool_name=call.tool_name,
                            success=False,
                            error=(
                                f"{reason}subject_reference currently requires Codex OAuth "
                                "(use auth_mode=codex or run `codex login` with auth_mode=auto)."
                            ),
                        )
                    data = await _request_openai_images(client, credential.token)
                    image_bytes = await _decode_openai_image_response(client, data)
                    credential_source = credential.source
        except httpx.HTTPStatusError as exc:
            body = exc.response.text[:500] if exc.response is not None else ""
            return ToolResult(
                call_id=call.id,
                tool_name=call.tool_name,
                success=False,
                error=f"OpenAI image request failed with HTTP {exc.response.status_code}: {body}",
            )
        except Exception as exc:
            return ToolResult(
                call_id=call.id,
                tool_name=call.tool_name,
                success=False,
                error=f"OpenAI image request failed: {type(exc).__name__}: {exc}",
            )

        output_path.write_bytes(image_bytes)
        try:
            display_path = str(output_path.relative_to(workspace))
        except ValueError:
            display_path = str(output_path)
        return ToolResult(
            call_id=call.id,
            tool_name=call.tool_name,
            success=True,
            output=json.dumps({
                "path": display_path,
                "model": image_model,
                "credential_source": credential_source,
                "bytes": len(image_bytes),
            }, ensure_ascii=False),
        )

    return ToolDefinition(
        name=tool_name,
        description=(
            "Generate an image with the configured default image provider and "
            "save it to a workspace file. This installation routes the request "
            f"through OpenAI GPT Image model {default_model}."
        ),
        trust_level=TrustLevel.GUARDED,
        capabilities=ToolCapability.NETWORK | ToolCapability.MUTATES,
        input_schema={
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Image prompt."},
                "output_path": {
                    "type": "string",
                    "description": "Workspace-relative output path. Defaults to outputs/openai-image-<id>.png.",
                },
                "model": {
                    "type": "string",
                    "description": "OpenAI image model.",
                    "default": default_model,
                },
                "size": {
                    "type": "string",
                    "description": "Image size supported by the selected model, e.g. auto or 1024x1024.",
                    "default": "auto",
                },
                "quality": {
                    "type": "string",
                    "description": "Quality supported by the selected model, e.g. auto, low, medium, high.",
                    "default": "auto",
                },
                "auth_mode": {
                    "type": "string",
                    "enum": ["auto", "codex", "api_key"],
                    "default": default_auth_mode,
                    "description": "auto prefers Codex OAuth and falls back to OPENAI_API_KEY.",
                },
                "subject_reference": {
                    "type": "array",
                    "description": (
                        "Reference images for subject or character consistency. "
                        "Currently supported on the Codex OAuth path."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "type": {
                                "type": "string",
                                "description": "Reference type, e.g. character.",
                                "default": "character",
                            },
                            "image_file": {
                                "type": "string",
                                "description": "Workspace-relative reference image path.",
                            },
                        },
                        "required": ["image_file"],
                        "additionalProperties": False,
                    },
                },
                "n": {"type": "integer", "minimum": 1, "maximum": 1, "default": 1},
            },
            "required": ["prompt"],
            "additionalProperties": False,
        },
        executor=_openai_image,
        tags=["image", "openai"],
        impact_scope="network",
        scope_descriptions=[
            "connects to api.openai.com for API-key image generation",
            "connects to chatgpt.com for Codex OAuth image generation",
            "writes one generated image file under the workspace",
        ],
        scope_resolver=_make_openai_image_scope_resolver(workspace),
    )


def _make_xai_tts_scope_resolver(workspace: Path):
    """Scope resolver for the xAI-backed TTS generation tool."""
    import os.path

    _workspace_resolved = workspace.resolve()

    def _resolve(call: ToolCall) -> ScopeRequest:
        # When ``output_path`` is omitted the executor writes to
        # ``outputs/tts-<id>.<codec>``. Mirror that here so the
        # permission selector reflects the real mutation surface
        # (``outputs``) instead of widening it to the workspace root.
        raw = call.args.get("output_path") or "outputs/tts-default"
        p = Path(str(raw))
        resolved = (workspace / p).resolve() if not p.is_absolute() else p.resolve()
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
                ScopeRequirement(
                    resource="network",
                    action="connect",
                    selector="api.x.ai",
                    tool_name=call.tool_name,
                    capabilities=call.capabilities,
                ),
            ],
        )

    return _resolve


def make_xai_tts_tool(
    workspace: Path,
    *,
    tool_name: str = "tts_generate",
    default_voice_id: str = "eve",
    default_language: str = "auto",
    default_format: str = "mp3",
    default_auth_mode: str = "oauth",
    default_sample_rate: int | None = None,
    default_bit_rate: int | None = None,
) -> ToolDefinition:
    """Build the canonical tts_generate tool backed by xAI TTS (OAuth-only).

    No XAI_API_KEY fallback by design — mirrors the xai/<model> chat provider
    so an OAuth-only path cannot silently spend API-key quota.
    """

    from loom.core.cognition.xai_auth import load_xai_oauth_credential

    async def _xai_tts(call: ToolCall) -> ToolResult:
        text = str(call.args.get("text", "")).strip()
        if not text:
            return ToolResult(
                call_id=call.id,
                tool_name=call.tool_name,
                success=False,
                error="Missing required argument: text",
            )

        auth_mode = str(call.args.get("auth_mode", default_auth_mode)).strip().lower()
        if auth_mode != "oauth":
            return ToolResult(
                call_id=call.id,
                tool_name=call.tool_name,
                success=False,
                error="auth_mode must be: oauth (xAI TTS is OAuth-only in Loom).",
            )

        credential = load_xai_oauth_credential()
        if credential is None:
            return ToolResult(
                call_id=call.id,
                tool_name=call.tool_name,
                success=False,
                error=(
                    "No unexpired xAI OAuth token found. "
                    "Run `loom auth xai` before using tts_generate."
                ),
            )

        codec = str(call.args.get("format", default_format)).strip().lower() or default_format
        if codec not in _TTS_CODEC_SUFFIX:
            return ToolResult(
                call_id=call.id,
                tool_name=call.tool_name,
                success=False,
                error=f"format must be one of: {list(_TTS_CODECS)}",
            )

        raw_output = str(call.args.get("output_path", "")).strip()
        suffix = _TTS_CODEC_SUFFIX[codec]
        if raw_output:
            output_path = _resolve_workspace_path(raw_output, workspace)
        else:
            output_path = workspace / "outputs" / f"tts-{uuid.uuid4().hex[:8]}{suffix}"
        output_path.parent.mkdir(parents=True, exist_ok=True)

        voice_id = str(call.args.get("voice_id", default_voice_id)).strip() or default_voice_id
        language = str(call.args.get("language", default_language)).strip() or default_language

        def _coerce_positive_int(value: Any, fallback: int | None) -> int | None:
            if isinstance(value, bool):
                return fallback
            if isinstance(value, (int, float)):
                ivalue = int(value)
                return ivalue if ivalue > 0 else fallback
            return fallback

        sample_rate = _coerce_positive_int(call.args.get("sample_rate"), default_sample_rate)
        bit_rate = _coerce_positive_int(call.args.get("bit_rate"), default_bit_rate)

        output_format: dict[str, Any] = {"codec": codec}
        if sample_rate is not None:
            output_format["sample_rate"] = sample_rate
        if bit_rate is not None and codec == "mp3":
            output_format["bit_rate"] = bit_rate

        payload: dict[str, Any] = {
            "text": text,
            "voice_id": voice_id,
            "language": language,
            "output_format": output_format,
        }
        speed = call.args.get("speed")
        if isinstance(speed, (int, float)) and not isinstance(speed, bool):
            payload["speed"] = float(speed)

        try:
            async with httpx.AsyncClient(timeout=_TTS_TIMEOUT) as client:
                resp = await client.post(
                    "https://api.x.ai/v1/tts",
                    headers={
                        "Authorization": f"Bearer {credential.token}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
                resp.raise_for_status()
                audio_bytes = resp.content
        except httpx.HTTPStatusError as exc:
            body = exc.response.text[:500] if exc.response is not None else ""
            return ToolResult(
                call_id=call.id,
                tool_name=call.tool_name,
                success=False,
                error=f"xAI TTS request failed with HTTP {exc.response.status_code}: {body}",
            )
        except Exception as exc:
            return ToolResult(
                call_id=call.id,
                tool_name=call.tool_name,
                success=False,
                error=f"xAI TTS request failed: {type(exc).__name__}: {exc}",
            )

        if not audio_bytes:
            return ToolResult(
                call_id=call.id,
                tool_name=call.tool_name,
                success=False,
                error="xAI TTS returned empty audio payload.",
            )

        output_path.write_bytes(audio_bytes)
        try:
            display_path = str(output_path.relative_to(workspace))
        except ValueError:
            display_path = str(output_path)
        return ToolResult(
            call_id=call.id,
            tool_name=call.tool_name,
            success=True,
            output=json.dumps({
                "path": display_path,
                "provider": "xai",
                "voice_id": voice_id,
                "format": codec,
                "bytes": len(audio_bytes),
            }, ensure_ascii=False),
        )

    return ToolDefinition(
        name=tool_name,
        description=(
            "Generate speech audio from text via xAI TTS (OAuth-only) and save "
            "it to a workspace file. Defaults to MP3 with built-in voice `eve`."
        ),
        trust_level=TrustLevel.GUARDED,
        capabilities=ToolCapability.NETWORK | ToolCapability.MUTATES,
        input_schema={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Text to convert to speech."},
                "output_path": {
                    "type": "string",
                    "description": (
                        "Workspace-relative output path. "
                        "Defaults to outputs/tts-<id>.<codec-ext>."
                    ),
                },
                "voice_id": {
                    "type": "string",
                    "description": (
                        "Built-in xAI voice (eve, ara, leo, rex, sal) "
                        "or a custom voice id."
                    ),
                    "default": default_voice_id,
                },
                "language": {
                    "type": "string",
                    "description": "BCP-47 language code (e.g. en, zh, pt-BR) or `auto`.",
                    "default": default_language,
                },
                "format": {
                    "type": "string",
                    "enum": list(_TTS_CODECS),
                    "description": "Audio codec for the saved file.",
                    "default": default_format,
                },
                "speed": {
                    "type": "number",
                    "description": "Speaking rate multiplier (xAI default applies when omitted).",
                },
                "sample_rate": {
                    "type": "integer",
                    "minimum": 1,
                    "description": (
                        "Output sample rate in Hz. Omit to use xAI default "
                        "(typically 24000)."
                    ),
                },
                "bit_rate": {
                    "type": "integer",
                    "minimum": 1,
                    "description": (
                        "Output bit rate in bps. Only applied for mp3; omit to "
                        "use xAI default (typically 128000)."
                    ),
                },
                "auth_mode": {
                    "type": "string",
                    "enum": ["oauth"],
                    "default": default_auth_mode,
                    "description": "xAI TTS is OAuth-only; XAI_API_KEY is intentionally ignored.",
                },
            },
            "required": ["text"],
            "additionalProperties": False,
        },
        executor=_xai_tts,
        tags=["tts", "audio", "xai"],
        impact_scope="network",
        scope_descriptions=[
            "connects to api.x.ai for xAI OAuth text-to-speech",
            "writes one generated audio file under the workspace",
        ],
        scope_resolver=_make_xai_tts_scope_resolver(workspace),
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
                ledger_emitter=getattr(parent_session, "_ledger_emitter", None),
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
            "Use this to delegate research, file analysis, or external review tasks. "
            "The sub-agent cannot interact with the user — it works autonomously until done.\n"
            "\n"
            "Tool scope: by default the sub-agent only sees SAFE tools (read_file, "
            "fetch_url, web_search, list_dir, scratchpad_read, plus result_write). "
            "If your task description requires write_file, run_bash, or any other "
            "GUARDED tool, you MUST list it in `tools=[...]` — otherwise the "
            "sub-agent will reach for a tool that does not exist and burn turns. "
            "Result hand-off goes through the sub-agent's result_write slot; the "
            "sub-agent CANNOT persist files for you under default tool scope, "
            "so prefer instructing it to put everything in result_write and "
            "letting the parent persist the artifact after."
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
                    "items": {"type": "string"},
                    "description": (
                        "Tool whitelist for the sub-agent. "
                        "List of tool names (e.g. ['read_file', 'web_search']) or comma-separated string. "
                        "Omit or null for SAFE-only tools. CRITICAL tools are always blocked. "
                        "MUST explicitly include GUARDED tools like 'write_file' or 'run_bash' if "
                        "the task needs them — otherwise the sub-agent has no way to call them."
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

# ==============================================================
# SECTION 7 — JOBS TOOLS (make_task_write_tool, make_jobs_*_tool, make_scratchpad_read_tool)
# ==============================================================

def make_task_write_tool(
    manager: "TaskListManager",
    ledger_emitter: "LedgerEmitter | None" = None,
) -> ToolDefinition:
    """Create the task_write tool — replace-the-whole-list semantics.

    When ``ledger_emitter`` is provided, every successful write emits a
    ``task_mutation`` event (doc/53 §3.1) carrying the full post-write
    status summary as ``task_state``. Per-task done/modify/abandon are
    reader-side derivations from successive snapshots, not separate
    events — post-#205 the write tool is the only mutation entry point.
    """

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

        if ledger_emitter is not None:
            from loom.core.ledger import TaskMutationPayload

            # NOTE: task_state currently embeds the full status_summary
            # snapshot per write. For long task lists this duplicates data
            # across successive events; future revision may switch to a
            # digest-only payload with a separate snapshot event channel
            # (consider when median list_length × write_count exceeds the
            # ledger row budget). v1 keeps full snapshot — readers can
            # answer "what did the list look like at event N?" with one
            # row instead of folding diffs.
            try:
                await ledger_emitter.emit_task_mutation(
                    payload=TaskMutationPayload(
                        operation="write",
                        task_id=f"tasklist:{manager.session_id}",
                        task_state=summary,
                    ),
                )
            except Exception:  # noqa: BLE001
                _log.exception("ledger task_mutation emit failed; continuing")

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
            "Each todo is {id, content, status, done_when} where status is "
            "'pending' | 'in_progress' | 'completed'. To 'complete' a "
            "step, rewrite the list with that item's status changed.\n\n"
            "This is your sticky-note board — a reminder of what's left, "
            "not a state container. Real outputs (reports, code, data) "
            "MUST go to files via write_file. The todo list never holds "
            "the result itself, only the fact that the step exists.\n\n"
            "'done_when' is your own acceptance criterion for the step — "
            "one short phrase describing the evidence that would prove "
            "the step is finished (e.g. 'P0–P3 findings each have "
            "file/line/evidence', 'embed renders correctly in Discord'). "
            "Writing it forces you to define done before you act, and "
            "you re-read it every time you update the list. Leave it "
            "empty when you genuinely cannot yet say what done looks "
            "like — empty values render as '—' so the gap stays visible "
            "and you can fill it on a later edit.\n\n"
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
                            "done_when": {
                                "type": "string",
                                "description": (
                                    "Acceptance criterion — one short "
                                    "phrase describing the evidence that "
                                    "proves this step is finished. "
                                    "Optional; empty renders as '—'."
                                ),
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
# Issue #375: Pursuit tools — long-lived task artifacts (markdown files
# under ~/.loom/pursuits/). Pursuit ≠ memory: pursuit is task-axis,
# memory.db is the agent's cognition. Three thin tools wrap PursuitStore;
# the agent orchestrates `pursuit_read` + web tools + `pursuit_write`
# herself rather than a monolithic `pursuit_pull` primitive.
# ------------------------------------------------------------------


def make_pursuit_list_tool(store: "PursuitStore") -> ToolDefinition:
    async def _pursuit_list(call: ToolCall) -> ToolResult:
        ids = store.list()
        payload = {"count": len(ids), "pursuits": ids}
        return ToolResult(
            call_id=call.id, tool_name=call.tool_name,
            success=True,
            output=json.dumps(payload, ensure_ascii=False),
        )

    return ToolDefinition(
        name="pursuit_list",
        description=(
            "List all active pursuits — long-lived tracking tasks the user "
            "has asked you to follow (e.g. 'OpenAI 世紀審判'). Returns ids "
            "you can pass to pursuit_read/pursuit_write. Use this when the "
            "user asks 'what am I tracking' or before answering 'how's X?' "
            "without an id in mind."
        ),
        trust_level=TrustLevel.SAFE,
        capabilities=ToolCapability.NONE,
        input_schema={"type": "object", "properties": {}},
        executor=_pursuit_list,
        tags=["task", "pursuit"],
        impact_scope="agent",
        inline_only=True,
    )


def make_pursuit_read_tool(store: "PursuitStore") -> ToolDefinition:
    async def _pursuit_read(call: ToolCall) -> ToolResult:
        pid = (call.args.get("id") or "").strip()
        if not pid:
            return ToolResult(
                call_id=call.id, tool_name=call.tool_name,
                success=False, error="'id' is required",
            )
        try:
            content = store.read(pid)
        except ValueError as exc:
            return ToolResult(
                call_id=call.id, tool_name=call.tool_name,
                success=False, error=str(exc),
            )
        payload = {"id": pid, "content": content}
        return ToolResult(
            call_id=call.id, tool_name=call.tool_name,
            success=True,
            output=json.dumps(payload, ensure_ascii=False),
        )

    return ToolDefinition(
        name="pursuit_read",
        description=(
            "Read a pursuit's full markdown by id. Returns the dimensions, "
            "thresholds, and accumulated snapshots the user has been "
            "tracking. Use when the user asks about a specific tracked "
            "topic. After reading, you typically run search/fetch tools "
            "yourself to gather fresh data, then call pursuit_write with "
            "an appended snapshot section."
        ),
        trust_level=TrustLevel.SAFE,
        capabilities=ToolCapability.NONE,
        input_schema={
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "description": "Pursuit id (e.g. 'openai-trial').",
                },
            },
            "required": ["id"],
        },
        executor=_pursuit_read,
        tags=["task", "pursuit"],
        impact_scope="agent",
        inline_only=True,
    )


def _make_pursuit_write_resolver() -> "Callable[[ToolCall], ScopeRequest]":
    """Scope resolver for pursuit_write — selector is the pursuit id.

    Granting scope for one pursuit id (e.g. "openai-trial") suppresses
    re-confirmation on subsequent writes to that same tracker, while
    leaving other pursuits gated. Invalid/missing id falls back to '*'
    so the requirement still surfaces to the user (it will fail at
    executor-level validation anyway).
    """
    def _resolve(call: ToolCall) -> ScopeRequest:
        pid = (call.args.get("id") or "").strip() or "*"
        return ScopeRequest(
            tool_name=call.tool_name,
            capabilities=call.capabilities,
            requirements=[
                ScopeRequirement(
                    resource="pursuit",
                    action="write",
                    selector=pid,
                    tool_name=call.tool_name,
                    capabilities=call.capabilities,
                ),
            ],
        )
    return _resolve


def make_pursuit_write_tool(store: "PursuitStore") -> ToolDefinition:
    async def _pursuit_write(call: ToolCall) -> ToolResult:
        pid = (call.args.get("id") or "").strip()
        content = call.args.get("content")
        if not pid:
            return ToolResult(
                call_id=call.id, tool_name=call.tool_name,
                success=False, error="'id' is required",
            )
        if not isinstance(content, str) or not content.strip():
            return ToolResult(
                call_id=call.id, tool_name=call.tool_name,
                success=False,
                error="'content' is required and must be a non-empty string",
            )
        try:
            path = store.write(pid, content)
        except ValueError as exc:
            return ToolResult(
                call_id=call.id, tool_name=call.tool_name,
                success=False, error=str(exc),
            )
        payload = {
            "id": pid,
            "path": str(path),
            "bytes": len(content.encode("utf-8")),
        }
        return ToolResult(
            call_id=call.id, tool_name=call.tool_name,
            success=True,
            output=json.dumps(payload, ensure_ascii=False),
        )

    return ToolDefinition(
        name="pursuit_write",
        description=(
            "Write/replace a pursuit's full markdown by id. Pass the FULL "
            "intended markdown — this overwrites previous content "
            "(edit-style, per #205/#206). Use for: (a) creating a new "
            "tracker from a spoken user request, (b) appending a fresh "
            "snapshot under '## 累積快照' after pulling new data, "
            "(c) editing dimensions/thresholds. Recommended shape: "
            "title + status/created/last_pulled metadata + '## 維度' + "
            "'## 累積快照' with dated subsections. To retire a pursuit, "
            "ask the user to delete the file under ~/.loom/pursuits/."
        ),
        trust_level=TrustLevel.GUARDED,
        capabilities=ToolCapability.MUTATES,
        input_schema={
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "description": (
                        "Pursuit id: lowercase letters/digits/hyphens, "
                        "1-64 chars. Becomes the filename "
                        "(~/.loom/pursuits/<id>.md)."
                    ),
                },
                "content": {
                    "type": "string",
                    "description": "Full markdown content (replaces previous).",
                },
                "justification": {
                    "type": "string",
                    "description": (
                        "簡短說明為何在目前的脈絡下寫入/覆蓋這個 pursuit 是合"
                        "理且必要的（給人類審核看）。"
                    ),
                },
            },
            "required": ["id", "content", "justification"],
        },
        executor=_pursuit_write,
        tags=["task", "pursuit"],
        impact_scope="filesystem",
        inline_only=True,
        scope_descriptions=["writes/overwrites ~/.loom/pursuits/<id>.md"],
        scope_resolver=_make_pursuit_write_resolver(),
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
    # ==============================================================
    # SECTION 8 — COGNITION TOOLS (make_request_model_tier_tool, make_clear_model_tier_tool)
    # ==============================================================
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
