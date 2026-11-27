"""
Middleware Pipeline — the spine of Loom's harness layer.

Every tool call flows through this pipeline before and after execution.
Middleware is composable: add, remove, or reorder without touching tool code.

Execution order (outermost → innermost):
    LifecycleMiddleware → TraceMiddleware → SchemaValidationMiddleware
    → BlastRadiusMiddleware → LifecycleGateMiddleware → tool handler
"""

import asyncio
import hashlib
import logging
import os
import shlex
import time
import traceback
import uuid
from pathlib import Path

_log = logging.getLogger(__name__)
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, UTC
from typing import Any, Awaitable, Callable, ClassVar

from .mode import PermissionMode
from .permissions import ToolCapability, TrustLevel

# ---------------------------------------------------------------------------
# Core data types
# ---------------------------------------------------------------------------

@dataclass
class ToolCall:
    """A single invocation of a registered tool, before execution."""
    tool_name: str
    args: dict[str, Any]
    trust_level: TrustLevel
    session_id: str
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    metadata: dict[str, Any] = field(default_factory=dict)
    capabilities: ToolCapability = field(default_factory=lambda: ToolCapability.NONE)
    abort_signal: asyncio.Event | None = field(default=None, compare=False, repr=False)
    origin: str = "interactive"
    """
    Where this call originated: "interactive" (CLI/TUI/Discord user),
    "mcp" (MCP client), "autonomy" (daemon trigger), "subagent" (child agent).
    BlastRadiusMiddleware uses this to decide whether to prompt or deny.
    """


# Structured failure categories — used for reflexive learning and failure analysis.
# Set on ToolResult.failure_type when success=False.
FAILURE_TYPES = {
    "tool_not_found",    # tool name not in registry
    "permission_denied", # trust level insufficient / user denied
    "timeout",           # execution exceeded time limit
    "execution_error",   # tool raised an exception at runtime
    "validation_error",  # bad/missing arguments
    "model_error",       # LLM API error during tool-related call
    "semantic_failure",  # tool mechanically succeeded but post_validator detected
                         # the action didn't achieve its intent (Issue #196)
}


@dataclass
class VerifierResult:
    """Outcome of a ``post_validator`` run (Issue #196).

    Returned by ``ToolDefinition.post_validator`` to signal whether a tool
    that mechanically succeeded actually achieved its intent. For backward
    compatibility, post_validator may still return plain ``bool`` — the
    harness coerces ``True`` to ``VerifierResult(passed=True)`` and
    ``False`` to ``VerifierResult(passed=False, reason="post-validation failed")``.
    """
    passed: bool
    reason: str | None = None   # populated only when passed=False
    signal: str | None = None   # optional machine-readable tag (e.g. "pytest_failed")


@dataclass
class ToolResult:
    """The outcome of a tool invocation, after execution."""
    call_id: str
    tool_name: str
    success: bool
    output: Any = None
    error: str | None = None
    failure_type: str | None = None   # one of FAILURE_TYPES when success=False
    duration_ms: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)
    # Issue #212: full diagnostic context for tool/middleware exceptions.
    # ``error`` stays a single line for the agent's prompt; ``error_context``
    # carries the full traceback (or other structured detail) for telemetry,
    # forensics, and post-mortem inspection. Always None on success.
    error_context: str | None = None


ToolHandler = Callable[[ToolCall], Awaitable[ToolResult]]


def output_text(output: Any) -> str:
    """The text form of a tool output: what the model reads.

    Tools may return structured outputs (dicts, lists, None). Anything that
    measures, digests or summarises an output goes through this one
    serialization so the ledger describes the same text the model saw.
    It matches ``str(result.output)`` in session.py and subagent.py, so
    ``None`` becomes ``"None"``.

    Exception: when JITRetrievalMiddleware spills an output over its
    threshold, the model sees a scratchpad placeholder instead, while the
    ledger END still digests and summarises the full output.
    """
    return str(output)


# Origins where no human is available to answer a confirmation prompt.
# "chime" (issue #668 S1): a scheduled phase turn runs inside a live Discord
# thread, but nobody is expected to be watching it. Prompting there held the
# session's turn lock for hours and queued every later life event behind it.
# Every human-in-the-loop wait — BlastRadius confirms, load_skill check
# approval, a requested pause — must consult this set and not wait.
UNATTENDED_ORIGINS = frozenset({"autonomy", "subagent", "chime"})


# ---------------------------------------------------------------------------
# Issue #212: middleware signal preservation helpers
# ---------------------------------------------------------------------------

_TRACE_KEY = "middleware_trace"


def _trace_middleware(
    call: ToolCall, name: str, decision: str, **extra: Any,
) -> None:
    """Append a structured decision entry to ``call.metadata['middleware_trace']``.

    Each entry is a small dict so the agent (and post-mortem tooling) can
    answer "which middleware acted on this call, in what order, with what
    outcome" without cross-referencing logs and ActionRecords. Cheap by
    design — one list append per decision point.
    """
    trace = call.metadata.setdefault(_TRACE_KEY, [])
    trace.append({"middleware": name, "decision": decision, **extra})

# ---------------------------------------------------------------------------
# Artifact extraction (#334 / doc/53 §3.1)
# ---------------------------------------------------------------------------
#
# Producer tools whose successful results materialise external artifacts
# are inspected at lifecycle END so neither the tool factory nor the
# registry needs to know about ledger emit. The same extractor is used
# by ``LoomSession._on_trace`` to feed ``_turn_artifact_max_size`` so the
# §3.3 thought-capture signal (artifact > 10 KB) actually fires.
#
# To register a new producer: add an extractor returning the four-tuple
# below (or ``None`` if the result didn't actually produce an artifact)
# and drop it into ``_ARTIFACT_EXTRACTORS``. One place, no duplication.


def _extract_write_file_artifact(call: "ToolCall", result: "ToolResult") -> dict | None:
    import hashlib
    content = call.args.get("content", "") or ""
    encoded = content.encode("utf-8")
    meta = result.metadata or {}
    return {
        "artifact_type": "text_file",
        "size_bytes": len(encoded),
        "digest": "sha256:" + hashlib.sha256(encoded).hexdigest(),
        "location": meta.get("_resolved_path") or call.args.get("path"),
    }


def _extract_image_generate_artifact(
    call: "ToolCall", result: "ToolResult"
) -> dict | None:
    import hashlib
    import json as _json
    try:
        payload = _json.loads(result.output or "{}")
    except Exception:
        payload = {}
    size_bytes = int(payload.get("bytes", 0) or 0)
    # Image bytes aren't kept on the result; digest the metadata tuple
    # so replays at least see a stable artifact identity.
    digest_input = (
        f"{payload.get('path','')}|{size_bytes}|{payload.get('model','')}"
    ).encode("utf-8")
    return {
        "artifact_type": "image",
        "size_bytes": size_bytes,
        "digest": "sha256:" + hashlib.sha256(digest_input).hexdigest(),
        "location": payload.get("path"),
    }


def _extract_tts_generate_artifact(
    call: "ToolCall", result: "ToolResult"
) -> dict | None:
    import hashlib
    import json as _json
    try:
        payload = _json.loads(result.output or "{}")
    except Exception:
        payload = {}
    size_bytes = int(payload.get("bytes", 0) or 0)
    digest_input = (
        f"{payload.get('path','')}|{size_bytes}|"
        f"{payload.get('voice_id','')}|{payload.get('format','')}"
    ).encode("utf-8")
    return {
        "artifact_type": "audio",
        "size_bytes": size_bytes,
        "digest": "sha256:" + hashlib.sha256(digest_input).hexdigest(),
        "location": payload.get("path"),
    }


def _extract_pursuit_write_artifact(
    call: "ToolCall", result: "ToolResult"
) -> dict | None:
    import hashlib
    import json as _json
    content = call.args.get("content", "") or ""
    encoded = content.encode("utf-8")
    try:
        payload = _json.loads(result.output or "{}")
    except Exception:
        payload = {}
    return {
        "artifact_type": "text_file",
        "size_bytes": len(encoded),
        "digest": "sha256:" + hashlib.sha256(encoded).hexdigest(),
        "location": payload.get("path"),
    }


_ARTIFACT_EXTRACTORS: dict[str, Callable[["ToolCall", "ToolResult"], dict | None]] = {
    "write_file": _extract_write_file_artifact,
    "image_generate": _extract_image_generate_artifact,
    "openai__text_to_image": _extract_image_generate_artifact,
    "tts_generate": _extract_tts_generate_artifact,
    "pursuit_write": _extract_pursuit_write_artifact,
}


def extract_artifact_info(call: "ToolCall", result: "ToolResult") -> dict | None:
    """Return ``{artifact_type, size_bytes, digest, location}`` for known
    producers, or ``None`` if ``call.tool_name`` doesn't produce artifacts
    or the result wasn't successful."""
    if result is None or not result.success:
        return None
    fn = _ARTIFACT_EXTRACTORS.get(call.tool_name)
    if fn is None:
        return None
    return fn(call, result)


# ---------------------------------------------------------------------------
# Middleware base
# ---------------------------------------------------------------------------

class Middleware(ABC):
    """
    Base class for all Loom middleware.

    Implement `process(call, next)` to intercept tool calls.
    Call `await next(call)` to continue down the pipeline.
    """
    @abstractmethod
    async def process(self, call: ToolCall, next: ToolHandler) -> ToolResult:
        ...

# ---------------------------------------------------------------------------
# Pipeline engine
# ---------------------------------------------------------------------------

class MiddlewarePipeline:
    """
    Builds and executes a composable middleware chain.

    Usage:
        pipeline = MiddlewarePipeline()
        pipeline.use(LogMiddleware(console))
        pipeline.use(TraceMiddleware(on_trace=memory.write_trace))
        result = await pipeline.execute(call, my_tool_handler)
    """

    def __init__(self, middlewares: list[Middleware] | None = None) -> None:
        self._middlewares: list[Middleware] = list(middlewares or [])

    def use(self, middleware: Middleware) -> "MiddlewarePipeline":
        self._middlewares.append(middleware)
        return self

    async def execute(self, call: ToolCall, handler: ToolHandler) -> ToolResult:
        def build_chain(index: int) -> ToolHandler:
            if index >= len(self._middlewares):
                return handler

            mw = self._middlewares[index]
            next_fn = build_chain(index + 1)

            async def wrapped(tc: ToolCall) -> ToolResult:
                return await mw.process(tc, next_fn)

            return wrapped

        return await build_chain(0)(call)

# ---------------------------------------------------------------------------
# Built-in middleware
# ---------------------------------------------------------------------------

class LogMiddleware(Middleware):
    """Logs every tool call and its outcome to a Rich console."""

    def __init__(self, console: Any) -> None:
        self._console = console

    async def process(self, call: ToolCall, next: ToolHandler) -> ToolResult:
        self._console.print(
            f"  [dim]~> tool[/dim] [bold]{call.tool_name}[/bold] "
            f"{call.trust_level.label}"
        )
        result = await next(call)
        status = "[green]ok[/green]" if result.success else "[red]!![/red]"
        self._console.print(
            f"  {status} [dim]{call.tool_name}[/dim] "
            f"[dim]{result.duration_ms:.0f}ms[/dim]"
        )
        if not result.success and result.error:
            self._console.print(f"  [red]  {result.error}[/red]")
        return result


class JITRetrievalMiddleware(Middleware):
    """Just-in-time spill of large tool outputs to scratchpad (Issue #197).

    When a tool produces output larger than 24,000 characters, this
    middleware writes the full output to the session scratchpad and
    replaces ``result.output`` with a structured placeholder pointing at
    the scratchpad ref. The model sees a compact reference instead of the
    raw payload, recovering token budget for synthesis-heavy turns.

    **Placement**: must wrap LifecycleMiddleware on the outside so that
    ``post_validator`` callbacks still see the full output for heuristic
    inspection (Python tracebacks, pytest summaries, etc.). JIT spills only
    affect what flows out to the message history, never what the verifier
    inspects.

    **Opt-out**: tools whose entire purpose is returning content the agent
    needs inline (``scratchpad_read``, ``list_dir``, ``task_write`` etc.)
    should set ``ToolDefinition.inline_only=True`` to bypass spill.

    **Async-mode passthrough**: when ``result.metadata["async"] is True``
    the tool already returned a job handle and the body is destined for
    scratchpad through its own job pipeline — JIT skips to avoid double
    indirection.

    **Threshold**: one fixed 24,000-character bound, without tool overrides.
    """

    THRESHOLD_CHARS = 24_000

    def __init__(
        self,
        scratchpad: Any,
        registry: Any,
    ) -> None:
        self._scratchpad = scratchpad
        self._registry = registry

    async def process(self, call: ToolCall, next: ToolHandler) -> ToolResult:
        result = await next(call)

        if self._scratchpad is None:
            return result
        if (result.metadata or {}).get("async") is True:
            return result

        tool_def = None
        if self._registry is not None:
            tool_def = self._registry.get(call.tool_name)
        if tool_def is not None and getattr(tool_def, "inline_only", False):
            return result

        output_str = output_text(result.output)
        size = len(output_str)
        if size <= self.THRESHOLD_CHARS:
            return result

        # Generate a stable, agent-readable ref. uuid suffix avoids
        # collisions across tools that produce many outputs in a turn.
        short_id = uuid.uuid4().hex[:6]
        ref = f"auto_{call.tool_name}_{short_id}"
        try:
            self._scratchpad.write(ref, output_str)
        except Exception as exc:
            _log.warning(
                "JIT spill to scratchpad failed for %r: %s — keeping inline.",
                call.tool_name,
                exc,
                exc_info=True,
            )
            _trace_middleware(
                call,
                "JITRetrieval",
                "spill-failed",
                reason=str(exc),
                error_context=traceback.format_exc(),
            )
            return result

        lines = output_str.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        # A giant single line must not turn the preview back into the payload.
        preview = "\n".join(lines[:20])
        if len(preview) > 2_000:
            preview = preview[:2_000] + "…"
        # #660: a clipped preview must not leave fetched content unclosed.
        if (
            preview.startswith("<untrusted_external_content>\n")
            and "</untrusted_external_content>" not in preview
        ):
            preview = (
                "\n".join(preview.split("\n")[:19]) + "\n</untrusted_external_content>"
            )
        placeholder = (
            f"[tool output spilled to scratchpad — {size} chars]\n"
            f"  ref: scratchpad://{ref}\n"
            f"  size: {size} chars, {len(lines)} lines\n"
            f"\nPreview (up to 20 lines, 2000 chars):\n{preview}"
        )

        new_metadata = {
            **(result.metadata or {}),
            "jit_spilled": True,
            "jit_ref": ref,
            "jit_original_size": size,
            # Lets RepeatCallTracker (#654) compare spilled outputs: the
            # placeholder's ref is unique per call, the content is not.
            "jit_digest": hashlib.sha256(
                output_str.encode("utf-8", "replace")
            ).hexdigest(),
        }
        return ToolResult(
            call_id=result.call_id,
            tool_name=result.tool_name,
            success=result.success,
            # A failure keeps its output (#690 S3c): the model reads only its
            # bounded tail, where the error is, plus the ref to the full text.
            output=placeholder if result.success else result.output,
            error=result.error,
            failure_type=result.failure_type,
            duration_ms=result.duration_ms,
            metadata=new_metadata,
            error_context=result.error_context,
        )


class TraceMiddleware(Middleware):
    """
    Measures wall-clock execution time and fires an async callback after
    each tool call completes.  The callback is what connects the harness
    to the memory layer — every tool result is automatically recorded
    without the tool author needing to think about it.
    """

    def __init__(
        self,
        on_trace: Callable[[ToolCall, ToolResult], Awaitable[None]] | None = None,
    ) -> None:
        self._on_trace = on_trace

    async def process(self, call: ToolCall, next: ToolHandler) -> ToolResult:
        t0 = time.monotonic()
        result = await next(call)
        result.duration_ms = (time.monotonic() - t0) * 1000.0

        if self._on_trace is not None:
            await self._on_trace(call, result)

        return result


WRITE_EXPECT_KEY = "write_expect"
"""What read before write established about a write's target: ``{"path":
resolved path, "sha256": digest or None (absent)}``. write_file re-checks
it where it writes (#704 S6)."""


class LegitimacyGuardMiddleware(Middleware):
    """
    Read before write (Issue #47 Phase 3, #118, #288; restated in #704 S6).

    The contract DK took from early Claude Code / opencode, for the tools in
    ``strict_guard_tools`` (write_file):

    - **Creating** a file needs no prior read: when nothing exists at the
      path at call time, there is nothing to overwrite.
    - **Overwriting** an existing file needs its current content read in
      this session, and unchanged since. A read records the file's digest;
      a write whose target's digest differs (never read, or changed by
      someone else since) is stopped with a reason that says which.
    - A successful write records the bytes it wrote, so the agent can keep
      editing its own file. It waives nothing for any other file.
    - The expectation rides on the call (``WRITE_EXPECT_KEY``) and the tool
      re-checks it where it writes, so a file that appears or changes while
      DK is being asked is not overwritten unseen.

    Paths resolve in the workspace exactly as the file tools resolve them
    (``loom.core.harness.paths``), so ``./a.md``, ``sub/../a.md`` and the
    absolute path name one file.

    **What counts as a read of a file** (#167, #283, #288): a SAFE tool
    with a ``path`` argument (read_file, probe_file, list_dir…), or a
    read-only shell command (grep/head/cat/awk/sed -n/tail/wc) whose one
    path the parser can pin down, and only when it succeeded (a read_file
    that fails on a binary file saw nothing). ``probe_file`` is a
    self-declared unlock: it reads no content, it records the agent's
    claim that it read the file some other way. A target that is not a
    regular file (directory, FIFO, device) is never overwritten. A read that names no file (a web search,
    a READ_PROBE tool, a shell pipeline) sets ``has_probed`` for the turn
    but unlocks no overwrite; before #704 S6 it unlocked every file, and
    one successful write waived the check for the rest of the session.

    ``run_bash`` and MCP tools are not guarded here: exec authorization
    belongs to ``BlastRadiusMiddleware``.
    """

    # Read-only shell commands that count as file probes (Issue #283).
    _READONLY_COMMANDS: ClassVar[frozenset[str]] = frozenset({
        "grep", "head", "cat", "awk", "sed", "tail", "wc",
    })

    # Shell metacharacters that defeat single-path bash extraction (#288).
    _BASH_AMBIGUOUS_CHARS: ClassVar[frozenset[str]] = frozenset("|<>;&")

    def __init__(self, workspace: Path | None = None) -> None:
        # Where relative paths resolve; None = the process's cwd (tests).
        self._workspace = workspace
        # Resolved path → digest of the content last read or written in
        # this session (None: seen absent).
        self._known: dict[str, str | None] = {}
        # Per-turn flag: "did the agent do any probing this turn".
        self._probed_this_turn: bool = False
        # Only file-writing tools belong here.  exec tools (run_bash) and MCP
        # generative tools are handled by BlastRadiusMiddleware.
        self.strict_guard_tools: set[str] = {
            "write_file",
        }

    @property
    def has_probed(self) -> bool:
        """Per-turn probe flag: True when any probe (file-scoped or not)
        fired this turn."""
        return self._probed_this_turn

    def _resolve(self, raw: str) -> Path | None:
        # The path exactly as the file tools resolve it: no strip, so
        # " a.md" is the file the tool would write (#711 review).
        from .paths import resolve_workspace_path
        if not isinstance(raw, str) or not raw.strip():
            return None
        return resolve_workspace_path(raw, self._workspace or Path.cwd())

    @classmethod
    def _extract_bash_path(cls, command: str) -> str | None:
        """Best-effort: pull a single file path from a known read-only bash
        invocation. Returns None when the parser can't pin one down — that
        case is an unscoped read (it unlocks no overwrite) and the agent must use
        ``probe_file()`` if they need a per-file mark.

        Bails on shell metacharacters (pipe/redirect/chain) and on glob
        patterns. The point isn't to be a shell — it's to catch the
        common forms (``grep PAT file``, ``head -n 100 file``,
        ``sed -n '1,20p' file``) cleanly.
        """
        import shlex
        if any(c in command for c in cls._BASH_AMBIGUOUS_CHARS):
            return None
        try:
            parts = shlex.split(command)
        except ValueError:
            return None
        if len(parts) < 2:
            return None
        # Drop flags. The remaining last token is the path candidate.
        # `sed -n '1,20p' file.py` survives because the script is single-quoted
        # and shlex strips the quotes — both '1,20p' and 'file.py' look like
        # candidates, last wins (sed's file argument is always last).
        candidates = [p for p in parts[1:] if not p.startswith("-")]
        if not candidates:
            return None
        last = candidates[-1]
        if any(c in last for c in "*?["):
            return None
        return last

    def _probe_signal(self, call: ToolCall) -> tuple[set[str], bool]:
        """Compute (file_paths, unscoped) for this call.

        - file_paths: raw paths of the specific files this call reads
        - unscoped:   True when the call reads *something* but the file
                      identity is unknown (READ_PROBE-flagged tools,
                      unparseable bash, etc.)

        Returning ``(set(), False)`` means "not a probe".
        """
        # Read-only bash with extractable path → per-file; otherwise unscoped.
        if call.tool_name == "run_bash" and self._is_readonly_bash(call):
            cmd = (call.args.get("command", "") or "")
            path = self._extract_bash_path(cmd)
            if path:
                return ({path}, False)
            return (set(), True)

        # READ_PROBE capability flag: tool reads but identity not file-scoped
        # (web_search, MCP read tools).
        if call.capabilities & ToolCapability.READ_PROBE:
            return (set(), True)

        # SAFE trust + explicit ``path`` arg: per-file probe.
        # Covers read_file, list_dir, probe_file. SAFE tools without a
        # ``path`` arg (agent_health, time_now) intentionally do NOT
        # count as a probe — that was the #288 sledgehammer hole.
        if call.trust_level == TrustLevel.SAFE:
            path = call.args.get("path", "")
            if isinstance(path, str) and path.strip():
                return ({path}, False)

        return (set(), False)

    @staticmethod
    def _is_readonly_bash(call: ToolCall) -> bool:
        command = (call.args.get('command', '') or '').strip()
        if not command:
            return False
        first_word = command.split()[0]
        if first_word not in LegitimacyGuardMiddleware._READONLY_COMMANDS:
            return False
        if first_word == 'sed' and '-n' not in command:
            return False
        return True

    def reset_probe(self) -> None:
        """Clear the per-turn flag. What was read stays known while the
        file is unchanged."""
        self._probed_this_turn = False

    def _stop(self, call: ToolCall, reason: str, guidance: str) -> ToolResult:
        from .guard_reason import guard_error
        from .lifecycle import LIFECYCLE_CTX_KEY
        ctx = call.metadata.get(LIFECYCLE_CTX_KEY)
        if ctx is not None:
            ctx.authorization_result = False
            ctx.authorization_reason = f"read before write: {reason}"
        return ToolResult(
            call_id=call.id, tool_name=call.tool_name, success=False,
            error=guard_error("read_before_write", reason, guidance),
            failure_type="permission_denied",
        )

    async def process(self, call: ToolCall, next: ToolHandler) -> ToolResult:
        from .paths import file_digest

        paths, unscoped = self._probe_signal(call)
        if paths or unscoped:
            self._probed_this_turn = True

        if call.tool_name not in self.strict_guard_tools:
            result = await next(call)
            # Only a read that succeeded saw the content (#711 review: a
            # read_file that fails on a binary file, a denied or skipped
            # read, unlocks nothing). The content it saw: the file as it
            # is after the read.
            if not result.success:
                return result
            for raw in paths:
                target = self._resolve(raw)
                if target is not None:
                    self._known[str(target)] = file_digest(target)
            return result

        raw_target = call.args.get("path", "")
        target = self._resolve(raw_target if isinstance(raw_target, str) else "")
        if target is None:
            return await next(call)  # no path: the schema / the tool says so
        from .paths import NOT_REGULAR
        current = file_digest(target)
        if current is not None and current.startswith(NOT_REGULAR):
            return self._stop(call, f"{raw_target} is not a regular file",
                              "write_file writes regular files only.")
        if current is not None:
            key = str(target)
            if key not in self._known or self._known[key] is None:
                return self._stop(
                    call, f"{raw_target} exists and has not been read in this session",
                    "Read it first (read_file, or grep/head/sed -n on this path, or "
                    "probe_file after reading it through a pipeline), then write. "
                    "Creating a file that does not exist needs no read.")
            if self._known[key] != current:
                return self._stop(
                    call, f"{raw_target} changed since it was read",
                    "Read it again, then write, so the write does not discard "
                    "the change.")
        call.metadata[WRITE_EXPECT_KEY] = {"path": str(target), "sha256": current}

        result = await next(call)
        if result.success:
            self._known[str(target)] = file_digest(target)
        return result


class BlastRadiusMiddleware(Middleware):
    """
    Guards GUARDED and CRITICAL tools by consulting a PermissionContext.

    Issue #45 Phase B: when a tool has a ``scope_resolver``, this middleware
    resolves the scope, writes it to ``call.metadata``, and uses
    ``PermissionContext.evaluate()`` for resource-level authorization.
    Tools without a resolver fall back to the legacy tool-name path.

    `confirm_fn` is injected by the platform layer so the middleware
    stays platform-agnostic (CLI prompt vs. webhook vs. Telegram).

    `registry` is an optional ToolRegistry used to look up ``scope_resolver``
    per tool.  When absent, all tools take the legacy path.
    """

    def __init__(
        self,
        perm_ctx: Any,
        confirm_fn: "Callable[[ToolCall], Awaitable[bool | ConfirmDecision]]",
        registry: Any | None = None,
        ledger_emitter: Any = None,
        classifier: Any = None,
        workspace: Path | None = None,
    ) -> None:
        self._perm = perm_ctx
        # #700: where skills/<name>/ check modules live. None = a write
        # never offers trust (sub-agents, tests of the bare middleware).
        self._workspace = workspace
        # #690 S6: consulted where a call would otherwise go to a human or
        # be skipped (see ``loom.core.harness.approval``). None = no
        # classifier (sub-agents, tests of the bare middleware).
        self._classifier = classifier
        self._confirm = confirm_fn
        self._registry = registry
        # PR-C: optional platform-side hook fired on every authorisation
        # decision. Lets the CLI route green-light events to the footer
        # (no留底) and red-light events to the inline harness stream.
        # Signature: (call, result, reason) -> None. Kept Optional so the
        # core stays platform-agnostic — main.py wires it in _chat().
        self._on_lifecycle_event: (
            "Callable[[ToolCall, bool, str], None] | None"
        ) = None
        # Optional ledger emitter — every _notify_lifecycle call also
        # emits one permission_decision event when wired (doc/53 §11.1).
        self._ledger_emitter = ledger_emitter

    # Origins where no human is available to answer confirmation prompts.
    _UNATTENDED_ORIGINS = UNATTENDED_ORIGINS

    def _declared(self, call: ToolCall) -> str | None:
        """The boundary that declares this call's tool (#704 S3), or None.
        A declaration never covers EXEC (#690 S7: shell is authorized only
        by exec scope grants) and never CRITICAL."""
        if call.capabilities & ToolCapability.EXEC or call.trust_level == TrustLevel.CRITICAL:
            return None
        declared_by = getattr(self._perm, "declared_by", None)
        if declared_by is None:
            return None
        return declared_by(call.tool_name, attended=not self._is_unattended(call))

    def _is_unattended(self, call: ToolCall) -> bool:
        """Return True if no human is available to answer prompts."""
        return call.origin in self._UNATTENDED_ORIGINS

    def _deny_unattended(
        self, call: ToolCall, verdict: str, *, classifier_note: str | None = None,
    ) -> ToolResult:
        """Return a denial ToolResult for unattended calls that need confirmation."""
        _log.info(
            "Denying %s (origin=%s, verdict=%s) — no human to confirm",
            call.tool_name, call.origin, verdict,
        )
        # A skip is not a deny (#690 S7 review): its own decision value keeps
        # it out of deny rates, and its reason says skip (#690 S3c). Rows
        # before S3c read "unattended-deny (...)" for the same thing.
        detail = f"; classifier {classifier_note}" if classifier_note else ""
        _, code = self._note_source(classifier_note)
        self._notify_lifecycle(
            call, False, f"unattended-skip ({call.origin}, {verdict}{detail})", decision="skip",
            source="unattended", code=code if classifier_note else verdict,
        )
        call.metadata["user_decision"] = False

        # Issue #668: say what actually happened. Nobody denied this —
        # nobody was there to ask. The call was skipped, not refused, and
        # routing around it (another tool, a rewritten command) would
        # launder the very authorization the skip is holding back.
        from .guard_reason import GUARD_REASON_KEY, GuardReason, ask_reason
        reason = call.metadata.get(GUARD_REASON_KEY)
        if not isinstance(reason, GuardReason):
            reason = ask_reason(classifier_note, call.metadata.get("scope_verdict"),
                                call.metadata.get("scope_diff"))
            call.metadata[GUARD_REASON_KEY] = reason
        error = (
            f"{reason.line()}\n"
            f"Skipped (not executed): {call.tool_name} needs authorization "
            f"beyond this '{call.origin}' turn's grants, and no human is "
            f"attending to approve it. This is not a denial. Do not retry it "
            f"or reach the same effect through another tool or command. "
            f"Note what was skipped, continue with the parts that don't "
            f"depend on it, and don't treat its result as done."
        )
        if classifier_note:
            error += f" (The approval classifier did not approve it: {classifier_note}.)"
        return ToolResult(
            call_id=call.id, tool_name=call.tool_name,
            success=False,
            error=error,
            failure_type="permission_denied",
            metadata={"unattended_skip": True, "origin": call.origin,
                      "skip_note": classifier_note or ""},
        )

    def _mode(self, call: ToolCall) -> PermissionMode:
        """The session's permission mode, read once per call (#704): a
        switch applies from the next dispatch, never halfway through one.
        A context without a mode (a test double) is treated as 安全."""
        mode = getattr(self._perm, "mode", None)
        if not isinstance(mode, PermissionMode):
            mode = PermissionMode.SAFE
        call.metadata.setdefault("permission_mode", mode.value)
        return PermissionMode(call.metadata["permission_mode"])

    @staticmethod
    def _note_source(note: str | None) -> tuple[str, str]:
        """(decision_source, reason_code) for a classifier or guardrail note."""
        if not note:
            return "policy", "no_grant"
        kind, _, reason = note.partition(": ")
        if kind in ("rule-allow", "rule-refuse"):
            return "rule", reason
        if kind in ("model-accept", "model-abstain"):
            return "luna", reason or kind.removeprefix("model-")
        if kind == "hard":
            return "hard_guardrail", reason
        return "classifier", note

    def _notify_lifecycle(
        self, call: ToolCall, result: bool, reason: str, *, decision: str | None = None,
        source: str = "policy", code: str = "",
    ) -> None:
        """
        Write authorization decision to LifecycleContext (Issue #50).

        If the call carries a LifecycleContext (injected by LifecycleMiddleware),
        record the real-time auth result so the lifecycle state machine can
        transition DECLARED → AUTHORIZED (or → DENIED) at the moment the
        decision is actually made — not retroactively.

        When no LifecycleContext is present (e.g. sub-agent pipeline),
        this is a no-op — full backward compatibility.
        """
        from .lifecycle import LIFECYCLE_CTX_KEY
        ctx = call.metadata.get(LIFECYCLE_CTX_KEY)
        if ctx is not None:
            ctx.authorization_result = result
            ctx.authorization_reason = reason

        # Ledger permission_decision emit (doc/53 §11.1). Sync wrapper
        # because _notify_lifecycle is called from sync contexts; we
        # schedule the async emit on the running loop. Failure is
        # swallowed — auth flow must never break on ledger.
        if self._ledger_emitter is not None:
            self._schedule_permission_decision_emit(
                call, result, reason, decision, source=source, code=code)
        # Issue #212: surface authorization decisions in middleware_trace
        # so an agent inspecting a denied result can see "BlastRadius said
        # X because Y" without cross-referencing the lifecycle ActionRecord.
        _trace_middleware(
            call, "BlastRadius",
            "authorize" if result else "deny",
            reason=reason,
        )

        # PR-C: optional platform hook for surfacing the decision in the
        # CLI/Discord stream. Wrapped in a broad except so a buggy
        # listener can never abort the harness pipeline.
        if self._on_lifecycle_event is not None:
            try:
                self._on_lifecycle_event(call, result, reason)
            except Exception:
                pass

    def _schedule_permission_decision_emit(
        self, call: ToolCall, result: bool, reason: str, decision: str | None = None,
        *, source: str = "policy", code: str = "",
    ) -> None:
        """Fire-and-forget permission_decision emit from a sync caller.

        ``_notify_lifecycle`` is sync but ledger emit is async; we
        schedule the coroutine on the running loop so the auth path is
        not blocked. Errors are logged and swallowed — auth must never
        be coupled to ledger health.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            _log.debug("no running loop — skipping ledger permission_decision emit")
            return

        mode = self._mode(call).value

        async def _emit() -> None:
            from loom.core.ledger import PermissionDecisionPayload

            nonlocal decision
            if decision is None:
                decision = "grant" if result else "deny"
            scope_grant = call.metadata.get("scope_request") or call.metadata.get(
                "scope_verdict"
            )
            try:
                await self._ledger_emitter.emit_permission_decision(
                    payload=PermissionDecisionPayload(
                        decision=decision,
                        tool_call_id=call.id,
                        trust_level=str(call.trust_level.value)
                        if hasattr(call.trust_level, "value")
                        else str(call.trust_level),
                        scope_grant=scope_grant if isinstance(scope_grant, dict) else None,
                        reason=reason,
                        origin=call.origin,
                        mode=mode,
                        decision_source=source,
                        reason_code=code,
                    ),
                )
            except Exception:
                _log.exception(
                    "ledger permission_decision emit failed; continuing"
                )

        loop.create_task(_emit())

    async def _enter_awaiting_confirm(self, call: ToolCall) -> None:
        """Transition ActionRecord to AWAITING_CONFIRM before prompting user (#109)."""
        from .lifecycle import LIFECYCLE_CTX_KEY, ActionState
        ctx = call.metadata.get(LIFECYCLE_CTX_KEY)
        if ctx is not None and ctx.record.state == ActionState.DECLARED:
            await ctx.transition(ActionState.AWAITING_CONFIRM, reason="awaiting user confirmation")

    def _write_scope_metadata(
        self, call: ToolCall, scope_request: Any,
        diff: Any | None, verdict: Any,
    ) -> None:
        """Write scope resolution results to call.metadata for audit."""
        call.metadata["scope_request"] = scope_request
        call.metadata["scope_diff"] = diff
        call.metadata["scope_verdict"] = verdict

    @staticmethod
    def _normalize_decision(raw: "bool | ConfirmDecision") -> "ConfirmDecision":
        """Convert legacy bool confirm result to ConfirmDecision."""
        from .scope import ConfirmDecision
        if isinstance(raw, ConfirmDecision):
            return raw
        return ConfirmDecision.ONCE if raw else ConfirmDecision.DENY

    # Sentinel to signal "resolver failed, use legacy path"
    _FALLBACK_TO_LEGACY = object()

    async def _scope_aware_process(
        self, call: ToolCall, next: ToolHandler, scope_resolver: Any,
    ) -> ToolResult | None | object:
        """
        Scope-aware authorization path (Issue #45 Phase B).

        Returns:
            ToolResult  — call was denied, return this result directly
            None        — authorization passed, caller should ``await next(call)``
            _FALLBACK_TO_LEGACY — resolver failed, caller should use legacy path
        """
        from .scope import PermissionVerdict as PV

        try:
            scope_request = scope_resolver(call)
        except Exception as exc:
            _log.warning(
                "scope_resolver for %r raised: %s — falling back to legacy",
                call.tool_name, exc, exc_info=True,
            )
            _trace_middleware(
                call, "BlastRadius", "scope-resolver-raised",
                reason=str(exc), error_context=traceback.format_exc(),
            )
            return self._FALLBACK_TO_LEGACY

        attended = not self._is_unattended(call)
        diff = self._perm.diff(scope_request, attended=attended)
        verdict = self._perm.evaluate(scope_request, call.trust_level, attended=attended)
        self._write_scope_metadata(call, scope_request, diff, verdict)

        hard: str | None = None
        if verdict == PV.ALLOW:
            # Only routine / system grants exist since #704 S4: a human's
            # "allow once" leaves none behind. A grant is a rule-allow too,
            # so it meets the hard guardrails like every other (#704 S3,
            # #714 B4): a routine's write grant over a directory never
            # reaches a SKILL.md, a policy file or a credential in it.
            hard = (self._hard_boundary(call, scope_request, verdict, declared=True)
                    if self._workspace is not None else None)
            if hard is None:
                self._notify_lifecycle(call, True, f"scope-allow: {diff.reason.value}",
                                       source="grant", code=diff.reason.value)
                return None  # proceed

        if verdict == PV.DENY:
            self._notify_lifecycle(call, False, "scope-deny", source="policy", code="scope_deny")
            call.metadata["user_decision"] = False
            from .guard_reason import guard_error
            return ToolResult(
                call_id=call.id, tool_name=call.tool_name,
                success=False,
                error=guard_error("scope", "the scope policy refuses this call",
                                  "Permission denied by scope policy."),
                failure_type="permission_denied",
            )

        # CONFIRM or EXPAND_SCOPE: the permission-mode decision point, then
        # a human (attended) or a skip (unattended). There is no name
        # bridge (#704 S3): a declared tool is decided in ``_auto_decide``.
        # #704 YOLO: within the hard guardrails, run without Luna and
        # without asking. #690 S6 (安全, 自理): before asking a human or
        # skipping, the approval classifier may approve this one call. It
        # grants nothing, so a refusal or an abstain leaves exactly the
        # path below.
        if hard is not None:
            note: str | None = f"hard: {hard}"
        else:
            note = await self._auto_decide(call, scope_request, verdict)
            if note is None:
                return None  # proceed

        # #704 S6: one reason line for the confirm or the skip.
        from .guard_reason import GUARD_REASON_KEY, ask_reason
        call.metadata[GUARD_REASON_KEY] = ask_reason(note, verdict, diff)
        if self._is_unattended(call):
            return self._deny_unattended(call, verdict.value, classifier_note=note)

        # #690 S6c: the confirm prompt says why the classifier did not
        # approve, so DK sees it ran.
        if note:
            call.metadata["classifier_note"] = note
        offer = self._skill_trust_offer(call)
        denied = await self._ask_human(call, f" ({verdict.value})")
        if denied is not None:
            return denied
        # #700: trust needs DK to have seen the diff and allowed this one call.
        if offer is not None and call.metadata.get("skill_trust_diff_shown") is True:
            call.metadata["approved_with_diff"] = True
        return None  # proceed

    async def _ask_human(self, call: ToolCall, label: str) -> ToolResult | None:
        """Ask DK about this one call (#704 S4): "allow this once" or deny.
        Allowing leaves no grant, lease or remembered name behind, so the
        next call is decided again. A deny is counted in
        ``perm.recent_denies``; three in a row end the turn as a run stop
        condition in the session loop, not here. Returns the denial result,
        or None when allowed."""
        from .scope import ConfirmDecision
        await self._enter_awaiting_confirm(call)
        decision = self._normalize_decision(await self._confirm(call))
        if decision != ConfirmDecision.ONCE:
            self._perm.recent_denies += 1
            self._notify_lifecycle(call, False, f"user denied{label}", source="operator", code="deny")
            call.metadata["user_decision"] = False
            return ToolResult(
                call_id=call.id, tool_name=call.tool_name,
                success=False,
                error=(
                    "USER ACTION: DENIED. This tool call was explicitly rejected by the "
                    "human user. Do not retry this action or equivalent substitutions "
                    "in this turn. Acknowledge and proceed with other tasks or wait for input."
                ),
                failure_type="permission_denied",
                metadata={"operator_denied": True},
            )
        self._perm.recent_denies = 0
        self._notify_lifecycle(call, True, f"user confirmed{label[:-1] + ', once)' if label else ' (once)'}",
                               source="operator", code="once")
        call.metadata["user_decision"] = True
        call.metadata["confirm_decision"] = "once"
        call.metadata["once_authorized"] = True
        return None

    async def _auto_decide(self, call: ToolCall, scope_request: Any, verdict: Any) -> str | None:
        """Approve *call* without a human, per the permission mode (#704).
        Returns None when approved (already recorded), else the note that
        says why not (a classifier reason, ``hard: …``, or "" when nothing
        looked at the call)."""
        mode = self._mode(call)
        # #704 S3: a tool its boundary declares (a routine's allowed_tools,
        # the Discord platform's tools) runs in every mode, unless the call
        # crosses a declared scope (EXPAND_SCOPE: the program's ceiling) or
        # a hard guardrail. This replaces the name bridge.
        from .scope import PermissionVerdict as PV
        source = self._declared(call)
        if source is not None and verdict != PV.EXPAND_SCOPE:
            if hard := self._hard_boundary(call, scope_request, verdict, declared=True):
                return f"hard: {hard}"
            self._notify_lifecycle(call, True, f"declared by {source}", source="program", code=source)
            return None
        if mode is PermissionMode.YOLO:
            if hard := self._hard_boundary(call, scope_request, verdict):
                return f"hard: {hard}"
            self._notify_lifecycle(call, True, "mode yolo", source="mode", code="yolo")
            return None
        if self._classifier is None:
            return ""
        try:
            approval = await self._classifier.decide(call, scope_request, verdict, mode)
        except Exception:
            _log.exception("approval classifier failed; not approving")
            return "error"
        if not approval.approved:
            return approval.reason
        # The hard guardrails hold in every mode: what Luna would pass (e.g.
        # a document whose content is a token, which Luna only sees
        # redacted) still goes to DK. A rule allow already cleared stricter
        # checks.
        # ``declared``: an unknown tool Luna judged against the intent
        # (#704 S3) is not re-refused for having no resolver.
        if (approval.reason.startswith("model-accept") and self._workspace is not None
                and (hard := self._hard_boundary(call, scope_request, verdict, declared=True))):
            return f"hard: {hard}"
        if argv := getattr(approval, "argv", None):
            # A delegated post runs the argv Luna reviewed, without a shell
            # (#705 review, #704 S2). Only this metadata, never the model's
            # args, selects run_bash's no-shell path.
            call.args["command"] = shlex.join(argv)
            call.args.pop("async_mode", None)
            call.metadata["delegated_argv"] = list(argv)
            call.metadata["approved_args_rewritten"] = ["command"]
        source, code = self._note_source(approval.reason)
        self._notify_lifecycle(call, True, f"classifier {approval.reason}", source=source, code=code)
        return None

    def _hard_boundary(self, call: ToolCall, scope_request: Any, verdict: Any, *,
                       declared: bool = False) -> str | None:
        """The hard guardrail *call* crosses (``approval.hard_boundary``),
        failing closed when it cannot be checked."""
        if self._workspace is None:
            return "no_workspace"
        from .approval import hard_boundary
        try:
            return hard_boundary(call, scope_request, verdict, self._perm, self._workspace,
                                 declared=declared)
        except Exception:
            _log.exception("hard guardrail check failed; not approving")
            return "check_failed"

    def _skill_trust_offer(self, call: ToolCall) -> Any:
        """#700: before DK confirms a write that replaces a trusted skill
        check module, put the diff against the trusted version where the
        platform confirm shows it. The platform sets
        ``skill_trust_diff_shown`` once DK can see it."""
        content = call.args.get("content")
        raw = call.args.get("path")
        if (self._workspace is None or call.tool_name != "write_file"
                or not isinstance(content, str) or not isinstance(raw, str)):
            return None
        from .skill_checks import trust_offer_for_write
        try:
            offer = trust_offer_for_write(self._workspace, raw, content)
        except Exception:
            _log.exception("skill trust offer failed; asking without one")
            return None
        if offer is not None:
            call.metadata["skill_trust_offer"] = offer
            call.metadata["skill_trust_diff"] = offer.diff
        return offer

    async def _retrust_approved_skill_write(self, call: ToolCall, result: ToolResult) -> None:
        """#700: DK allowed this write after seeing its diff against the
        trusted version, so the written bytes become the trusted ones.
        Only ``approved_with_diff`` counts: a model accept, a grant, a
        lease or auto, a bridge or an unattended turn never sets it."""
        offer = call.metadata.get("skill_trust_offer")
        if (call.metadata.get("approved_with_diff") is not True or offer is None
                or not result.success or self._is_unattended(call)
                or result.metadata.get("_resolved_path") != str(offer.module)):
            return
        from .skill_checks import retrust_approved_write
        try:
            changed = retrust_approved_write(offer)
        except Exception:
            _log.exception("skill trust update after approved write failed")
            return
        if changed is None:
            return
        payload = {
            "skill": offer.module.parent.name, "file": str(offer.module),
            "old_hash": changed[0][:12], "new_hash": changed[1][:12], "call_id": call.id,
        }
        if self._ledger_emitter is not None:
            try:
                await self._ledger_emitter.emit("skill_trust_update", payload)
                return
            except Exception:
                _log.exception("ledger skill_trust_update emit failed")
        _log.warning("skill trust updated after DK-approved write: %s", payload)

    async def process(self, call: ToolCall, next: ToolHandler) -> ToolResult:
        # --- Scope-aware path (Issue #45 Phase B) ---
        # If registry is available and tool has a scope_resolver, use it.
        scope_resolver = None
        if self._registry is not None:
            tool_def = self._registry.get(call.tool_name)
            if tool_def is not None:
                scope_resolver = getattr(tool_def, "scope_resolver", None)

        if scope_resolver is not None:
            result = await self._scope_aware_process(call, next, scope_resolver)
            if result is self._FALLBACK_TO_LEGACY:
                pass  # fall through to legacy path below
            elif result is not None:
                return result  # short-circuited (denied)
            else:
                result = await next(call)  # scope-aware ALLOW
                await self._retrust_approved_skill_write(call, result)
                return result

        # --- Tools without a scope_resolver ---
        # A SAFE tool is read-only and local by its own declaration. Every
        # other call goes to the decision point: a declaration, YOLO, or
        # (#690 S6b) the classifier for MCP public search.
        if call.trust_level == TrustLevel.SAFE:
            self._notify_lifecycle(call, True, "safe tool", source="trust", code="safe")
            return await next(call)
        from .scope import PermissionVerdict as PV
        note = await self._auto_decide(call, None, PV.CONFIRM)
        if note is None:
            return await next(call)
        from .guard_reason import GUARD_REASON_KEY, ask_reason
        call.metadata[GUARD_REASON_KEY] = ask_reason(note, None, None)

        # Unattended origins cannot request new permissions
        if self._is_unattended(call):
            return self._deny_unattended(call, "legacy-not-authorized", classifier_note=note)

        if note:
            call.metadata["classifier_note"] = note
        denied = await self._ask_human(call, "")
        if denied is not None:
            return denied
        # A human's answer covers this call only (#704 S3): the next call
        # of the same tool is decided again.
        return await next(call)


# ---------------------------------------------------------------------------
# Action Lifecycle Middleware — two-layer architecture (Issue #42 + #50)
#
# The lifecycle is split into TWO cooperating middleware layers:
#
#   Pipeline order:
#     LifecycleMiddleware (outer)          ← DECLARED, post-OBSERVED states
#       → TraceMiddleware
#       → SchemaValidationMiddleware
#       → BlastRadiusMiddleware
#       → LifecycleGateMiddleware (inner)  ← AUTHORIZED → PREPARED → EXECUTING → OBSERVED
#         → handler (tool executor)
#
#   Why two layers?
#     A single middleware cannot intercept both BEFORE and AT the moment
#     the tool executor runs.  ``next(call)`` fires the entire remaining
#     chain as one opaque call — the outermost middleware has no way to
#     inject logic immediately before the handler.
#
#     The outer layer creates the ActionRecord and handles states that
#     occur after execution (validation, rollback, memorialized).
#     The inner layer sits right before the handler and fires EXECUTING
#     at the exact dispatch moment, with abort-signal racing.
#
#   They share state through LifecycleContext in call.metadata.
# ---------------------------------------------------------------------------


class LifecycleGateMiddleware(Middleware):
    """
    Inner lifecycle middleware — real-time control gates.

    Sits at the innermost position in the pipeline, just before the tool
    executor (handler).  When ``process(call, next)`` is called, ``next``
    is the actual tool executor function.

    Responsibilities:
        AUTHORIZED  — read from LifecycleContext (set by BlastRadiusMiddleware)
        PREPARED    — evaluate ToolDefinition.precondition_checks[]
        EXECUTING   — fire at the exact moment ``next(call)`` is invoked
                      race abort_signal for real-time abort
        OBSERVED    — fire when the executor returns

    When no LifecycleContext is present (e.g. sub-agent pipeline without
    LifecycleMiddleware), this middleware is a transparent pass-through.
    """

    def __init__(self, registry: Any, skill_check_manager: Any = None) -> None:
        self._registry = registry
        # Optional: used to attribute precondition failures back to the
        # skill that mounted the check, so the error message can tell the
        # agent how to self-recover (unload_skill / load different skill).
        self._skill_check_manager = skill_check_manager

    async def process(self, call: ToolCall, next: ToolHandler) -> ToolResult:
        from .lifecycle import LIFECYCLE_CTX_KEY, ActionState

        ctx = call.metadata.get(LIFECYCLE_CTX_KEY)
        if ctx is None:
            # No lifecycle context → pass through (sub-agent, plugin, etc.)
            return await next(call)

        record = ctx.record
        tool_def = self._registry.get(call.tool_name)

        # ── AUTHORIZED ────────────────────────────────────────────────
        # BlastRadiusMiddleware already wrote authorization_result to ctx
        # at the moment the decision was made.  We now fire the state
        # transition.  If it didn't write (SAFE tool, auto-authorized),
        # treat as authorized.
        auth_reason = ctx.authorization_reason or "passed blast radius"
        await ctx.transition(ActionState.AUTHORIZED, reason=auth_reason)

        # ── PREPARED — callable precondition gates ────────────────────
        precondition_checks = (
            getattr(tool_def, "precondition_checks", []) if tool_def else []
        )
        precondition_descs = (
            getattr(tool_def, "preconditions", []) if tool_def else []
        )

        for i, check in enumerate(precondition_checks):
            try:
                passed = await check(call)
            except Exception as exc:
                _log.warning(
                    "precondition_check[%d] for %r raised: %s — treating as failed",
                    i, call.tool_name, exc, exc_info=True,
                )
                _trace_middleware(
                    call, "LifecycleGate", "precondition-raised",
                    check_index=i, reason=str(exc),
                    error_context=traceback.format_exc(),
                )
                passed = False

            if not passed:
                desc = (
                    precondition_descs[i]
                    if i < len(precondition_descs)
                    else f"precondition_check[{i}]"
                )

                # Attribute the failing check back to the skill that mounted
                # it, so the agent can self-recover without bubbling to user.
                owner_skill = None
                if self._skill_check_manager is not None:
                    try:
                        owner_skill = self._skill_check_manager.owner_of(check)
                    except Exception:
                        owner_skill = None

                error_lines = [f"Precondition failed: {desc}"]
                if owner_skill is not None:
                    error_lines.append(
                        f"This check was mounted by skill '{owner_skill}'."
                    )
                    error_lines.append(
                        "Recovery: if this skill's guard is not relevant to "
                        "your current task, call "
                        f"unload_skill(name=\"{owner_skill}\") to lift it; "
                        "otherwise adjust the command to satisfy the check. "
                        "This is recoverable — do not ask the user unless "
                        "the intent itself is ambiguous."
                    )
                error_text = "\n".join(error_lines)

                _trace_middleware(
                    call, "LifecycleGate", "precondition-failed",
                    check_index=i, reason=desc, owner_skill=owner_skill,
                )
                await ctx.transition(
                    ActionState.ABORTED,
                    reason=f"Precondition failed: {desc}",
                )
                result = ToolResult(
                    call_id=call.id,
                    tool_name=call.tool_name,
                    success=False,
                    error=error_text,
                    failure_type="execution_error",
                    metadata={"failure_stage": "guard"},
                )
                record.result = result
                await ctx.memorialize("precondition_failed")
                return result

        await ctx.transition(ActionState.PREPARED)

        # ── EXECUTING — fire at the exact dispatch moment ─────────────
        await ctx.transition(ActionState.EXECUTING)

        # Race executor against abort signal for real-time cancellation
        if call.abort_signal is not None and call.abort_signal.is_set():
            # Already aborted before we started
            await ctx.transition(
                ActionState.ABORTED, reason="abort signal (pre-execution)",
            )
            result = ToolResult(
                call_id=call.id, tool_name=call.tool_name,
                success=False, error="Aborted before execution.",
                failure_type="execution_error",
                # Never ran, so re-issuing repeats nothing (#690 S3c review).
                metadata={"failure_stage": "guard"},
            )
            record.result = result
            await ctx.memorialize("aborted")
            return result

        if call.abort_signal is not None:
            exec_task = asyncio.create_task(next(call))
            abort_task = asyncio.create_task(call.abort_signal.wait())
            done, pending = await asyncio.wait(
                {exec_task, abort_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for p in pending:
                p.cancel()
                try:
                    await p
                except (asyncio.CancelledError, Exception):
                    pass

            if exec_task in done:
                exc = exec_task.exception()
                if exc is not None:
                    # Tool raised instead of returning ToolResult — convert and
                    # continue through OBSERVED so MEMORIALIZED always fires.
                    tb = "".join(traceback.format_exception(
                        type(exc), exc, exc.__traceback__,
                    ))
                    _log.warning(
                        "tool %r raised during abort-raced execution: %s",
                        call.tool_name, exc, exc_info=exc,
                    )
                    _trace_middleware(
                        call, "LifecycleGate", "tool-raised",
                        reason=str(exc), error_context=tb,
                    )
                    result = ToolResult(
                        call_id=call.id, tool_name=call.tool_name,
                        success=False, error=str(exc),
                        failure_type="execution_error",
                        error_context=tb,
                    )
                else:
                    result = exec_task.result()
            else:
                # Abort signal fired during execution
                _trace_middleware(
                    call, "LifecycleGate", "aborted-during-execution",
                    reason="abort signal during execution",
                )
                await ctx.transition(
                    ActionState.ABORTED,
                    reason="abort signal during execution",
                )
                result = ToolResult(
                    call_id=call.id, tool_name=call.tool_name,
                    success=False, error="Execution aborted by signal.",
                    failure_type="execution_error",
                    metadata={"failure_stage": "cancelled"},
                )
                record.result = result
                await ctx.memorialize("aborted")
                return result
        else:
            try:
                result = await next(call)
            except Exception as exc:
                # Tool raised instead of returning ToolResult — convert and
                # continue through OBSERVED so MEMORIALIZED always fires.
                tb = traceback.format_exc()
                _log.warning(
                    "tool %r raised during execution: %s", call.tool_name, exc,
                    exc_info=True,
                )
                _trace_middleware(
                    call, "LifecycleGate", "tool-raised",
                    reason=str(exc), error_context=tb,
                )
                result = ToolResult(
                    call_id=call.id, tool_name=call.tool_name,
                    success=False, error=str(exc),
                    failure_type="execution_error",
                    error_context=tb,
                )

        # ── OBSERVED — executor returned ──────────────────────────────
        # If the handler returned a timeout result, do NOT transition to
        # OBSERVED — leave at EXECUTING so the outer LifecycleMiddleware
        # can correctly drive EXECUTING → TIMED_OUT.
        if result.failure_type == "timeout":
            record.result = result
            return result

        await ctx.transition(ActionState.OBSERVED)
        record.result = result
        return result


_SKILL_PAYLOAD_TOOL_NAMES = frozenset({"load_skill", "unload_skill"})


def _memorialize_reason(rec: "ActionRecord") -> str:
    """The reason memorialize() was given: the END's outcome (#690 S3b).

    "unknown" when the last transition is not MEMORIALIZED or carries no
    reason, so a missing value is explicit rather than an empty outcome.
    """
    last = rec.state_history[-1] if rec.state_history else None
    if last is None or last.to_state.value != "memorialized":
        return "unknown"
    return last.reason or "unknown"


def _skill_id_from_call(call: "ToolCall") -> str | None:
    """Return the target skill name for load_skill / unload_skill calls.

    Surfaces it as a top-level `skill_id` payload field so the ledger's
    `skill_id` virtual column (doc/54 §5 P0-1) indexes per-skill usage.
    Returns None for all other tools.
    """
    if call.tool_name not in _SKILL_PAYLOAD_TOOL_NAMES:
        return None
    name = call.args.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    return None


class LifecycleMiddleware(Middleware):
    """
    Outer lifecycle middleware — ActionRecord bookends and post-execution flow.

    Sits at the outermost position in the middleware pipeline.  Wraps every
    tool call in an ActionRecord and drives the lifecycle state machine.

    Architecture (Issue #50: Control-first Lifecycle)
    -------------------------------------------------
    Phase 1 (Issue #42) stamped states retroactively after the inner
    pipeline completed.  Phase 2 makes each state a genuine gate by
    splitting the lifecycle into two cooperating middleware layers.

    This outer layer handles:
        DECLARED        — create ActionRecord, inject LifecycleContext
        post-OBSERVED   — VALIDATED, COMMITTED, REVERTING, REVERTED
        MEMORIALIZED    — trace written to audit / episodic
        failure paths   — DENIED, ABORTED (from inner middleware)

    The inner layer (LifecycleGateMiddleware) handles:
        AUTHORIZED → PREPARED → EXECUTING → OBSERVED

    ┌─────────────────────────────────────────────────────────────────┐
    │ LifecycleMiddleware (outer)                                     │
    │   DECLARED — ActionRecord created, LifecycleContext injected   │
    │                                                                 │
    │   ↓ inner pipeline                                             │
    │     TraceMiddleware → SchemaValidation → BlastRadius           │
    │                                                                 │
    │   ↓ LifecycleGateMiddleware (inner)                            │
    │     AUTHORIZED ← BlastRadius wrote to LifecycleContext         │
    │     PREPARED   ← precondition_checks[] evaluated               │
    │     EXECUTING  ← fires at exact dispatch + abort racing        │
    │     OBSERVED   ← executor returned                             │
    │                                                                 │
    │   ↑ back to this outer layer                                   │
    │   VALIDATED → COMMITTED  or  REVERTING → REVERTED              │
    │   MEMORIALIZED                                                  │
    └─────────────────────────────────────────────────────────────────┘

    Terminal failure states: DENIED, ABORTED, TIMED_OUT.

    Backward compatibility
    ----------------------
    Tools with no precondition_checks, post_validator, or rollback_fn
    follow: DECLARED → AUTHORIZED → PREPARED → EXECUTING → OBSERVED
    → COMMITTED → MEMORIALIZED — identical to Phase 1 happy path.
    """

    def __init__(
        self,
        registry: Any,
        on_lifecycle: Callable[["ActionRecord"], Awaitable[None]] | None = None,
        on_state_change: Callable[["ActionRecord", str, str], Awaitable[None]] | None = None,
        ledger_emitter: Any = None,
    ) -> None:
        from .lifecycle import (
            ActionRecord, ActionIntent, ActionState,
            LifecycleContext, LIFECYCLE_CTX_KEY,
        )
        self._registry = registry
        self._on_lifecycle = on_lifecycle
        self._on_state_change = on_state_change
        # Store class references to avoid circular imports at call time
        self._ActionRecord = ActionRecord
        self._ActionIntent = ActionIntent
        self._ActionState = ActionState
        self._LifecycleContext = LifecycleContext
        self._CTX_KEY = LIFECYCLE_CTX_KEY
        # Optional ledger emitter (doc/53 §11.1 — Middleware-layer events).
        # When wired, emits one tool_lifecycle BEGIN at process start and
        # one tool_lifecycle END at memorialize (terminal). state_history
        # is bundled into the END payload (§3.1 — "state_history 內嵌進
        # 事件 payload，不另存"). rolled_back flag distinguishes REVERTED
        # outcomes; we keep phase="END" rather than a separate ROLLBACK
        # phase since §7.2 already provides rolled_back as a payload field.
        self._ledger_emitter = ledger_emitter
        # Records whose END row was written. process() cites the END event id
        # to the model only when it is here (#690 S3c review).
        self._ended: set[str] = set()

    @staticmethod
    async def _advance_to(
        ctx: "LifecycleContext",
        target: "ActionState",
        auth_reason: str = "passed blast radius",
    ) -> None:
        """Advance record through pre-execution states up to *target*.

        Handles DECLARED, AWAITING_CONFIRM, AUTHORIZED, PREPARED, EXECUTING
        in order, stopping when *target* is reached or surpassed.
        Extracted to avoid DRY violations across fallback paths (#108 review).
        """
        from .lifecycle import ActionState as AS
        _chain = [
            (AS.DECLARED,          AS.AUTHORIZED, auth_reason),
            (AS.AWAITING_CONFIRM,  AS.AUTHORIZED, auth_reason),
            (AS.AUTHORIZED,        AS.PREPARED,   None),
            (AS.PREPARED,          AS.EXECUTING,  None),
            (AS.EXECUTING,         AS.OBSERVED,   None),
        ]
        for from_state, to_state, reason in _chain:
            if ctx.record.state == from_state:
                await ctx.transition(to_state, reason=reason)
            if ctx.record.state == target:
                break

    async def process(self, call: ToolCall, next: ToolHandler) -> ToolResult:
        ActionRecord = self._ActionRecord
        ActionIntent = self._ActionIntent
        ActionState = self._ActionState

        # ── Build intent from tool definition ─────────────────────────
        tool_def = self._registry.get(call.tool_name)
        intent = ActionIntent(
            intent_summary=f"{call.tool_name}({', '.join(f'{k}=...' for k in call.args)})",
            scope=getattr(tool_def, "impact_scope", "general") if tool_def else "general",
            preconditions=list(getattr(tool_def, "preconditions", [])) if tool_def else [],
        )

        # ── DECLARED ──────────────────────────────────────────────────
        record = ActionRecord(call=call, intent=intent)

        # Compose on_lifecycle: ledger END emit runs first, then user hook.
        on_lifecycle = self._on_lifecycle
        if self._ledger_emitter is not None:
            on_lifecycle = self._compose_on_lifecycle(call, record)

        # Inject LifecycleContext with shared callbacks
        ctx = self._LifecycleContext(
            record=record,
            _on_state_change=self._on_state_change,
            _on_lifecycle=on_lifecycle,
        )
        call.metadata[self._CTX_KEY] = ctx

        # ── ledger BEGIN emit (best-effort; never breaks the call) ────
        if self._ledger_emitter is not None:
            await self._emit_lifecycle_begin(call, record)

        try:
            result = await self._run(call, next, ctx, record, tool_def)
            if record.id in self._ended:
                # The END row's id, so the model can cite it (#690 S3c).
                result.metadata = {
                    **(result.metadata or {}),
                    "ledger_event_id": f"evt_tool_end_{record.id[:12]}",
                }
            return result
        except BaseException as exc:
            # #690 S3b: cancelled (a preempting turn, CancelledError while
            # waiting at confirm) or an exception passed through before
            # memorialize. Record the END this BEGIN is owed, then re-raise.
            if (
                self._ledger_emitter is not None
                and record.state != ActionState.MEMORIALIZED
            ):
                outcome = (
                    "cancelled" if isinstance(exc, asyncio.CancelledError)
                    else "exception"
                )
                # rec.result, not None: if a result already exists, keep its
                # digest and summary. See _shielded_end for what shield does
                # and does not protect.
                await self._shielded_end(
                    call, record, outcome=outcome, result=record.result,
                    error=(
                        f"{type(exc).__name__}: {exc}" if str(exc)
                        else type(exc).__name__
                    ),
                )
            raise
        finally:
            self._ended.discard(record.id)

    async def _run(
        self,
        call: ToolCall,
        next: ToolHandler,
        ctx: "LifecycleContext",
        record: "ActionRecord",
        tool_def: Any,
    ) -> ToolResult:
        ActionState = self._ActionState

        # ── Run inner pipeline ────────────────────────────────────────
        # The chain: Trace → SchemaValidation → BlastRadius →
        # LifecycleGateMiddleware → handler.
        #
        # LifecycleGateMiddleware drives AUTHORIZED → PREPARED →
        # EXECUTING → OBSERVED as real control gates.
        # BlastRadiusMiddleware writes auth decisions to ctx.
        result = await next(call)

        # ── Post-pipeline analysis ────────────────────────────────────
        # If the record already reached a terminal state (precondition
        # failure, abort during execution), everything is done.
        if record.is_terminal:
            return result

        # ── Handle failures from inner middleware ─────────────────────
        # These come from SchemaValidation or BlastRadius, BEFORE
        # LifecycleGateMiddleware ever ran.

        if not result.success and result.failure_type == "permission_denied":
            if record.state in (ActionState.DECLARED, ActionState.AWAITING_CONFIRM):
                await ctx.transition(ActionState.DENIED, reason=result.error)
                record.result = result
                # An unattended skip ends as "skipped", not "denied" (#690 S7
                # review). The state machine still records DENIED: the call
                # never ran either way.
                skipped = result.metadata.get("unattended_skip")
                await ctx.memorialize("skipped" if skipped else "denied")
                return result

        if not result.success and result.failure_type == "tool_not_found":
            if record.state in (ActionState.DECLARED, ActionState.AWAITING_CONFIRM):
                record.result = result
                await ctx.transition(ActionState.DENIED, reason="tool not found")
                await ctx.memorialize("tool_not_found")
                return result

        if not result.success and result.failure_type == "validation_error":
            # Schema validation failed AFTER authorization passed
            auth_reason = ctx.authorization_reason or "passed blast radius"
            await self._advance_to(ctx, ActionState.AUTHORIZED, auth_reason)
            await ctx.transition(ActionState.ABORTED, reason=result.error)
            record.result = result
            await ctx.memorialize("validation_error")
            return result

        if not result.success and result.failure_type == "timeout":
            # Timeout from the handler (e.g. run_bash timeout).
            # LifecycleGateMiddleware already transitioned through
            # AUTHORIZED → PREPARED → EXECUTING.
            if record.state == ActionState.EXECUTING:
                await ctx.transition(ActionState.TIMED_OUT, reason=result.error)
                record.result = result
                await ctx.memorialize("timed_out")
                return result
            # Fallback: force through remaining states
            if not record.state.is_terminal:
                await self._advance_to(ctx, ActionState.EXECUTING)
                await ctx.transition(ActionState.TIMED_OUT, reason=result.error)
                record.result = result
                await ctx.memorialize("timed_out")
            return result

        # ── Happy path: OBSERVED already fired by LifecycleGateMiddleware ─
        if record.state != ActionState.OBSERVED:
            # Safety fallback: LifecycleGateMiddleware did not run, which
            # means the pipeline is misconfigured.  Log at ERROR so the gap
            # is immediately visible rather than silently papered over.
            _log.error(
                "LifecycleGateMiddleware did not run for %r (state=%s) — "
                "pipeline may be misconfigured; stamping states retroactively",
                call.tool_name, record.state.value,
            )
            auth_reason = ctx.authorization_reason or "passed blast radius"
            await self._advance_to(ctx, ActionState.OBSERVED, auth_reason)
            record.result = result

        # ── Post-validation (if post_validator defined) ───────────────
        post_validator = getattr(tool_def, "post_validator", None) if tool_def else None
        rollback_fn = getattr(tool_def, "rollback_fn", None) if tool_def else None

        if post_validator is not None and result.success:
            try:
                raw_verdict = await post_validator(call, result)
            except Exception as _val_exc:
                _log.warning(
                    "post_validator for %r raised unexpectedly: %s — treating as passed",
                    call.tool_name, _val_exc, exc_info=True,
                )
                _trace_middleware(
                    call, "Lifecycle", "post-validator-raised",
                    reason=str(_val_exc),
                    error_context=traceback.format_exc(),
                )
                raw_verdict = True

            # Normalize legacy bool return type to VerifierResult (Issue #196).
            if isinstance(raw_verdict, VerifierResult):
                verdict = raw_verdict
            elif raw_verdict is True:
                verdict = VerifierResult(passed=True)
            else:
                verdict = VerifierResult(
                    passed=False, reason="post-validation failed"
                )

            if verdict.passed:
                # OBSERVED → VALIDATED → COMMITTED
                await ctx.transition(ActionState.VALIDATED)
                await ctx.transition(ActionState.COMMITTED)
            else:
                # OBSERVED → VALIDATED (fail) → REVERTING → REVERTED
                await ctx.transition(ActionState.VALIDATED)
                reason = verdict.reason or "post-validation failed"

                if rollback_fn is not None:
                    await ctx.transition(
                        ActionState.REVERTING, reason=reason,
                    )
                    try:
                        rb_result = await rollback_fn(call, result)
                        record.rollback_result = rb_result
                    except Exception as exc:
                        tb = traceback.format_exc()
                        _log.warning(
                            "rollback_fn for %r raised: %s",
                            call.tool_name, exc, exc_info=True,
                        )
                        _trace_middleware(
                            call, "Lifecycle", "rollback-raised",
                            reason=str(exc), error_context=tb,
                        )
                        record.rollback_result = ToolResult(
                            call_id=call.id,
                            tool_name=call.tool_name,
                            success=False,
                            error=f"Rollback failed: {exc}",
                            error_context=tb,
                        )
                    await ctx.transition(ActionState.REVERTED)
                    # Rolled-back result — still a semantic failure at heart.
                    result = ToolResult(
                        call_id=result.call_id,
                        tool_name=result.tool_name,
                        success=False,
                        error=f"{reason}; action rolled back.",
                        failure_type="semantic_failure",
                        duration_ms=result.duration_ms,
                        metadata={
                            **result.metadata,
                            "rolled_back": True,
                            "verifier_signal": verdict.signal,
                        },
                    )
                    record.result = result
                else:
                    # Issue #196: no rollback doesn't mean ignore the signal.
                    # Previously this branch silently committed a "success"
                    # result — the failure signal was lost. Now we surface
                    # semantic_failure so the model can self-correct.
                    # Issue #298: the agent must still see what actually
                    # happened (traceback text, test output). The output
                    # stays in `output`, and the failure contract shows its
                    # bounded tail (#690 S3c). Copying it into `error` too
                    # sent unbounded text twice.
                    original_output = result.output
                    result = ToolResult(
                        call_id=result.call_id,
                        tool_name=result.tool_name,
                        success=False,
                        output=original_output,
                        error=reason,
                        failure_type="semantic_failure",
                        duration_ms=result.duration_ms,
                        metadata={
                            **result.metadata,
                            "verifier_signal": verdict.signal,
                            "verifier_reason": reason,
                            "original_output": original_output,
                        },
                    )
                    record.result = result
                    await ctx.transition(ActionState.COMMITTED)
        else:
            # No post_validator → skip VALIDATED, go directly to COMMITTED
            await ctx.transition(ActionState.COMMITTED)

        # ── MEMORIALIZED ──────────────────────────────────────────────
        await ctx.memorialize(record.state.value)

        return result

    # -- ledger emit helpers (Phase 2 Step 2 commit 5) -------------------

    @staticmethod
    def _args_digest(args: dict[str, Any]) -> str:
        # NOTE: ``default=str`` is a survival fallback for args carrying
        # non-JSON-serialisable objects (Path, datetime, custom dataclasses
        # without to_dict). Two distinct objects whose ``str()`` repr happens
        # to collide will produce the same digest. This is acceptable for
        # tool_lifecycle's purpose — args_digest is used to GROUP events
        # for the same call across BEGIN/END (where the digest is fixed by
        # the call.id linkage anyway) and for replay heuristics. It is
        # NOT a content-addressed dedup primitive; do not rely on it for
        # safety-critical equality checks.
        import hashlib
        import json as _json
        try:
            blob = _json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
        except Exception:
            blob = repr(sorted(args.items()))
        return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()

    @staticmethod
    def _result_digest(result: "ToolResult | None") -> str | None:
        if result is None:
            return None
        import hashlib
        body = output_text(result.output) + "|" + (result.error or "")
        return "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()

    async def _maybe_emit_artifact(
        self, call: "ToolCall", result: "ToolResult", rec: "ActionRecord"
    ) -> None:
        info = extract_artifact_info(call, result)
        if info is None:
            return
        from loom.core.ledger import ArtifactEmitPayload
        await self._ledger_emitter.emit_artifact_emit(
            payload=ArtifactEmitPayload(
                artifact_type=info["artifact_type"],
                size_bytes=info["size_bytes"],
                digest=info["digest"],
                location=info["location"],
            ),
            parent_event_id=f"evt_tool_end_{rec.id[:12]}",
        )

    async def _emit_lifecycle_begin(
        self, call: "ToolCall", record: "ActionRecord"
    ) -> None:
        """Emit tool_lifecycle phase=BEGIN. Failure never breaks the call."""
        from loom.core.ledger import ToolLifecyclePayload

        try:
            await self._ledger_emitter.emit_tool_lifecycle(
                payload=ToolLifecyclePayload(
                    phase="BEGIN",
                    tool_name=call.tool_name,
                    tool_call_id=call.id,
                    args_digest=self._args_digest(call.args),
                    args=None,  # full args 留 future debug mode；BEGIN 帶 digest 即可
                    state_history=[],
                    skill_id=_skill_id_from_call(call),
                ),
                event_id=f"evt_tool_begin_{record.id[:12]}",
            )
        except Exception as exc:
            _log.exception("ledger tool_lifecycle BEGIN emit failed; continuing")
            await self._ledger_emitter.record_gap(
                missing_event_type="tool_lifecycle",
                missing_event_id=f"evt_tool_begin_{record.id[:12]}",
                exc=exc, tool_name=call.tool_name, tool_call_id=call.id,
            )

    async def _shielded_end(
        self, call: "ToolCall", rec: "ActionRecord", **kwargs: Any,
    ) -> None:
        """Write the END under ``asyncio.shield``.

        shield protects the write, not the await. A cancel delivered while
        we wait makes this await raise at once, and the write carries on as
        a detached task: it lands as long as the event loop keeps running.
        If the loop closes first (e.g. the CLI exits), that END is lost and
        no ledger_gap is written for it.
        """
        await asyncio.shield(self._emit_lifecycle_end(call, rec, **kwargs))

    async def _emit_lifecycle_end(
        self,
        call: "ToolCall",
        rec: "ActionRecord",
        *,
        outcome: str | None,
        result: "ToolResult | None" = None,
        error: str | None = None,
    ) -> None:
        """Emit tool_lifecycle phase=END. Failure never breaks the call.

        ``result`` is None when the call ended without one (cancelled at
        confirm, say); the END then carries no digest or summary, only
        ``outcome`` and ``error``.
        """
        from loom.core.ledger import ToolLifecyclePayload

        event_id = f"evt_tool_end_{rec.id[:12]}"
        summary: str | None = None
        if result is not None and result.success:
            summary = output_text(result.output)[:200] or None
        elif result is not None:
            summary = result.error[:200] if result.error else None
            # An explicit error (the exception that ended process()) wins
            # over the result's own error text.
            error = error or result.error
        try:
            await self._ledger_emitter.emit_tool_lifecycle(
                payload=ToolLifecyclePayload(
                    phase="END",
                    tool_name=call.tool_name,
                    tool_call_id=call.id,
                    args_digest=self._args_digest(call.args),
                    result_digest=self._result_digest(result),
                    result_summary=summary,
                    state_history=rec.history_dicts(),
                    rolled_back=any(
                        t.to_state.value == "reverted" for t in rec.state_history
                    ),
                    error=error,
                    skill_id=_skill_id_from_call(call),
                    outcome=outcome,
                ),
                event_id=event_id,
                # parent links the END to its sibling BEGIN, whose event_id
                # was derived from record.id the same way.
                parent_event_id=f"evt_tool_begin_{rec.id[:12]}",
            )
            self._ended.add(rec.id)
        except Exception as exc:
            _log.exception(
                "ledger tool_lifecycle END emit failed; continuing "
                "(tool_name=%s call_id=%s event_id=%s)",
                call.tool_name, call.id, event_id,
            )
            await self._ledger_emitter.record_gap(
                missing_event_type="tool_lifecycle",
                missing_event_id=event_id,
                exc=exc, tool_name=call.tool_name, tool_call_id=call.id,
            )

    def _compose_on_lifecycle(
        self, call: "ToolCall", record: "ActionRecord"
    ) -> Callable[["ActionRecord"], Awaitable[None]]:
        """Return a wrapper that emits END to ledger then calls user hook."""
        original = self._on_lifecycle

        async def wrapped(rec: "ActionRecord") -> None:
            result = rec.result
            # By the time on_lifecycle fires, state is MEMORIALIZED;
            # check the history for any REVERTED transition.
            rolled_back = any(
                t.to_state.value == "reverted" for t in rec.state_history
            )
            # Shielded: a cancel landing mid-write (state is already
            # MEMORIALIZED, so process() will not write a fallback END)
            # must not drop this END.
            await self._shielded_end(
                call, rec, outcome=_memorialize_reason(rec), result=result,
            )

            # #334 — emit artifact_emit for known producer tools.
            if result is not None and result.success and not rolled_back:
                try:
                    await self._maybe_emit_artifact(call, result, rec)
                except Exception:
                    _log.exception("ledger artifact_emit failed; continuing")

            if original is not None:
                await original(rec)

        return wrapped
