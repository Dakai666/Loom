"""
Loom CLI — thin platform wrapper.

``LoomSession`` and all session logic live in ``loom.core.session``.
This module provides the click command group and Rich-rendered streaming
output that are specific to the terminal interface.

Usage
-----
    loom chat                         # MiniMax-M2.7 (default)
    loom chat --model MiniMax-M2.7-highspeed
    loom chat --model claude-sonnet-4-6
    loom memory list
    loom reflect --session <id>
"""

import asyncio
import json
import logging
import os
import subprocess
import time
import tomllib
from pathlib import Path
from typing import Any

import click
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.rule import Rule
from rich.text import Text

# Force UTF-8 output on Windows so the Rich console can render full Unicode.
import sys as _sys

if _sys.platform == "win32":
    try:
        _sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        _sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, Exception):
        pass

from loom.core.session import (
    LoomSession,
    build_router,
    compress_session,
    _load_loom_config,
    _load_env,
)
from loom.core.cognition.reflection import ReflectionAPI
from loom.core.harness.middleware import BlastRadiusMiddleware
from loom.core.memory.episodic import EpisodicMemory
from loom.core.memory.procedural import ProceduralMemory
from loom.core.memory.relational import RelationalMemory
from loom.core.memory.semantic import SemanticMemory
from loom.core.memory.store import SQLiteStore
from loom.core.memory.session_log import SessionLog
from loom.platform.cli.harness_channel import HarnessChannel
from loom.platform.cli.theme import LOOM_THEME
from loom.platform.cli.theme_persist import load_preference, save_preference, available_themes
from loom.platform.cli.ui import (
    ActionRolledBack,
    ActionStateChange,
    CompressDone,
    ReasoningContinuation,
    TextChunk,
    ThinkCollapsed,
    TierChanged,
    TierExpiryHint,
    ToolBegin,
    ToolEnd,
    TurnDone,
    TurnDropped,
    TurnPaused,
    clear_line,
    status_bar,
    tool_begin_line,
    tool_end_line,
    tool_running_line,
)

console = Console(highlight=False, theme=LOOM_THEME)

# Harness messages route through this channel — see harness_channel.py.
# Module-level instance so non-_chat code paths (slash commands, error
# handlers) can emit without threading a parameter through every call.
harness = HarnessChannel(console)
logger = logging.getLogger(__name__)


def _project_env_path() -> Path:
    """Return the .env path Loom should update for CLI onboarding."""
    return Path.cwd() / ".env"


def _set_env_value(path: Path, key: str, value: str) -> None:
    """Set KEY=value in a dotenv file while preserving unrelated lines."""
    encoded = f"{key}={value}"
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    out: list[str] = []
    replaced = False
    prefix = f"{key}="
    for line in lines:
        if line.startswith(prefix):
            if not replaced:
                out.append(encoded)
                replaced = True
            continue
        out.append(line)
    if not replaced:
        if out and out[-1].strip():
            out.append("")
        out.append(encoded)
    path.write_text("\n".join(out) + "\n", encoding="utf-8")

# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------


@click.group()
    # ==============================================================
    # SECTION 1 — CLI ENTRY POINT (cli)
    # ==============================================================
def cli() -> None:
    """Loom — harness-first agent framework."""


@cli.command()
@click.option("--model", default=None, show_default=True)
@click.option("--db", default="~/.loom/memory.db", show_default=True)
@click.option("--resume", is_flag=True, default=False, help="Resume the most recent session.")
@click.option("--session", "resume_id", default=None, metavar="ID", help="Resume a specific session by ID.")
@click.option("--name", "name", default=None, metavar="TITLE",
              help="Set a title on the new (or resumed) session for easier identification.")
def chat(model: str, db: str, resume: bool, resume_id: str | None,
         name: str | None) -> None:
    # ==============================================================
    # SECTION 2 — CHAT (chat, _chat)
    # ==============================================================
    """Start an interactive agent session."""
    asyncio.run(_resolve_and_chat(model, db, resume, resume_id, name))


async def _resolve_and_chat(
    model: str,
    db: str,
    resume: bool,
    resume_id: str | None,
    name: str | None = None,
) -> None:
    """Resolve --resume / --session flags, then launch the CLI chat loop."""
    if model is None:
        from loom.core.cognition.router import get_default_model
        model = get_default_model()
    resolved_id = resume_id
    if resume and resolved_id is None:
        store = SQLiteStore(db)
        await store.initialize()
        async with store.connect() as conn:
            sl = SessionLog(conn)
            rows = await sl.list_sessions(limit=1)
        if rows:
            resolved_id = rows[0]["session_id"]
            title = rows[0].get("title") or "(no title)"
            console.print(f"[loom.muted]Resuming session [loom.accent]{resolved_id}[/loom.accent]: {title}[/loom.muted]")
        else:
            console.print("[loom.muted]No sessions found — starting a new session.[/loom.muted]")

    # Issue #260: CLI session switch loop — ``/sessions`` and ``/new``
    # set ``session._cli_next_target`` and then exit the LoomApp.
    # ``_chat`` returns that target string; we restart with the requested
    # session. ``--name`` is applied only on first iteration (subsequent
    # switches inherit whatever title is in DB).
    next_target: str | None = resolved_id
    init_title: str | None = name
    while True:
        switch_to = await _chat(
            model, db,
            resume_session_id=next_target,
            init_title=init_title,
        )
        # ``--name`` is intentionally a one-shot: it labels the session
        # the user just opened, not every session they might subsequently
        # switch to via ``/sessions``. Each post-switch session keeps
        # whatever title is in the DB.
        init_title = None
        if switch_to is None:
            return
        if switch_to == "__new__":
            next_target = None
        else:
            next_target = switch_to


async def _chat(
    model: str,
    db: str,
    resume_session_id: str | None = None,
    init_title: str | None = None,
) -> str | None:
    """Run one chat session. Returns:

    - ``None`` on normal exit (user closed the app)
    - a ``session_id`` string when ``/sessions`` picked a different session
    - ``"__new__"`` when ``/new`` requested a fresh session
    """
    session = LoomSession(model=model, db_path=db, resume_session_id=resume_session_id)
    await session.start()

    # Issue #260: apply ``--name`` (or the title set by ``/name``) so the
    # session shows up in the picker / ``loom sessions list`` with the
    # operator-friendly label rather than just its UUID
    if init_title:
        try:
            async with session._store.connect() as conn:
                await SessionLog(conn).update_title(session.session_id, init_title)
        except Exception as exc:
            harness.inline(
                f"could not apply --name {init_title!r}: {exc}",
                level="error",
            )

    # Issue #260: signal channel for ``/sessions`` and ``/new`` to
    # request a session restart. Slash handler sets it; ``turn_loop``
    # observes it and exits the LoomApp; this function reads + returns
    # it so the outer loop in ``_resolve_and_chat`` can act
    session._cli_next_target = None  # type: ignore[attr-defined]

    # Issue #120 PR1: show diagnostic summaries inline in the CLI.
    async def _cli_diagnostic(diagnostic):
        vis = session._reflection_visibility
        if vis == "off":
            return
        console.print(f"[loom.muted]  ⇢ diagnosed {diagnostic.one_line_summary()}[/loom.muted]")
        if vis == "verbose" and diagnostic.skill_edit_suggestions:
            for hint in diagnostic.skill_edit_suggestions[:2]:
                console.print(f"[loom.muted]      · {hint}[/loom.muted]")

    session.subscribe_diagnostic(_cli_diagnostic)


    # PR-C3: route BlastRadiusMiddleware authorisation decisions through
    # the harness channel. Red-light events留底 inline so the user can
    # forensically trace why a tool was blocked. Green-light events stay
    # silent — pre-authorized / exec_auto / scope-allow are routine
    # successes, not events worth announcing. PR-D will reassess whether
    # any *specific* green-light kind (e.g. "new scope grant just issued")
    # deserves a footer flash, but blanket flashing every approval would
    # spam the surface and contradict doc/49's "綠燈不出聲" principle.
    def _on_lifecycle(call: "ToolCall", result: bool, reason: str) -> None:
        if not result:
            harness.inline(
                f"auth denied: {call.tool_name} — {reason}",
                level="warning",
            )

    for _mw in session._pipeline._middlewares:
        if isinstance(_mw, BlastRadiusMiddleware):
            _mw._on_lifecycle_event = _on_lifecycle
            break

    # Route LegitimacyGuardMiddleware's trajectory-anomaly warning
    # through the harness channel too. Before this, the message
    # landed via the stdlib logger and printed unstyled into scrollback,
    # looking like it belonged to the surrounding tool args (the user
    # could not tell it was a separate harness signal).
    def _on_trajectory_anomaly(tool_name: str, origin: str) -> None:
        harness.inline(
            f"trajectory anomaly: {tool_name} ran EXEC without a prior "
            f"probe (origin={origin}); exec_auto fast-pass revoked.",
            level="warning",
        )

    from loom.core.harness.middleware import LegitimacyGuardMiddleware
    for _mw in session._pipeline._middlewares:
        if isinstance(_mw, LegitimacyGuardMiddleware):
            _mw._on_trajectory_anomaly = _on_trajectory_anomaly
            break

    # PR-C4: surface history sanitize repairs and governor rejections.
    # Both are silent today; making them visible takes them off the
    # "weird invisible behaviour" list that haunts users of generative
    # systems.
    def _on_sanitize(args_fixed: int, msgs_dropped: int) -> None:
        parts: list[str] = []
        if args_fixed:
            parts.append(f"{args_fixed} arg(s) repaired")
        if msgs_dropped:
            parts.append(f"{msgs_dropped} orphan message(s) dropped")
        if parts:
            harness.inline(f"sanitize: {', '.join(parts)}", level="info")

    def _on_governor_reject(key: str, tier: str, contradictions: int) -> None:
        detail = f"tier={tier}"
        if contradictions:
            detail += f", {contradictions} contradiction(s)"
        harness.inline(
            f"governor blocked memorize {key!r} ({detail})",
            level="warning",
        )

    session._on_sanitize_repaired = _on_sanitize       # type: ignore[attr-defined]
    session._on_governor_reject = _on_governor_reject  # type: ignore[attr-defined]

    # PR-D4: clear the noisy startup output (resume log, MCP load,
    # diagnostic block, …) before drawing the welcome signature so
    # the user sees a clean ceremony instead of scrollback debris.
    console.clear()

    # 5-line ASCII signature replaces the old render_header Panel +
    # MemoryIndex Panel splatter. The full MemoryIndex still feeds
    # the LLM's system prompt unchanged.
    from loom.platform.cli.ui import render_welcome_signature
    _idx = session._memory_index

    # Issue #260: surface session title + short id in the signature so
    # the user always knows which thread they're sitting in (after
    # ``/sessions`` switch, or after ``--resume``)
    _session_title: str | None = None
    try:
        async with session._store.connect() as conn:
            _meta = await SessionLog(conn).get_session(session.session_id)
        if _meta:
            _session_title = _meta.get("title")
    except Exception as exc:
        # Cosmetic: title is a presentation field. Fail open with a
        # debug-level note so prod issues are still traceable
        logger.debug("session title lookup failed: %s", exc)

    console.print(
        render_welcome_signature(
            model=model,
            persona=session.current_personality,
            skill_count=getattr(_idx, "skill_count", 0),
            fact_count=getattr(_idx, "semantic_count", 0),
            mcp_count=len(session._mcp_clients),
            episode_count=getattr(_idx, "episode_sessions", 0),
            relation_count=getattr(_idx, "relational_count", 0),
            session_title=_session_title,
            session_id_short=session.session_id[:8],
        )
    )

    # Issue #260: when resuming a session that has prior content, echo
    # the last few user / assistant turns into scrollback so the user
    # has visual context for what was discussed before. Limited to last
    # 4 messages and 240 chars each — full history is in the DB and
    # still in the LLM's context, this is just a visual aid
    if resume_session_id and session.session_id == resume_session_id:
        try:
            async with session._store.connect() as conn:
                _msgs = await SessionLog(conn).load_messages(session.session_id)
        except Exception as exc:
            logger.debug("resume preview load_messages failed: %s", exc)
            _msgs = []
        # Keep only user/assistant text turns; drop tool messages and
        # blank assistant entries (those usually mean a tool-only turn)
        _preview = [
            m for m in _msgs
            if m.get("role") in ("user", "assistant")
            and isinstance(m.get("content"), str)
            and m.get("content", "").strip()
        ][-4:]
        if _preview:
            console.print()
            console.print(
                "[loom.muted]  ↳ previous turns "
                f"({len(_msgs)} messages total)[/loom.muted]"
            )
            for m in _preview:
                role = m.get("role", "")
                content = m.get("content", "").strip().replace("\n", " ")
                if len(content) > 240:
                    content = content[:237] + "…"
                tag = (
                    "[loom.accent]you ›[/loom.accent]"
                    if role == "user"
                    else "[loom.muted]Loom ▎[/loom.muted]"
                )
                console.print(f"  {tag} [loom.muted]{content}[/loom.muted]")
            console.print()

    # PR-D4: anchor the persistent app's bottom region to the actual
    # bottom of the terminal. ``full_screen=False`` mode draws the
    # bottom area at whatever row the cursor was on when run() was
    # called — without padding, that's just below the welcome sig
    # near the top of the terminal, leaving a sea of empty rows
    # underneath. Pad with blank lines so the cursor sits near the
    # terminal's last row before the app starts. As streaming output
    # arrives via patch_stdout, those padding rows scroll up and out
    # naturally
    import shutil as _shutil
    import sys as _sys
    _term_h = _shutil.get_terminal_size(fallback=(80, 24)).lines
    # Welcome sig footprint ~ 12 lines (Columns of two Panels: 2 borders
    # + 7-line LOOM logo + 1 blank + 2 motto lines on the left; right
    # info panel is shorter and shares the same outer borders).
    # bottom area ~ 5 lines (thinking + sep_top + input + sep_bottom +
    # footer). Conditional/thinking only shows during agent calls so a
    # frame more is fine.
    _pad = max(0, _term_h - 12 - 5)
    _sys.stdout.write("\n" * _pad)
    _sys.stdout.flush()

    # ── PR-D1: persistent prompt_toolkit Application ──────────────────────
    #
    # Replaces PR-A's per-iteration ``prompt_async`` + three-event stdin
    # coordinator. The LoomApp owns the bottom region of the terminal
    # (input area + footer + transient confirm/pause overlays) for the
    # entire chat session. Streaming output flows into the natural
    # scrollback above it via ``patch_stdout``, which is still needed
    # to keep ``console.print`` calls cooperating with the persistent
    # bottom rendering.
    #
    # Producer/consumer architecture:
    #   - LoomApp.on_submit  — fires when user presses Enter in INPUT
    #                          mode; pushes the text into input_queue.
    #                          If a turn is in flight, cancel + queue
    #                          with an interrupt prefix.
    #   - turn_loop          — drains the queue and runs streaming
    #                          turns via _run_streaming_turn.
    #
    # Confirm / HITL pause / HITL redirect — all routed through the
    # app's mode-flag mechanism (LoomApp.request_confirm / request_pause
    # / request_redirect_text). No nested Applications; the widgets
    # render inside the same app's layout, so the "Application is not
    # running" race that PR-A worked around with three-event coordination
    # simply cannot occur.
    _INTERRUPT_PREFIX = "\x00LOOM_INTERRUPT\x00"
    input_queue: asyncio.Queue[str] = asyncio.Queue()
    current_turn_task: asyncio.Task | None = None
    shutdown = asyncio.Event()

    # Build the persistent app first so we can register its callbacks
    # before kicking off the run loop. ``app`` is captured by the
    # on_submit closure below.
    from loom.platform.cli.app import build_loom_app

    async def _on_submit(text: str) -> None:
        nonlocal current_turn_task
        stripped = text.strip()
        if not stripped:
            return

        if stripped.lower() in {"exit", "quit", "q"}:
            shutdown.set()
            app.application.exit()
            return

        # Echo the submitted text to scrollback so the user can see
        # what they sent. The persistent app's input buffer clears on
        # submit (so the bottom area becomes ready for the next
        # message), and without an explicit echo there'd be no record
        # of what was typed.
        echo_lines = stripped.splitlines() or [stripped]
        first, *rest = echo_lines
        console.print(
            f"[loom.muted]you ›[/loom.muted] [loom.text]{first}[/loom.text]"
        )
        for line in rest:
            console.print(f"[loom.muted]      [/loom.muted][loom.text]{line}[/loom.text]")

        # Abort-on-submit: if a turn is in flight, cancel and queue
        # the new message with the interrupt prefix.
        #
        # Issue #312: we used to call only `session.cancel()`, which
        # sets a soft `_abort` flag checked at LLM-call boundaries —
        # an in-flight `run_bash` (or any long await) would still run
        # to completion. Mirror /stop and Escape (action_interrupt) by
        # also cancelling the worker task, so awaits inside the turn
        # receive CancelledError immediately.
        in_flight = current_turn_task is not None and not current_turn_task.done()
        if in_flight:
            session.cancel()
            current_turn_task.cancel()
            harness.inline("⏸ 上一輪已中斷，接收新訊息…", level="info")
            try:
                await asyncio.wait_for(current_turn_task, timeout=3.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
            except Exception:
                pass
            await input_queue.put(_INTERRUPT_PREFIX + stripped)
        else:
            await input_queue.put(stripped)

    app = build_loom_app(on_submit=_on_submit)
    app.footer.model = model
    app.footer.persona = session.current_personality
    # Issue #276: seed tier badge state from session config so the footer
    # has the right context immediately (saves the "old tier showing for
    # one turn before refresh" UX issue from #275).
    app.footer.default_tier = session._default_tier if session._tier_models else 0
    app.footer.tier = session._active_tier() if session._tier_models else 0
    # Issue #284: transient footer hints toggle. Default on; set
    # ``[cli] transient_hints = false`` in loom.toml to disable.
    app.transient_hints_enabled = bool(
        session._loom_config.get("cli", {}).get("transient_hints", True)
    )

    # Wire confirm + pause routing into the session so _confirm_tool_cli
    # and the HITL pause path can render through the app's mode flag
    # instead of spinning their own short-lived Applications. Sessions
    # used outside `loom chat` (tests, scripts) still fall back to
    # select_prompt as before.
    session._loom_app = app  # type: ignore[attr-defined]

    # PR-E (#236): mirror task_write into the floating panel. The
    # manager fires ``on_change`` with the post-write status summary
    # after every task_write tool call (replace semantics — full list
    # every time), so the panel just re-renders from that snapshot.
    tlm = getattr(session, "_tasklist_manager", None)
    if tlm is not None:
        def _push_tasklist(summary: dict) -> None:
            app.update_tasklist(summary.get("todos") or [])
        tlm.on_change = _push_tasklist

    async def turn_loop() -> None:
        nonlocal current_turn_task
        while not shutdown.is_set():
            try:
                text = await asyncio.wait_for(input_queue.get(), timeout=0.25)
            except asyncio.TimeoutError:
                continue

            # PR-E follow-up: a fully-completed TaskList (collapsed to
            # ``✓ N/N done``) is stale the moment the user starts a new
            # turn. Without this it would linger until the next
            # task_write call, which feels like a UI leak. Clearing
            # here ``acknowledges'' the previous list and frees the
            # bottom region for whatever comes next
            if app._tasklist_state.collapsed:
                app.update_tasklist([])

            # Slash commands run inline — no streaming turn, no cancel.
            if text.startswith("/"):
                try:
                    await _handle_slash(text, session)
                except Exception as exc:
                    harness.inline(f"slash command error: {exc}", level="error")
                # Issue #260: ``/sessions`` / ``/new`` request a session
                # restart by setting ``session._cli_next_target``. The
                # full restart path is intentionally indirect:
                #   slash handler sets _cli_next_target
                #   → here: shutdown.set() + app.application.exit()
                #   → ``app.run()`` task completes → asyncio.wait wakes
                #   → ``_chat`` finally reads _cli_next_target and returns it
                #   → outer loop in ``_resolve_and_chat`` restarts with the
                #     new session_id
                # Don't shortcut this by returning directly — patch_stdout
                # cleanup + session.stop() must run via the existing path
                if getattr(session, "_cli_next_target", None) is not None:
                    shutdown.set()
                    try:
                        app.application.exit()
                    except Exception as _exc:
                        harness.inline(
                            f"app exit signalling failed: {_exc}",
                            level="error",
                        )
                continue

            # Detect interruption marker injected by _on_submit.
            if text.startswith(_INTERRUPT_PREFIX):
                text = text[len(_INTERRUPT_PREFIX):]
                text = "[使用者打斷上一輪並接續]\n" + text

            console.print()
            # Heartbeat (#418/#419). Light up THINKING the moment we
            # dispatch the streaming turn; stream-event handlers inside
            # _run_streaming_turn transition it to TOOLING / PAUSED /
            # back to idle. If the turn crashes / aborts before any
            # event lands, the finally below still returns to idle.
            from loom.platform.interaction_language import HeartbeatState
            app.start_heartbeat(
                state=HeartbeatState.THINKING.value,
                label="Loom is thinking",
                stale_after_s=30.0,
            )
            current_turn_task = asyncio.create_task(
                _run_streaming_turn(session, text)
            )
            try:
                await current_turn_task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                harness.inline(f"turn error: {exc}", level="error")
            finally:
                current_turn_task = None
                app.stop_heartbeat()

            # Update footer token budget + grants at each turn boundary
            # (per doc/49 decision: TTL refresh on turn edge, not per-
            # second tick — keeps the footer visually stable while the
            # user is reading).
            # Footer shows the *binding* pressure — token or block,
            # whichever is fuller — so we don't read "42%" while the
            # wire is actually one parallel-tool turn away from MiniMax's
            # 2013-block ceiling.
            app.footer.token_pct = session.budget.pressure * 100
            app.footer.persona = session.current_personality
            # Belt-and-suspenders for /model: slash handlers update
            # footer.model immediately on switch, but a future code path
            # could change session.model without going through them. Keep
            # the badge aligned with reality at every turn edge.
            # Issue #276: when tier system is active, show the tier-resolved
            # model rather than the raw session.model (they may diverge
            # mid-turn after a sticky escalation).
            if session._tier_models:
                app.footer.model = session._active_model()
                app.footer.tier = session._active_tier()
                app.footer.turns_at_tier = session._turns_at_current_tier
            else:
                app.footer.model = session.model
            try:
                snapshot = session._build_grants_snapshot()
                app.footer.grants_active = snapshot.active_count
                app.footer.grants_next_expiry_secs = snapshot.next_expiry_secs
            except Exception:
                pass
            app.invalidate()

    async def footer_ticker() -> None:
        """Tick the footer at 2 Hz so live fields (heartbeat elapsed,
        compaction spinner, transient hint expiry — #284) progress
        visibly."""
        while not shutdown.is_set():
            if (app.footer.heartbeat_state != "idle"
                    or app.footer.compacting
                    or app.footer.transient_hint is not None):
                app.invalidate()
            await asyncio.sleep(0.5)

    # patch_stdout makes console.print flow above the app's persistent
    # bottom region. raw=True passes escape sequences through so the
    # existing Rich rendering (panels, spinners) keeps working.
    from prompt_toolkit.patch_stdout import patch_stdout

    try:
        with patch_stdout(raw=True):
            app_task = asyncio.create_task(app.run())
            turn_task = asyncio.create_task(turn_loop())
            ticker_task = asyncio.create_task(footer_ticker())
            done, pending = await asyncio.wait(
                {app_task, turn_task, ticker_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            shutdown.set()
            for task in pending:
                task.cancel()
            for task in pending:
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
    finally:
        switch_to = getattr(session, "_cli_next_target", None)
        await session.stop()
        if switch_to is None:
            console.print("\n[loom.muted]Session ended. Goodbye.[/loom.muted]")
    return switch_to


def _format_ttl(g: Any) -> str:
    """Format a grant's TTL as human-readable string."""
    import time as _time
    if g.valid_until <= 0:
        return "session"
    remaining = max(0, g.valid_until - _time.time())
    if remaining > 3600:
        return f"{remaining / 3600:.1f}h"
    if remaining > 60:
        return f"{remaining / 60:.0f}m"
    return f"{remaining:.0f}s"


# Sources protected from /scope clear — system grants and exec_auto
# (which backs /auto mode and must not be silently removed).
_CLEAR_PROTECTED_SOURCES = frozenset({"system", "exec_auto"})


def _scope_command_core(
    perm: Any, args: str, emit: "Callable[[str], None]",
) -> None:
    """
    Shared /scope logic for CLI and Discord.

    ``emit`` receives plain-text messages (no Rich markup) — the caller
    is responsible for rendering.

    Side effects: mutates ``perm.grants`` for revoke/clear.
    """
    purged = perm.purge_expired()

    if not args or args == "list":
        if not perm.grants:
            msg = "No active scope grants."
            if purged:
                msg += f" ({purged} expired removed)"
            emit(msg)
            return
        lines = []
        for i, g in enumerate(perm.grants):
            ttl = _format_ttl(g)
            constraints_str = ", ".join(
                f"{k}={v}" for k, v in g.constraints.items()
            ) if g.constraints else ""
            line = f"#{i} {g.resource}/{g.action}/{g.selector[:30]} ({g.source}, {ttl})"
            if constraints_str:
                line += f" [{constraints_str}]"
            lines.append(line)
        if purged:
            lines.append(f"({purged} expired grant{'s' if purged != 1 else ''} removed)")
        emit("\n".join(lines))

    elif args.startswith("revoke"):
        parts = args.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip().isdigit():
            emit("Usage: /scope revoke <number>")
            return
        idx = int(parts[1].strip())
        if idx < 0 or idx >= len(perm.grants):
            emit(f"Grant #{idx} does not exist. Use /scope to list.")
            return
        g = perm.grants[idx]
        perm.revoke_matching(lambda grant, _g=g: grant is _g)
        emit(f"Revoked #{idx}: {g.resource}/{g.action}/{g.selector} (source={g.source})")

    elif args == "clear":
        before = len(perm.grants)
        perm.revoke_matching(lambda g: g.source not in _CLEAR_PROTECTED_SOURCES)
        removed = before - len(perm.grants)
        emit(
            f"Cleared {removed} grant{'s' if removed != 1 else ''}. "
            f"{len(perm.grants)} protected grant{'s' if len(perm.grants) != 1 else ''} remain."
        )

    else:
        emit("/scope — list | /scope revoke <N> | /scope clear")


def _handle_scope_command(session: "LoomSession", args: str, console: Any) -> None:
    """
    Handle /scope subcommands (CLI Rich output).

    /scope          — list active grants (Rich Table)
    /scope revoke N — revoke grant #N
    /scope clear    — revoke all non-system/exec_auto grants
    """
    from rich.table import Table

    perm = session.perm

    # For the list subcommand, render a Rich Table instead of plain text
    if not args or args == "list":
        purged = perm.purge_expired()
        if not perm.grants:
            console.print("[loom.muted]  No active scope grants.[/loom.muted]")
            if purged:
                console.print(f"[loom.muted]  ({purged} expired grant{'s' if purged != 1 else ''} removed)[/loom.muted]")
            return

        table = Table(title="Active Scope Grants", border_style="dim", show_lines=False)
        table.add_column("#", style="dim", width=3)
        table.add_column("Resource", style="cyan")
        table.add_column("Action", style="green")
        table.add_column("Selector")
        table.add_column("Source", style="dim")
        table.add_column("TTL", style="yellow")
        table.add_column("Constraints", style="dim")

        for i, g in enumerate(perm.grants):
            ttl_str = _format_ttl(g)
            constraints_str = ", ".join(
                f"{k}={v}" for k, v in g.constraints.items()
            ) if g.constraints else "-"
            table.add_row(
                str(i), g.resource, g.action,
                g.selector[:40], g.source, ttl_str, constraints_str,
            )
        console.print(table)
        if purged:
            console.print(f"[loom.muted]  ({purged} expired grant{'s' if purged != 1 else ''} removed)[/loom.muted]")
    else:
        # Delegate revoke/clear/help to shared core
        _scope_command_core(perm, args, lambda msg: console.print(f"[loom.muted]  {msg}[/loom.muted]"))


async def _handle_slash(cmd: str, session: "LoomSession") -> None:
    """Dispatch a slash command and print feedback."""
    parts = cmd.split(maxsplit=1)
    command = parts[0]
    arg = parts[1].strip() if len(parts) > 1 else ""

    if command == "/model":
        if not arg:
            providers = ", ".join(session.router.providers)
            console.print(
                f"[loom.muted]Current model: [bold]{session.model}[/bold]  "
                f"providers: {providers}[/loom.muted]\n"
                "[loom.muted]  MiniMax-*           requires MINIMAX_API_KEY in .env (Anthropic-compatible endpoint)[/loom.muted]\n"
                "[loom.muted]  claude-*            requires ANTHROPIC_API_KEY in .env[/loom.muted]\n"
                "[loom.muted]  gpt-*               requires OPENAI_API_KEY in .env (try `loom auth openai`)[/loom.muted]\n"
                "[loom.muted]  openai/<model>      explicit OpenAI prefix (e.g. openai/gpt-5.5)[/loom.muted]\n"
                "[loom.muted]  codex/<model>       Codex OAuth backend (e.g. codex/gpt-5.5; run `codex login`)[/loom.muted]\n"
                "[loom.muted]  openrouter/<v>/<m>  requires OPENROUTER_API_KEY in .env (e.g. openrouter/deepseek/deepseek-v4-pro)[/loom.muted]\n"
                "[loom.muted]  deepseek-*          requires DEEPSEEK_API_KEY in .env  (e.g. deepseek-v4-pro)[/loom.muted]\n"
                "[loom.muted]  ollama/<name>       enable [providers.ollama] in loom.toml[/loom.muted]\n"
                "[loom.muted]  lmstudio/<name>     enable [providers.lmstudio] in loom.toml[/loom.muted]"
            )
        else:
            ok = session.set_model(arg)
            if ok:
                console.print(f"[loom.muted]Model switched to: [bold]{arg}[/bold][/loom.muted]")
                # Footer was set once at startup (build_loom_app) and never
                # refreshed for runtime model switches — user saw the success
                # line above but the bottom badge kept showing the old name
                # until the next turn boundary's stat refresh.
                if (loom_app := getattr(session, "_loom_app", None)) is not None:
                    loom_app.footer.model = arg
                    loom_app.invalidate()
            else:
                console.print(
                    f"[loom.error]Could not switch to '{arg}'.[/loom.error] "
                    "[loom.muted]Either the prefix is not recognised, or the provider is not registered "
                    "(check API key in .env or enable in loom.toml).[/loom.muted]"
                )

    elif command == "/tier":
        # Issue #276: tier system manual control.
        if not session._tier_models:
            console.print(
                "[loom.muted]Tier system not configured. Add a [cognition.tiers] block "
                "to loom.toml to enable.[/loom.muted]"
            )
        elif not arg:
            # Status read-out
            active = session._active_tier()
            sticky = session._sticky_tier
            model = session._active_model()
            sticky_str = f"sticky on Tier {sticky}" if sticky is not None else "follows default"
            console.print(
                f"[loom.muted]Active: [bold]Tier {active}[/bold] · {model}  "
                f"({sticky_str}, {session._turns_at_current_tier} turns)[/loom.muted]"
            )
            for t in sorted(session._tier_models):
                marker = " ← active" if t == active else ""
                console.print(
                    f"[loom.muted]  Tier {t}: {session._tier_models[t]}{marker}[/loom.muted]"
                )
        else:
            try:
                target_tier = int(arg.split()[0])
            except (TypeError, ValueError):
                console.print(
                    f"[loom.error]Invalid tier '{arg}'.[/loom.error] "
                    "[loom.muted]Use ``/tier`` for status or ``/tier N`` to switch.[/loom.muted]"
                )
            else:
                if target_tier not in session._tier_models:
                    console.print(
                        f"[loom.error]Tier {target_tier} not configured.[/loom.error]"
                    )
                else:
                    new_sticky = None if target_tier == session._default_tier else target_tier
                    ev = session._set_sticky_tier(
                        new_sticky, reason="user /tier", source="user",
                    )
                    if ev is not None:
                        session._lifecycle_events.put_nowait(ev)
                    console.print(
                        f"[loom.muted]Tier → [bold]{session._active_tier()}[/bold] · "
                        f"{session._active_model()}[/loom.muted]"
                    )
                    if (loom_app := getattr(session, "_loom_app", None)) is not None:
                        loom_app.footer.model = session._active_model()
                        loom_app.invalidate()

    elif command == "/personality":
        if not arg:
            p = session.current_personality
            avail = session._stack.available_personalities()
            console.print(
                f"[loom.muted]Active: [bold]{p or '(none)'}[/bold]  "
                f"Available: {', '.join(avail) or '(none)'}[/loom.muted]"
            )
        elif arg == "off":
            session.switch_personality("off")
            console.print("[loom.muted]Personality cleared.[/loom.muted]")
        else:
            ok = session.switch_personality(arg)
            if ok:
                console.print(f"[loom.muted]Personality -> [bold]{arg}[/bold][/loom.muted]")
            else:
                avail = session._stack.available_personalities()
                console.print(
                    f"[loom.error]Unknown personality '{arg}'.[/loom.error] "
                    f"[loom.muted]Available: {', '.join(avail) or '(none)'}[/loom.muted]"
                )

    elif command == "/think":
        think = session._last_think
        if think:
            console.print(
                Panel(think, title="[loom.muted]Reasoning chain[/loom.muted]", border_style="dim")
            )
        else:
            console.print("[loom.muted]No reasoning chain captured for the last turn.[/loom.muted]")

    elif command == "/compact":
        pressure_str = session.budget.format_pressure()
        # PR-D4: surface compaction in footer BEFORE the inline message
        # so the spinner is visible from the very first frame; clear
        # immediately after _smart_compact returns so the footer
        # snaps back to normal without waiting for the next ticker
        loom_app = getattr(session, "_loom_app", None)
        if loom_app is not None:
            loom_app.footer.compacting = True
            loom_app.invalidate()
        harness.inline(f"compacting context ({pressure_str} used)…", level="info")
        try:
            await session._smart_compact()
        finally:
            if loom_app is not None:
                loom_app.footer.compacting = False
                loom_app.invalidate()
                # Update pressure% from the just-finished compaction so
                # the user sees the new context% immediately
                loom_app.footer.token_pct = session.budget.pressure * 100
                loom_app.invalidate()

    elif command == "/theme":
        args_part = parts[1] if len(parts) > 1 else ""
        active = load_preference()
        all_themes = available_themes()

        if not args_part.strip():
            # List available themes with current selection marked
            harness.inline("[bold]Available themes[/bold]", level="info")
            for name in all_themes:
                marker = "  ← currently active" if name == active else ""
                harness.inline(f"  {name}{marker}", level="info")
            return

        target = args_part.strip()
        if target not in all_themes:
            harness.inline(
                f"Unknown theme '{target}'. Available: {', '.join(all_themes)}",
                level="error",
            )
            return

        save_preference(target)
        harness.inline(
            f"Theme switched to [loom.accent]{target}[/loom.accent]. Restart to apply.",
            level="info",
        )

    elif command == "/sessions":
        # Issue #260: list recent sessions, let the user pick one by
        # number. Submitting an empty line is a no-op (stay in current
        # session). The actual switch is performed by the outer loop in
        # ``_resolve_and_chat`` after we set ``_cli_next_target`` and
        # exit the LoomApp; here we only persist the intent
        from loom.core.memory.session_log import SessionLog as _SL

        async with session._store.connect() as conn:
            rows = await _SL(conn).list_sessions(limit=20)

        if not rows:
            console.print("[loom.muted]No sessions found.[/loom.muted]")
            return

        console.print("[bold]Sessions[/bold]")
        for i, r in enumerate(rows, 1):
            sid = r.get("session_id", "")
            sid_short = sid[:8]
            title = r.get("title") or "(untitled)"
            last = (r.get("last_active") or "")[:16].replace("T", " ")
            turns = r.get("turn_count", 0)
            mark = (
                "[loom.accent]●[/loom.accent]"
                if sid == session.session_id
                else " "
            )
            console.print(
                f"  {mark} [loom.warning]{i:>2}[/loom.warning] "
                f"[loom.text]{title[:40]:<40}[/loom.text] "
                f"[loom.muted]{sid_short}  {last}  {turns}t[/loom.muted]"
            )
        console.print(
            "[loom.muted]  Enter a number to switch, "
            "[loom.warning]n[/loom.warning] for new session, or just press Enter to stay.[/loom.muted]"
        )

        loom_app = getattr(session, "_loom_app", None)
        if loom_app is None:
            # Non-interactive context (tests / scripts) — picker can't
            # take input. Tell the user where to look instead so they
            # don't just see the list and wonder what to do
            console.print(
                "[loom.warning]  Interactive picker unavailable in this "
                "context.[/loom.warning] [loom.muted]Use [/loom.muted]"
                "[loom.warning]loom sessions list[/loom.warning][loom.muted] / "
                "[/loom.muted][loom.warning]loom chat --session <id>[/loom.warning] "
                "[loom.muted]from the shell instead.[/loom.muted]"
            )
            return

        try:
            choice = (await loom_app.request_redirect_text()).strip().lower()
        except Exception:
            choice = ""

        if not choice:
            return
        if choice in ("n", "new"):
            session._cli_next_target = "__new__"  # type: ignore[attr-defined]
            return
        if choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(rows):
                target = rows[idx].get("session_id", "")
                if target and target != session.session_id:
                    session._cli_next_target = target  # type: ignore[attr-defined]
                else:
                    console.print("[loom.muted]  Already in this session.[/loom.muted]")
                return
        console.print(f"[loom.warning]  Invalid choice: {choice!r}[/loom.warning]")

    elif command == "/new":
        # Start a fresh session — outer loop picks up the sentinel and
        # restarts with no resume_session_id.
        session._cli_next_target = "__new__"  # type: ignore[attr-defined]

    elif command == "/name":
        # Issue #260: rename the current session in place. Title shows
        # up immediately in ``loom sessions list`` and on the next
        # ``/sessions`` picker open
        if not arg:
            console.print(
                "[loom.muted]Usage: [loom.warning]/name <title>[/loom.warning][/loom.muted]"
            )
        else:
            try:
                from loom.core.memory.session_log import SessionLog as _SL
                async with session._store.connect() as conn:
                    await _SL(conn).update_title(session.session_id, arg)
                console.print(
                    f"[loom.muted]  ✓ Session renamed → "
                    f"[loom.accent]{arg}[/loom.accent][/loom.muted]"
                )
            except Exception as exc:
                console.print(f"[loom.error]  Rename failed: {exc}[/loom.error]")

    elif command == "/stop":
        # In CLI the turn is a blocking await — the user can't type while it runs.
        # /stop typed before a turn starts is a no-op; the real interrupt is Ctrl+C.
        console.print(
            "[loom.muted]  /stop interrupts a running turn.  "
            "In CLI mode, press [loom.warning]Ctrl+C[/loom.warning] while the agent is responding.[/loom.muted]"
        )

    elif command == "/auto":
        if not session._strict_sandbox:
            console.print(
                "[loom.warning]  /auto requires strict_sandbox = true in loom.toml.[/loom.warning]\n"
                "[loom.muted]  Without workspace confinement, auto-approving run_bash "
                "would grant unrestricted shell access.[/loom.muted]"
            )
        else:
            session.perm.exec_auto = not session.perm.exec_auto
            state = "on" if session.perm.exec_auto else "off"
            if session.perm.exec_auto:
                console.print(
                    f"[loom.muted]Exec auto-approve: [loom.success]{state}[/loom.success] — "
                    "run_bash pre-authorized within workspace. "
                    "Absolute paths that escape the workspace still require confirmation.[/loom.muted]"
                )
            else:
                console.print(f"[loom.muted]Exec auto-approve: [loom.warning]{state}[/loom.warning] — run_bash will confirm every call.[/loom.muted]")

    elif command.startswith("/scope"):
        _scope_args = command[len("/scope"):].strip()
        _handle_scope_command(session, _scope_args, console)

    elif command == "/pause":
        # Toggle HITL mode (auto-pause after every tool batch)
        session.hitl_mode = not session.hitl_mode
        state = "on" if session.hitl_mode else "off"
        console.print(
            f"[loom.muted]HITL pause mode: [{'yellow' if session.hitl_mode else 'green'}]{state}"
            f"[/{'yellow' if session.hitl_mode else 'green'}][/loom.muted]"
        )
        if session.hitl_mode:
            console.print(
                "[loom.muted]  The agent will pause after each tool batch for your input.[/loom.muted]\n"
                "[loom.muted]  At pause> :  r(esume) · c(ancel) · <message>(redirect)[/loom.muted]"
            )

    elif command == "/help":
        console.print(
            Panel(
                "[bold]Session[/bold]\n\n"
                "  Start a new session:    [loom.warning]loom chat[/loom.warning]\n"
                "  Resume last session:    [loom.warning]loom chat --resume[/loom.warning]\n"
                "  Resume specific:        [loom.warning]loom chat --session <id>[/loom.warning]\n"
                "  List sessions:          [loom.warning]loom sessions list[/loom.warning]\n\n"
                "[bold]Slash commands[/bold]\n\n"
                "  [loom.warning]/new[/loom.warning]                       Start a fresh session\n"
                "  [loom.warning]/sessions[/loom.warning]                  List + switch sessions (numbered picker)\n"
                "  [loom.warning]/name[/loom.warning] [loom.muted]<title>[/loom.muted]             Rename the current session\n"
                "  [loom.warning]/model[/loom.warning]                     Show current model + registered providers\n"
                "  [loom.warning]/model[/loom.warning] [loom.muted]<name>[/loom.muted]              Switch model at runtime\n"
                "    [loom.muted]MiniMax-M2.7            → MiniMax via Anthropic SDK (MINIMAX_API_KEY)[/loom.muted]\n"
                "    [loom.muted]claude-sonnet-4-6       → Anthropic (ANTHROPIC_API_KEY)[/loom.muted]\n"
                "    [loom.muted]gpt-5.5 / gpt-5.5-pro   → OpenAI (OPENAI_API_KEY; run `loom auth openai`)[/loom.muted]\n"
                "    [loom.muted]codex/gpt-5.5           → Codex OAuth backend (run `codex login`)[/loom.muted]\n"
                "    [loom.muted]ollama/<model>          → local Ollama  (enable in loom.toml)[/loom.muted]\n"
                "    [loom.muted]lmstudio/<model>        → local LM Studio  (enable in loom.toml)[/loom.muted]\n"
                "  [loom.warning]/theme[/loom.warning]                    Show available CLI themes\n"
                "  [loom.warning]/theme[/loom.warning] [loom.muted]<name>[/loom.muted]              Switch CLI theme on next restart\n"
                "  [loom.warning]/personality[/loom.warning] [loom.muted]<name>[/loom.muted]      Switch cognitive persona\n"
                "  [loom.warning]/personality off[/loom.warning]           Remove active persona\n"
                "  [loom.warning]/tier[/loom.warning]                      Show active LLM tier + sticky state (#276)\n"
                "  [loom.warning]/tier[/loom.warning] [loom.muted]<N>[/loom.muted]                 Switch tier (1 = daily · 2 = deep reasoning)\n"
                "  [loom.warning]/think[/loom.warning]                     View last turn's reasoning chain\n"
                "  [loom.warning]/compact[/loom.warning]                   Compress older context\n"
                "  [loom.warning]/auto[/loom.warning]                      Toggle run_bash auto-approve (requires strict_sandbox)\n"
                "  [loom.warning]/scope[/loom.warning]                     List active scope grants (leases)\n"
                "  [loom.warning]/scope revoke <N>[/loom.warning]          Revoke a specific grant\n"
                "  [loom.warning]/scope clear[/loom.warning]               Revoke all non-system grants\n"
                "  [loom.warning]/pause[/loom.warning]                     Toggle HITL pause after each tool batch\n"
                "  [loom.warning]/stop[/loom.warning]                      Immediately cancel a running turn (CLI: use Ctrl+C)\n"
                "  [loom.warning]/help[/loom.warning]                      Show this message\n\n"
                "[bold]Keyboard shortcuts[/bold]\n\n"
                "  [loom.muted]Ctrl-L[/loom.muted]       Clear screen\n"
                "  [loom.muted]up / down[/loom.muted]    Browse input history\n"
                "  [loom.muted]Tab[/loom.muted]          Autocomplete slash commands\n"
                "  [loom.muted]exit / Ctrl-C[/loom.muted]  End session",
                title="[loom.warning] Loom — command reference [/loom.warning]",
                border_style="yellow",
            )
        )

    else:
        console.print(f"[loom.muted]Unknown command '{command}'. Type /help for help.[/loom.muted]")



def _start_tool_heartbeat(loom_app: Any, name: str, args: dict[str, Any]) -> None:
    """Resolve action-language label for ``name`` + ``args`` and push it
    into the footer heartbeat as TOOLING. Stale threshold falls out of
    ``resolve_tool_action`` (long-runners get 90s; everything else 30s).
    """
    from loom.platform.interaction_language import (
        HeartbeatState,
        resolve_tool_action,
    )

    action = resolve_tool_action(name, args)
    loom_app.start_heartbeat(
        state=HeartbeatState.TOOLING.value,
        label=action.label,
        subject=action.subject,
        stale_after_s=action.stale_after_s,
    )


async def _run_streaming_turn(session: "LoomSession", user_input: str) -> None:
    """
    Execute one streaming agent turn with real character-by-character output.

    Design rationale
    ----------------
    Rich Live rewrites the entire panel on every update — visually it looks
    like the response appears all at once, and its background render thread
    conflicts with blocking stdin reads (breaking tool-confirmation input).

    Instead we use plain console.print(chunk, end="") so each token appends
    in place, giving genuine streaming.  A Rule separator frames the response
    without the Live complexity.
    """
    console.print()
    t0 = time.monotonic()
    text_buffer = ""
    at_line_start = True
    active_tool: str | None = None
    spinner_task: asyncio.Task | None = None
    frame_index = 0

    def _cancel_spinner() -> None:
        nonlocal spinner_task
        if spinner_task and not spinner_task.done():
            spinner_task.cancel()
            spinner_task = None

    def _print_spinner() -> None:
        nonlocal frame_index
        clear_line()
        console.print(tool_running_line(active_tool or "", frame_index), end="")
        frame_index = (frame_index + 1) % 4

    async def _spin_loop() -> None:
        """Background task: animate spinner while tool is running."""
        nonlocal frame_index
        try:
            while True:
                await asyncio.sleep(0.1)
                _print_spinner()
        except asyncio.CancelledError:
            pass

    # Give the session a handle to cancel the spinner before confirm prompts.
    session._cancel_spinner_fn = _cancel_spinner

    # PR-D2 attempt 3 at the CJK truncation bug. Hypothesis: chunks
    # arriving from the LLM are split at arbitrary byte offsets — a
    # Chinese line of 7 wide chars is roughly 14 cells / 21 bytes,
    # arriving as 2-3 separate chunks. patch_stdout's StdoutProxy
    # buffers + flushes asynchronously via run_in_terminal; if the
    # bottom-area redraw fires *between* two chunks of the same line,
    # the redraw repositions cursor to col 0 of the line, and the
    # next chunk overwrites whatever was already there — eating the
    # opening characters of that line.
    #
    # Fix: buffer streaming chunks line-by-line, flush on newline. A
    # full line is written atomically, so the bottom-area redraw
    # cycle can't interleave inside it
    _stream_pending = ""

    def _flush_streaming(force: bool = False) -> None:
        """Drain the line buffer.

        Lines (text up to and including a ``\n``) are written
        atomically. With ``force=True`` (called at TurnDone) any
        partial trailing text is also flushed.
        """
        nonlocal _stream_pending
        import sys as _sys
        if not _stream_pending:
            return
        if "\n" in _stream_pending:
            idx = _stream_pending.rfind("\n")
            done = _stream_pending[: idx + 1]
            _stream_pending = _stream_pending[idx + 1:]
            _sys.stdout.write(done)
            _sys.stdout.flush()
        if force and _stream_pending:
            _sys.stdout.write(_stream_pending)
            _sys.stdout.flush()
            _stream_pending = ""

    # PR-E follow-up #248: markdown reblit at TurnDone.
    # ``_segment_buffer`` accumulates streamed text since the last
    # tool row (or turn start). At TurnDone we cursor-up that segment
    # and re-print it as Rich Markdown so **bold**, headings, code
    # blocks etc. become formatted instead of raw. Per-segment
    # tracking — not the whole turn — because tool rows printed
    # mid-turn are anchored content we cannot rewrite.
    _segment_buffer = ""

    def _terminal_width() -> int:
        loom_app = getattr(session, "_loom_app", None)
        if loom_app is not None:
            try:
                return max(20, loom_app.application.output.get_size().columns)
            except Exception:
                pass
        try:
            import os as _os
            return max(20, _os.get_terminal_size().columns)
        except Exception:
            return 80

    def _segment_visual_rows(text: str, width: int) -> int:
        """Rows the cursor advanced while writing ``text`` to a
        terminal of ``width`` cols. Accounts for soft-wrap on long
        lines and CJK/emoji double-width cells. Used to compute how
        many lines to cursor-up before re-rendering Markdown."""
        from wcwidth import wcwidth as _wcw
        row = 0
        col = 0
        for ch in text:
            if ch == "\n":
                row += 1
                col = 0
                continue
            w = _wcw(ch)
            if w < 0:
                w = 1
            if col + w > width:
                row += 1
                col = w
            else:
                col += w
                if col == width:
                    row += 1
                    col = 0
        return row

    async def _markdown_reblit() -> None:
        """Replace the last streamed text segment with Rich Markdown.

        Skips plain prose (no markdown markers) — reblitting pure text
        only causes visual flicker without adding signal. When the
        segment never produced visible rows (e.g. tool-only turn or
        ended exactly where it started), there's nothing to overwrite.
        """
        text = _segment_buffer
        if not text or not text.strip():
            return
        # Cheap markdown sniff — only reblit when there's something
        # to gain. Covers bold/italic/strikethrough, ATX headings,
        # code spans, fenced code, lists (-/*/+ and 1.), blockquotes
        # (incl. nested), links, tables. ``***`` (bold-italic) and
        # ``__`` (alt bold) are subsets of ``**``/``_`` so already
        # matched by those entries
        markers = (
            "**", "__", "_", "~~", "`", "```",
            "# ", "## ", "### ", "#### ",
            "- ", "* ", "+ ", "1. ", "2. ",
            "> ", "](", "| ",
        )
        if not any(m in text for m in markers):
            return

        width = _terminal_width()
        # Cursor at TurnDone always sits at col 0 of the row *below*
        # the last content row: either the segment's own trailing \n
        # advanced it there, or the ``if not at_line_start`` branch
        # in TurnDone printed an extra \n. So:
        #   ends with \n  → helper already counted the advance
        #   no trailing \n → helper undercount by 1 (the added \n)
        rows = _segment_visual_rows(text, width)
        if not text.endswith("\n"):
            rows += 1

        from rich.markdown import Markdown as _Markdown

        if rows <= 0:
            # Cursor is already at the start of where the segment
            # was; nothing to overwrite. ``text.strip()`` non-empty
            # combined with rows == 0 only happens for whitespace-
            # only segments which the early-return covered, so this
            # is effectively unreachable — guard anyway
            return

        def _reblit() -> None:
            import sys as _sys
            _sys.stdout.write(f"\r\033[{rows}A\033[J")
            _sys.stdout.flush()
            console.print(_Markdown(text.rstrip()))

        loom_app = getattr(session, "_loom_app", None)
        if loom_app is not None:
            await loom_app.print_above(_reblit)
        else:
            _reblit()

    # PR-E follow-up #246: envelope three-stage fade.
    # After ToolEnd we paint the row in **committed** style (the
    # default — green/red accent). 3s later, if no other content has
    # been printed below it, we cursor-up reblit it as **frozen**
    # (fully muted) so the visual weight sinks. ``_output_seq``
    # increments on every printable event and the freeze timer
    # rejects when its captured value no longer matches — that's how
    # we detect "something else got printed underneath".
    _output_seq = 0
    _freeze_task: asyncio.Task | None = None

    def _bump_output_seq() -> None:
        nonlocal _output_seq
        _output_seq += 1

    def _cancel_pending_freeze() -> None:
        nonlocal _freeze_task
        if _freeze_task and not _freeze_task.done():
            _freeze_task.cancel()
        _freeze_task = None

    async def _freeze_envelope(seq_at_schedule: int, name: str, success: bool,
                                duration_ms: float) -> None:
        """Sleep, then reblit the most-recent ToolEnd row as frozen.

        Bails if anything else printed in the meantime — only the
        envelope still anchored at the bottom can have its visual
        weight reduced via cursor-up.
        """
        try:
            await asyncio.sleep(3.0)
        except asyncio.CancelledError:
            return
        if _output_seq != seq_at_schedule:
            return

        def _rewrite() -> None:
            import sys as _sys
            # Layout above cursor: [tool_end row] [blank row] [cursor]
            # → up 2, clear forward, reprint frozen + blank
            _sys.stdout.write("\r\033[2A\033[J")
            _sys.stdout.flush()
            console.print(tool_end_line(name, success, duration_ms, frozen=True))
            console.print()

        loom_app = getattr(session, "_loom_app", None)
        try:
            if loom_app is not None:
                await loom_app.print_above(_rewrite)
            else:
                _rewrite()
        except Exception:
            # Don't let a cosmetic reblit failure poison the turn —
            # cursor manipulation can fail under terminal resize, etc.
            pass

    # Heartbeat (#418/#419) is THINKING from turn dispatch. We only
    # transition it OUT of THINKING when the agent actually produces
    # visible output (text or tool) — never on system-only boundary
    # events like CompressDone or TierExpiryHint, because the LLM
    # round-trip is still pending across those. ToolBegin / TurnPaused
    # / TurnDone branches drive their own heartbeat transitions, so
    # only TextChunk needs the explicit "first chunk wins" clear.
    _thinking_cleared = False

    def _clear_thinking_on_first_text() -> None:
        nonlocal _thinking_cleared
        if _thinking_cleared:
            return
        _thinking_cleared = True
        loom_app = getattr(session, "_loom_app", None)
        if loom_app is not None and loom_app.footer.heartbeat_state == "thinking":
            loom_app.stop_heartbeat()

    try:
        async for event in session.stream_turn(user_input):
            if isinstance(event, TextChunk):
                _clear_thinking_on_first_text()
                _bump_output_seq()
                _stream_pending += event.text
                text_buffer += event.text
                _segment_buffer += event.text
                _flush_streaming()
                at_line_start = (not _stream_pending) and event.text.endswith("\n")

            elif isinstance(event, ThinkCollapsed):
                # PR-D2: silent in CLI. Anthropic native thinking blocks
                # only become readable after ``stream.text_stream``
                # completes (session.py:1707), so the inline summary
                # always landed *below* the response — visually out of
                # order and crowding the input area. The full chain is
                # still stored on ``session._last_think`` and accessible
                # via ``/think``. Discord renders ThinkCollapsed in its
                # own way and isn't affected.
                pass

            elif isinstance(event, ReasoningContinuation):
                # Issue #271: drain any in-flight streamed text before
                # the indicator so it lands on its own line.
                _flush_streaming(force=True)
                console.print(
                    f"[dim]🤔 {event.display_text} "
                    f"(延伸 {event.attempt}/{event.max_attempts})[/dim]"
                )

            elif isinstance(event, TierChanged):
                # Issue #276: tier moved — refresh footer immediately, log
                # to console. The Discord path handles its own display
                # below; this is the plain CLI mirror.
                _flush_streaming(force=True)
                arrow = "⇪" if event.to_tier > event.from_tier else "⇩"
                console.print(
                    f"[dim]{arrow} Tier {event.from_tier} → {event.to_tier} · "
                    f"{event.to_model}  ({event.source}: {event.reason})[/dim]"
                )
                # Footer needs to track the new model NOW, not at next
                # turn boundary (same lesson as #275).
                if (loom_app := getattr(session, "_loom_app", None)) is not None:
                    loom_app.footer.model = event.to_model
                    loom_app.footer.tier = event.to_tier
                    loom_app.footer.tier_expiry_hint_active = False
                    loom_app.invalidate()
                    # Issue #284: don't dedup tier moves — each one is a
                    # rare, deliberate event the user wants to see.
                    loom_app.show_transient_hint(
                        f"{arrow} tier T{event.from_tier} → T{event.to_tier} ({event.to_model})",
                        severity="info",
                        duration_s=4.0,
                    )

            elif isinstance(event, CompressDone):
                # Issue #284: surface compaction as a transient hint
                # rather than a console line — CompressDone was
                # previously not displayed in CLI ("too noisy for
                # inline chat"); a 3-second flash is the right weight.
                #
                # Heartbeat is intentionally untouched here. ``stream_turn``
                # drains ``_pending_compactions`` before the LLM call
                # (session.py around line 1885), so CompressDone is
                # often the FIRST event of a turn with the LLM round-
                # trip still pending. Stopping the heartbeat here would
                # blank the footer for the rest of the turn even though
                # the agent is still working — codex review caught this
                # on PR #426. If the heartbeat happens to be in a stale
                # TOOLING from elsewhere, that's a bug to fix at the
                # source, not by laundering through CompressDone.
                if (loom_app := getattr(session, "_loom_app", None)) is not None:
                    loom_app.show_transient_hint(
                        f"🗜 compacted → {event.fact_count} facts",
                        severity="info",
                        duration_s=3.0,
                    )

            elif isinstance(event, TierExpiryHint):
                _flush_streaming(force=True)
                console.print(
                    f"[loom.warning]⏳ 已在 Tier {event.tier} 跑了 "
                    f"{event.turns_used} turns（閾值 {event.threshold}）。"
                    f"如果深度推理階段已結束，可考慮 /tier {1} 或讓絲絲 "
                    f"自行 request_model_tier(1).[/loom.warning]"
                )
                if (loom_app := getattr(session, "_loom_app", None)) is not None:
                    loom_app.footer.tier_expiry_hint_active = True
                    loom_app.invalidate()

            elif isinstance(event, ToolBegin):
                _bump_output_seq()
                # Drain pending streamed text before the tool row
                _flush_streaming(force=True)
                # Tool row anchors content we cannot rewrite; close
                # the current markdown-reblit segment so only post-
                # tool text gets reblit at TurnDone
                _segment_buffer = ""
                # New tool row prints below the prior committed
                # envelope — that prior one can no longer freeze
                _cancel_pending_freeze()
                # Cancel any running spinner
                _cancel_spinner()
                # Ensure tool rows start on a fresh line
                if not at_line_start:
                    console.print()
                    at_line_start = True
                active_tool = event.name
                frame_index = 0
                console.print(
                    tool_begin_line(event.name, event.args, width=_terminal_width())
                )
                # Start spinner animation
                spinner_task = asyncio.create_task(_spin_loop())
                # Footer heartbeat → TOOLING with action-language label
                # resolved from the tool name + args (#419).
                loom_app = getattr(session, "_loom_app", None)
                if loom_app is not None:
                    _start_tool_heartbeat(loom_app, event.name, event.args)

            elif isinstance(event, ToolEnd):
                _bump_output_seq()
                # Cancel spinner and clear its line
                _cancel_spinner()
                clear_line()
                console.print(
                    tool_end_line(event.name, event.success, event.duration_ms)
                )
                at_line_start = True
                active_tool = None
                console.print()
                # Schedule the freeze reblit. Any printable event
                # before the timer expires bumps _output_seq and
                # invalidates this scheduled freeze
                _cancel_pending_freeze()
                _freeze_task = asyncio.create_task(
                    _freeze_envelope(_output_seq, event.name,
                                     event.success, event.duration_ms)
                )
                # Footer heartbeat → idle; #419. Touch first so any
                # very-long tool that managed to land here still counts
                # as alive at the moment of completion (avoids the
                # observer-effect race where stop() raced the watchdog).
                loom_app = getattr(session, "_loom_app", None)
                if loom_app is not None:
                    loom_app.touch_heartbeat()
                    loom_app.stop_heartbeat()

            elif isinstance(event, TurnPaused):
                _bump_output_seq()
                _cancel_pending_freeze()
                # ── HITL pause (PR-A3: arrow-key widget) ──────────────────
                _cancel_spinner()
                clear_line()
                if not at_line_start:
                    console.print()
                console.print(
                    Rule(
                        f"[loom.warning]⏸  Paused[/loom.warning]  "
                        f"[loom.muted]({event.tool_count_so_far} tool(s) so far)[/loom.muted]",
                        style="loom.warning",
                    )
                )

                _PAUSE_RESUME = "resume"
                _PAUSE_CANCEL = "cancel"
                _PAUSE_REDIRECT = "redirect"

                # PR-D1: route through LoomApp's mode-flag widgets so the
                # pause + redirect overlays render inside the layout
                # (用過即焚, no scrollback residue) rather than spinning
                # nested Applications.
                loom_app = getattr(session, "_loom_app", None)
                # Heartbeat → PAUSED_BLOCKING while waiting on user; the
                # stalled-prefix is suppressed for this state so a long
                # think-time doesn't render as "still waiting".
                if loom_app is not None:
                    from loom.platform.interaction_language import HeartbeatState
                    loom_app.start_heartbeat(
                        state=HeartbeatState.PAUSED_BLOCKING.value,
                        label="等待你的決定",
                    )
                if loom_app is not None:
                    choice = await loom_app.request_pause(
                        title="Loom 已暫停，下一步？",
                        options=[
                            ("繼續執行剩下的工具", _PAUSE_RESUME,   "r"),
                            ("導向新指令並繼續",   _PAUSE_REDIRECT, "m"),
                            ("取消這個 turn",     _PAUSE_CANCEL,   "c"),
                        ],
                        default_index=0,
                        cancel_value=_PAUSE_CANCEL,
                    )
                else:
                    # Fallback for tests / scripts without a running app.
                    from loom.platform.cli.ui import SelectOption, select_prompt
                    async def _pause_pick():
                        return await select_prompt(
                            title="Loom 已暫停，下一步？",
                            options=[
                                SelectOption(label="繼續執行剩下的工具", value=_PAUSE_RESUME,   shortcut="r"),
                                SelectOption(label="導向新指令並繼續",   value=_PAUSE_REDIRECT, shortcut="m"),
                                SelectOption(label="取消這個 turn",     value=_PAUSE_CANCEL,   shortcut="c"),
                            ],
                            default_index=0,
                            cancel_value=_PAUSE_CANCEL,
                        )
                    runner = getattr(session, "_run_interactive", None)
                    choice = await (runner(_pause_pick) if runner else _pause_pick())

                # Decision made — release the PAUSED_BLOCKING heartbeat.
                # The next event (TextChunk / ToolBegin) will repaint it.
                if loom_app is not None:
                    loom_app.stop_heartbeat()

                if choice == _PAUSE_CANCEL:
                    session.cancel()
                elif choice == _PAUSE_REDIRECT:
                    if loom_app is not None:
                        raw = await loom_app.request_redirect_text()
                    else:
                        from prompt_toolkit import PromptSession as _PS
                        async def _ask_redirect():
                            ps = _PS()
                            try:
                                return await ps.prompt_async(
                                    [("class:prompt", "redirect › ")],
                                )
                            except (EOFError, KeyboardInterrupt):
                                return ""
                        runner = getattr(session, "_run_interactive", None)
                        raw = await (runner(_ask_redirect) if runner else _ask_redirect())
                    raw = (raw or "").strip()
                    if raw:
                        session.resume_with(raw)
                        console.print(f"[loom.muted]  Injected: {raw[:80]}[/loom.muted]")
                    else:
                        session.resume()
                else:  # _PAUSE_RESUME
                    session.resume()

            elif isinstance(event, TurnDone):
                _bump_output_seq()
                # Heartbeat → idle before any of the post-turn UI work
                # so the footer isn't claiming "still thinking" while we
                # write last-turn stats. The finally on the outer turn
                # loop is belt-and-suspenders.
                if (loom_app := getattr(session, "_loom_app", None)) is not None:
                    loom_app.stop_heartbeat()
                # Cancel any pending freeze before we touch the
                # cursor — markdown reblit will move it past the
                # frozen target row anyway
                _cancel_pending_freeze()
                # Drain any unterminated trailing chunk before
                # printing the post-turn UI
                _flush_streaming(force=True)
                # Cancel any running spinner and clear cursor
                _cancel_spinner()
                clear_line()
                if not at_line_start:
                    console.print()
                # PR-E follow-up #248: reblit the final text segment
                # as Rich Markdown. No-op if the turn ended on a tool
                # row, or if the segment is plain prose with no
                # markdown markers worth rendering
                await _markdown_reblit()
                cache_total = event.cache_read_input_tokens + event.cache_creation_input_tokens + event.input_tokens
                cache_hit_pct = (event.cache_read_input_tokens / cache_total * 100) if cache_total > 0 else 0.0
                elapsed = time.monotonic() - t0
                # PR-D1: route turn stats into the persistent footer
                # instead of printing an inline status_bar that scrolls
                # away after the next turn. When no LoomApp is wired
                # (tests / scripts) fall back to the inline print so
                # CLI ergonomics outside `loom chat` don't regress
                loom_app = getattr(session, "_loom_app", None)
                if loom_app is not None:
                    s = loom_app.footer
                    s.last_turn_cache_hit = cache_hit_pct
                    s.last_turn_input_tokens = event.input_tokens
                    s.last_turn_output_tokens = event.output_tokens
                    s.last_turn_elapsed_s = elapsed
                    s.last_turn_tool_count = event.tool_count
                    loom_app.invalidate()
                    # Issue #284: transient hints for cross-threshold
                    # signals at TurnDone. Dedup keys ensure each
                    # threshold fires once per session. Uses pressure
                    # (max of token / block) so the warning fires when
                    # the wire is about to clip, not just on token budget.
                    pct = session.budget.pressure * 100
                    if pct >= 80:
                        bound = " (blocks)" if session.budget.block_bound else ""
                        loom_app.show_transient_hint(
                            f"⚠️ context {pct:.0f}%{bound} — auto-compact soon",
                            severity="warn",
                            duration_s=4.0,
                            dedup_key="ctx_80",
                        )
                    turn_idx = getattr(session, "_turn_index", 0)
                    if turn_idx in {20, 50, 100}:
                        loom_app.show_transient_hint(
                            f"📍 turn {turn_idx} — 考慮 /summarize",
                            severity="info",
                            duration_s=3.0,
                            dedup_key=f"turn_{turn_idx}",
                        )
                else:
                    console.print(
                        status_bar(
                            context_fraction=session.budget.pressure,
                            input_tokens=event.input_tokens,
                            output_tokens=event.output_tokens,
                            elapsed_ms=elapsed * 1000,
                            tool_count=event.tool_count,
                            cache_hit_pct=cache_hit_pct,
                        )
                    )

    except asyncio.CancelledError:
        # PR-A2: turn was cancelled by user-initiated abort (Enter on
        # next message). Render a clean ABORTED marker and re-raise so
        # the caller's await sees the cancellation.
        _flush_streaming(force=True)
        _cancel_spinner()
        clear_line()
        console.print()
        console.print(
            Rule(
                "[loom.warning]⏸  ABORTED[/loom.warning]  [loom.muted]turn cut short by user[/loom.muted]",
                style="loom.warning",
            )
        )
        raise
    except Exception as exc:
        _flush_streaming(force=True)
        _cancel_spinner()
        clear_line()
        console.print()
        harness.inline(f"turn aborted with error: {exc}", level="error")
    finally:
        # PR-E follow-up #246: pending freeze must not outlive the turn —
        # the cursor relationship to the tool_end row breaks once any
        # post-turn UI prints (status, next prompt, etc.)
        _cancel_pending_freeze()
        # Defensive: ensure the spinner task never outlives this turn,
        # even if neither except branch fired (clean exit) or if a path
        # added later forgets to cancel it.
        _flush_streaming(force=True)
        _cancel_spinner()


# ---------------------------------------------------------------------------
# sessions commands
# ---------------------------------------------------------------------------


@cli.group()
def sessions() -> None:
    """Manage saved conversation sessions."""


@sessions.command("list")
@click.option("--db", default="~/.loom/memory.db", show_default=True)
    # ==============================================================
    # SECTION 4 — SESSION MGMT (sessions)
    # ==============================================================
@click.option("--limit", default=20, show_default=True)
def sessions_list(db: str, limit: int) -> None:
    """List recent sessions."""
    asyncio.run(_sessions_list(db, limit))


@sessions.command("show")
@click.argument("session_id")
@click.option("--db", default="~/.loom/memory.db", show_default=True)
def sessions_show(session_id: str, db: str) -> None:
    """Print full conversation replay for SESSION_ID."""
    asyncio.run(_sessions_show(session_id, db))


@sessions.command("rm")
@click.argument("session_id")
@click.option("--db", default="~/.loom/memory.db", show_default=True)
def sessions_rm(session_id: str, db: str) -> None:
    """Delete SESSION_ID and all its messages."""
    asyncio.run(_sessions_rm(session_id, db))


async def _sessions_list(db: str, limit: int) -> None:
    from rich.table import Table

    store = SQLiteStore(db)
    await store.initialize()
    async with store.connect() as conn:
        sl = SessionLog(conn)
        rows = await sl.list_sessions(limit)

    if not rows:
        console.print("[loom.muted]No sessions found.[/loom.muted]")
        return

    table = Table(title="Sessions", show_header=True, header_style="bold cyan")
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Title", max_width=44)
    table.add_column("Model", style="dim")
    table.add_column("Turns", justify="right")
    table.add_column("Last Active")
    for r in rows:
        table.add_row(
            r["session_id"],
            r["title"] or "[loom.muted](no title)[/loom.muted]",
            r["model"],
            str(r["turn_count"]),
            r["last_active"][:16].replace("T", " "),
        )
    console.print(table)


async def _sessions_show(session_id: str, db: str) -> None:
    store = SQLiteStore(db)
    await store.initialize()
    async with store.connect() as conn:
        sl = SessionLog(conn)
        meta = await sl.get_session(session_id)
        messages = await sl.load_messages(session_id)

    if meta is None:
        console.print(f"[loom.error]Session '{session_id}' not found.[/loom.error]")
        return

    console.print(Rule(f"[loom.accent]Session {session_id}[/loom.accent]"))
    console.print(
        f"[loom.muted]Model: {meta['model']}  |  "
        f"Turns: {meta['turn_count']}  |  "
        f"Started: {meta['started_at'][:16].replace('T', ' ')}[/loom.muted]"
    )
    console.print()

    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "") or ""
        if role == "user":
            console.print(f"[bold loom.warning]you>[/bold loom.warning] {content}")
        elif role == "assistant":
            if content:
                console.print(Markdown(content))
        elif role == "tool":
            console.print(f"[loom.muted]  [tool] {str(content)[:300]}[/loom.muted]")
        console.print()


async def _sessions_rm(session_id: str, db: str) -> None:
    store = SQLiteStore(db)
    await store.initialize()
    async with store.connect() as conn:
        sl = SessionLog(conn)
        meta = await sl.get_session(session_id)
        if meta is None:
            console.print(f"[loom.error]Session '{session_id}' not found.[/loom.error]")
            return
        await sl.delete_session(session_id)
    console.print(f"[loom.muted]Session [loom.accent]{session_id}[/loom.accent] deleted.[/loom.muted]")


# ---------------------------------------------------------------------------


@cli.group()
def auth() -> None:
    """Configure provider authentication."""


@auth.command("openai")
@click.option("--api-key", default=None, help="OpenAI API key to write to .env.")
@click.option("--skip-codex-login", is_flag=True, default=False,
              help="Do not offer to run the official Codex CLI login flow.")
@click.option("--env-file", type=click.Path(dir_okay=False, path_type=Path),
              default=None, help="Dotenv file to update (defaults to ./.env).")
def auth_openai(api_key: str | None, skip_codex_login: bool, env_file: Path | None) -> None:
    """Run Codex CLI login helper and/or store OPENAI_API_KEY for Loom."""
    from loom.core.cognition.openai_auth import load_codex_oauth_credential

    target = env_file or _project_env_path()
    console.print("[loom.muted]OpenAI setup for Loom[/loom.muted]")
    console.print(
        "[loom.muted]Run the official Codex CLI OAuth flow (`codex login`) "
        "as a user-friendly first step. Loom can use an unexpired Codex "
        "access token for OpenAI image generation, while OPENAI_API_KEY "
        "remains the portable fallback credential.[/loom.muted]"
    )

    if api_key is None and not skip_codex_login and click.confirm("Run `codex login` now?", default=True):
        try:
            result = subprocess.run(["codex", "login"], check=False)
        except FileNotFoundError:
            console.print(
                "[loom.warning]Codex CLI was not found on PATH. Install it or "
                "continue with an API key.[/loom.warning]"
            )
        else:
            if result.returncode == 0:
                console.print("[loom.muted]Codex CLI login completed.[/loom.muted]")
                codex_credential = load_codex_oauth_credential()
                if codex_credential is not None:
                    console.print(
                        "[loom.muted]Found an unexpired Codex OAuth access token "
                        "for Loom's experimental Codex credential path.[/loom.muted]"
                    )
            else:
                console.print(
                    f"[loom.warning]`codex login` exited with code "
                    f"{result.returncode}; continuing with API-key setup.[/loom.warning]"
                )

    existing = _load_env().get("OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if api_key is None:
        if existing and not click.confirm("OPENAI_API_KEY already exists. Replace it?", default=False):
            console.print("[loom.muted]Keeping existing OPENAI_API_KEY.[/loom.muted]")
            return
        if click.confirm("Store an OPENAI_API_KEY for Loom now?", default=not bool(existing)):
            api_key = click.prompt("OPENAI_API_KEY", hide_input=True).strip()

    if api_key:
        _set_env_value(target, "OPENAI_API_KEY", api_key.strip())
        console.print(f"[loom.muted]Saved OPENAI_API_KEY to {target}.[/loom.muted]")
        console.print("[loom.muted]Try: loom chat --model gpt-5.5[/loom.muted]")
    else:
        console.print(
            "[loom.muted]No API key saved. Codex CLI login is still useful for "
            "`codex`, but Loom needs OPENAI_API_KEY for OpenAI provider calls.[/loom.muted]"
        )


# ---------------------------------------------------------------------------


@cli.group()
def memory() -> None:
    """Inspect the memory store."""


@memory.command("list")
@click.option("--db", default="~/.loom/memory.db", show_default=True)
@click.option("--limit", default=20, show_default=True)
def memory_list(db: str, limit: int) -> None:
    # ==============================================================
    # SECTION 5 — MEMORY (memory, reflect)
    # ==============================================================
    """Show recent semantic memories."""
    asyncio.run(_memory_list(db, limit))


async def _memory_list(db: str, limit: int) -> None:
    store = SQLiteStore(db)
    await store.initialize()
    async with store.connect() as conn:
        sem = SemanticMemory(conn)
        entries = await sem.list_recent(limit)

    if not entries:
        console.print("[loom.muted]No semantic memories yet.[/loom.muted]")
        return

    console.print(Rule("[loom.accent]Semantic Memory[/loom.accent]"))
    for e in entries:
        c = "green" if e.confidence > 0.7 else "yellow" if e.confidence > 0.4 else "red"
        console.print(
            f"  [{c}]{e.confidence:.2f}[/{c}]  [loom.muted]{e.key}[/loom.muted]\n       {e.value}\n"
        )


# ---------------------------------------------------------------------------


@cli.command()
@click.option("--session", default=None, help="Session ID (latest if omitted)")
@click.option("--db", default="~/.loom/memory.db", show_default=True)
def reflect(session: str | None, db: str) -> None:
    """Show reflection report for a session."""
    asyncio.run(_reflect(session, db))


async def _reflect(session_id: str | None, db: str) -> None:
    from loom.core.memory.facade import MemoryFacade
    from loom.core.memory.search import MemorySearch

    store = SQLiteStore(db)
    await store.initialize()
    async with store.connect() as conn:
        ep = EpisodicMemory(conn)
        pr = ProceduralMemory(conn)
        sem = SemanticMemory(conn)
        rel = RelationalMemory(conn)
        facade = MemoryFacade(
            semantic=sem,
            procedural=pr,
            relational=rel,
            episodic=ep,
            search=MemorySearch(sem, pr),
        )
        api = ReflectionAPI(facade)

        if session_id is None:
            console.print("[loom.muted]No session ID given — showing skill health only.[/loom.muted]")
        else:
            summary = await api.session_summary(session_id)
            console.print(Panel(summary, title=f"[loom.accent]Session {session_id}[/loom.accent]"))

            rates = await api.tool_success_rate(session_id)
            if rates:
                console.print(Rule("Tool success rates"))
                for name, rate in sorted(rates.items()):
                    c = "green" if rate > 0.8 else "yellow" if rate > 0.5 else "red"
                    console.print(f"  [{c}]{rate:.0%}[/{c}]  {name}")

        skills = await api.skill_health_report()
        if skills:
            console.print(Rule("Skill health"))
            for s in skills:
                console.print(
                    f"  [loom.success]{s['confidence']:.2f}[/loom.success]  "
                    f"[bold]{s['name']}[/bold]  "
                    f"[loom.muted]used {s['usage_count']}×  "
                    f"tags: {s['tags']}[/loom.muted]"
                )


# ---------------------------------------------------------------------------
# loom diagnostic commands (Issue #120 PR 1)
# ---------------------------------------------------------------------------


@cli.group()
def diagnostic() -> None:
    """Inspect structured skill diagnostics (TaskReflector output)."""


@diagnostic.command("recent")
@click.option("--skill", default=None, metavar="NAME", help="Filter by skill name.")
@click.option("--limit", default=10, show_default=True, type=int)
@click.option("--db", default="~/.loom/memory.db", show_default=True)
def diagnostic_recent(skill: str | None, limit: int, db: str) -> None:
    """Show recent TaskDiagnostic entries from semantic memory."""
    # ==============================================================
    # SECTION 6 — DIAGNOSTICS (diagnostic)
    # ==============================================================
    asyncio.run(_diagnostic_recent(skill, limit, db))


async def _diagnostic_recent(skill: str | None, limit: int, db: str) -> None:
    from loom.core.cognition.task_reflector import TaskDiagnostic

    store = SQLiteStore(db)
    await store.initialize()
    async with store.connect() as conn:
        sem = SemanticMemory(conn)
        # Two key shapes:
        #   skill:<name>:diagnostic:<ts>     when --skill is given
        #   skill:                           otherwise (we filter :diagnostic: in-memory)
        if skill is not None:
            prefix = f"skill:{skill}:diagnostic:"
            entries = await sem.list_by_prefix(prefix, limit=limit)
        else:
            raw = await sem.list_by_prefix("skill:", limit=limit * 5)
            entries = [e for e in raw if ":diagnostic:" in e.key][:limit]

    if not entries:
        where = f" for skill '{skill}'" if skill else ""
        console.print(f"[loom.muted]No diagnostics found{where}.[/loom.muted]")
        return

    console.print(Rule("[loom.accent]Recent skill diagnostics[/loom.accent]"))
    for e in entries:
        try:
            diag = TaskDiagnostic.from_json(e.value)
        except Exception:
            console.print(f"  [loom.error]![/loom.error] [loom.muted]{e.key}[/loom.muted]  (unparseable)")
            continue

        score_color = (
            "green" if diag.quality_score >= 4.0
            else "yellow" if diag.quality_score >= 2.5
            else "red"
        )
        ts = diag.timestamp.strftime("%Y-%m-%d %H:%M")
        console.print(
            f"[loom.muted]{ts}[/loom.muted]  "
            f"[bold loom.accent]{diag.skill_name}[/bold loom.accent]  "
            f"[loom.muted]{diag.task_type}[/loom.muted]  "
            f"[{score_color}]{diag.quality_score:.1f}[/{score_color}]"
        )
        if diag.instructions_violated:
            for v in diag.instructions_violated[:3]:
                console.print(f"   [loom.error]✗[/loom.error] {v}")
        if diag.skill_edit_suggestions:
            console.print("   [bold]→ suggestions:[/bold]")
            for s in diag.skill_edit_suggestions[:3]:
                console.print(f"     • {s}")
        console.print()


# ---------------------------------------------------------------------------
# loom skill commands (Issue #120 PR 2)
# ---------------------------------------------------------------------------


@cli.group()
def skill() -> None:
    """Run skill review utilities."""


# ---------------------------------------------------------------------------
# loom skill weekly — doc/54 §4.2 / §5 P0-6 weekly worker
# ---------------------------------------------------------------------------


@skill.command("weekly")
@click.option("--days", default=7, show_default=True, type=int,
              help="Window size in days.")
@click.option("--output-dir", default="outputs/self_check",
              show_default=True, type=click.Path(),
              help="Where to write the markdown report (omit --no-write to skip).")
@click.option("--no-write", is_flag=True, default=False,
              help="Print to stdout instead of writing a file.")
def skill_weekly(days: int, output_dir: str, no_write: bool) -> None:
    """Run the weekly skill review worker.

    Pure ledger query + structural analysis — no LLM. Produces a markdown
    report listing per-skill activity and a "該關注清單" of structural
    observations (doc/54 §4.4).
    """
    asyncio.run(_skill_weekly(days, output_dir, no_write))


async def _skill_weekly(days: int, output_dir: str, no_write: bool) -> None:
    from loom.core.ledger import LedgerStore
    from loom.core.skill_review import generate_weekly_report

    store = LedgerStore()
    await store.open()
    try:
        workspace = Path.cwd()
        skills_roots = [
            workspace / "skills",
            Path.home() / ".loom" / "skills",
        ]
        report = await generate_weekly_report(
            store,
            skills_roots=skills_roots,
            output_dir=None if no_write else Path(output_dir),
            window_days=days,
            write_to_disk=not no_write,
        )
    finally:
        await store.close()

    if no_write:
        console.print(report.markdown)
    else:
        console.print(
            f"Weekly skill review written to: [bold]{report.output_path}[/bold]\n"
            f"Skills seen: {len(report.skills_seen)} | "
            f"On disk: {len(report.skills_on_disk)} | "
            f"Attention items: {len(report.attention)}"
        )

# loom import command
# ---------------------------------------------------------------------------


@cli.command("import")
@click.argument("file", type=click.Path(exists=True, dir_okay=False))
@click.option(
    "--lens",
    default=None,
    metavar="NAME",
    help="Force a specific lens (hermes, openai_tools). Auto-detected if omitted.",
)
@click.option(
    "--min-confidence",
    default=0.5,
    show_default=True,
    type=float,
    help="Minimum confidence for skill import (0.0–1.0).",
)
@click.option("--db", default="~/.loom/memory.db", show_default=True)
@click.option("--dry-run", is_flag=True, default=False, help="Show decisions without writing.")
def import_cmd(
    file: str, lens: str | None, min_confidence: float, db: str, dry_run: bool
) -> None:
    """Import skills or tools from a JSON file using a Lens."""
    asyncio.run(_import(file, lens, min_confidence, db, dry_run))


async def _import(
    file: str,
    lens_name: str | None,
    min_confidence: float,
    db: str,
    dry_run: bool,
) -> None:
    # ==============================================================
    # SECTION 8 — AUTONOMY (autonomy)
    # ==============================================================
    import json as _json
    from loom.extensibility import (
        LensRegistry, HermesLens, OpenAIToolsLens,
        SkillImportPipeline,
    )
    from loom.extensibility.adapter import AdapterRegistry

    # Build registry with all built-in lenses
    lens_registry = LensRegistry()
    lens_registry.register(HermesLens())
    lens_registry.register(OpenAIToolsLens())

    # Load file
    raw_path = Path(file).expanduser().resolve()
    try:
        source = _json.loads(raw_path.read_text(encoding="utf-8"))
    except Exception as exc:
        console.print(f"[loom.error]Could not read '{raw_path}': {exc}[/loom.error]")
        return

    # Extract via lens
    result = lens_registry.extract(source, lens_name=lens_name)
    if result is None:
        avail = ", ".join(lens_registry.registered_names) or "(none)"
        console.print(
            f"[loom.error]No lens matched this file.[/loom.error] "
            f"[loom.muted]Available: {avail}. Use --lens to specify one.[/loom.muted]"
        )
        return

    console.print(f"[loom.muted]Lens:[/loom.muted] [loom.accent]{result.source}[/loom.accent]  "
                  f"[loom.muted]File:[/loom.muted] {raw_path.name}")

    if result.warnings:
        for w in result.warnings:
            console.print(f"  [loom.warning]⚠[/loom.warning]  {w}")

    if result.is_empty:
        console.print("[loom.muted]Nothing to import.[/loom.muted]")
        return

    store = SQLiteStore(db)
    async with store.connect() as conn:
        from loom.core.memory.procedural import ProceduralMemory

        # ── Skills ──────────────────────────────────────────────────────────
        if result.skills:
            procedural = ProceduralMemory(conn)
            pipeline = SkillImportPipeline(procedural, min_confidence=min_confidence)
            decisions = await pipeline.process(result.skills)

            console.print(f"\n[bold]Skills[/bold] ({len(decisions)} evaluated)")
            approved = [d for d in decisions if d.approved]
            rejected = [d for d in decisions if not d.approved]

            for d in approved:
                marker = "[loom.muted](dry-run)[/loom.muted]" if dry_run else "[loom.success]✓[/loom.success]"
                console.print(
                    f"  {marker} [loom.accent]{d.skill_name}[/loom.accent]  "
                    f"[loom.muted]conf={d.adjusted_confidence:.2f}[/loom.muted]"
                )
            for d in rejected:
                console.print(
                    f"  [loom.muted]✗[/loom.muted] [loom.muted]{d.skill_name}[/loom.muted]  "
                    f"[loom.error]{d.reason}[/loom.error]"
                )

            if not dry_run and approved:
                count = await pipeline.import_approved(decisions, result.skills)
                console.print(
                    f"\n  [loom.success]{count} skill(s) written to ProceduralMemory.[/loom.success]"
                )

        # ── Tool adapters ────────────────────────────────────────────────────
        if result.platform_adapters:
            console.print(f"\n[bold]Tool adapters[/bold] ({len(result.platform_adapters)} found)")
            for a in result.platform_adapters:
                trust_color = {"safe": "green", "guarded": "yellow", "critical": "red"}.get(
                    a.get("trust_level", "safe"), "white"
                )
                console.print(
                    f"  [loom.muted]·[/loom.muted] [loom.accent]{a['name']}[/loom.accent]  "
                    f"[{trust_color}]{a.get('trust_level', 'safe').upper()}[/{trust_color}]  "
                    f"[loom.muted]{a.get('description', '')[:60]}[/loom.muted]"
                )
            if dry_run:
                console.print(
                    "  [loom.muted](dry-run) Adapters listed but not installed into any session.[/loom.muted]"
                )
            else:
                console.print(
                    "  [loom.muted]Adapters listed. Use AdapterRegistry.from_lens_result() "
                    "in code, or place tools in loom_tools.py for auto-loading.[/loom.muted]"
                )

        # ── Middleware patterns (informational) ──────────────────────────────
        if result.middleware_patterns:
            console.print(
                f"\n[bold]Middleware patterns[/bold] "
                f"[loom.muted](informational — not imported)[/loom.muted]"
            )
            for m in result.middleware_patterns:
                console.print(f"  [loom.muted]·[/loom.muted] {m['name']}  {m.get('description', '')[:60]}")


# ---------------------------------------------------------------------------
# loom autonomy commands
# ---------------------------------------------------------------------------


@cli.group()
def autonomy() -> None:
    """Manage the autonomous action engine."""


@autonomy.command("start")
@click.option(
    "--config", default="loom.toml", show_default=True, help="Path to loom.toml"
)
@click.option("--model", default=None, show_default=True)
@click.option("--db", default="~/.loom/memory.db", show_default=True)
@click.option(
    "--interval", default=60, show_default=True, help="Poll interval in seconds"
)
def autonomy_start(config: str, model: str, db: str, interval: int) -> None:
    """Start the autonomy daemon (foreground)."""
    asyncio.run(_autonomy_start(config, model, db, interval))


async def _autonomy_start(config: str, model: str, db: str, interval: int) -> None:
    from loom.autonomy.daemon import AutonomyDaemon
    from loom.notify.adapters.cli import CLINotifier
    from loom.notify.confirm import ConfirmFlow
    from loom.notify.router import NotificationRouter

    notifier = CLINotifier(console)
    notify_router = NotificationRouter()
    notify_router.register(notifier)

    # Auto-register Discord if DISCORD_WEBHOOK_URL is set in env or loom.toml
    env = _load_env()
    loom_cfg = _load_loom_config()
    discord_url = (
        env.get("DISCORD_WEBHOOK_URL")
        or os.environ.get("DISCORD_WEBHOOK_URL", "")
        or loom_cfg.get("notify", {}).get("discord", {}).get("webhook_url", "")
    )
    if discord_url:
        from loom.notify.adapters.discord import DiscordNotifier
        rest_api_url = (
            loom_cfg.get("notify", {}).get("discord", {}).get("rest_api_url")
            or env.get("LOOM_API_URL", "")
        )
        discord_notifier = DiscordNotifier(
            webhook_url=discord_url,
            username=loom_cfg.get("notify", {}).get("discord", {}).get("username", "Loom Agent"),
            rest_api_url=rest_api_url or None,
        )
        notify_router.register(discord_notifier)
        console.print(f"[loom.muted]  Discord notifier registered.[/loom.muted]")

    confirm_flow = ConfirmFlow(
        send_fn=notify_router.send,
        wait_fn=notifier.wait_reply,
    )

    session = LoomSession(model=model, db_path=db)
    await session.start()

    daemon = AutonomyDaemon(
        notify_router=notify_router,
        confirm_flow=confirm_flow,
        loom_session=session,
    )
    import logging
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")

    n = daemon.load_config(config)
    console.print(
        Panel(
            f"[bold loom.accent]Loom Autonomy Daemon[/bold loom.accent]\n"
            f"Loaded [loom.success]{n}[/loom.success] trigger(s) from [loom.muted]{config}[/loom.muted]\n"
            f"Poll interval: {interval}s  |  model: {model}\n"
            f"[loom.muted]Press Ctrl-C to stop.[/loom.muted]",
            border_style="cyan",
        )
    )

    try:
        await daemon.start(poll_interval=float(interval))
    except KeyboardInterrupt:
        pass
    finally:
        await session.stop()
        console.print("[loom.muted]Autonomy daemon stopped.[/loom.muted]")


@autonomy.command("status")
@click.option("--config", default="loom.toml", show_default=True)
def autonomy_status(config: str) -> None:
    """Show registered triggers from loom.toml."""
    from loom.autonomy.daemon import AutonomyDaemon
    from loom.notify.router import NotificationRouter
    from loom.notify.confirm import ConfirmFlow

    daemon = AutonomyDaemon(
        notify_router=NotificationRouter(),
        confirm_flow=ConfirmFlow(send_fn=lambda n: asyncio.coroutine(lambda: None)()),
    )
    n = daemon.load_config(config)
    triggers = daemon.registered_triggers()

    console.print(Rule("[loom.accent]Registered Triggers[/loom.accent]"))
    if not triggers:
        console.print(
            "[loom.muted]No triggers found (check autonomy.enabled in loom.toml)[/loom.muted]"
        )
        return

    for t in triggers:
        color = "green" if t["enabled"] else "dim"
        console.print(
            f"  [{color}]{t['name']}[/{color}]  "
            f"[loom.muted]{t['kind']}[/loom.muted]  "
            f"trust=[loom.warning]{t['trust_level']}[/loom.warning]\n"
            f"    {t['intent']}\n"
        )


@autonomy.command("emit")
@click.argument("event_name")
@click.option("--config", default="loom.toml", show_default=True)
@click.option("--model", default=None, show_default=True)
@click.option("--db", default="~/.loom/memory.db", show_default=True)
def autonomy_emit(event_name: str, config: str, model: str, db: str) -> None:
    """Manually emit an event to trigger matching EventTriggers."""
    asyncio.run(_autonomy_emit(event_name, config, model, db))


async def _autonomy_emit(event_name: str, config: str, model: str, db: str) -> None:
    from loom.autonomy.daemon import AutonomyDaemon
    from loom.notify.adapters.cli import CLINotifier
    from loom.notify.confirm import ConfirmFlow
    from loom.notify.router import NotificationRouter

    notifier = CLINotifier(console)
    notify_router = NotificationRouter()
    notify_router.register(notifier)
    confirm_flow = ConfirmFlow(
        send_fn=notify_router.send,
        wait_fn=notifier.wait_reply,
    )

    session = LoomSession(model=model, db_path=db)
    await session.start()

    daemon = AutonomyDaemon(
        notify_router=notify_router,
        confirm_flow=confirm_flow,
        loom_session=session,
    )
    daemon.load_config(config)
    fired = await daemon.evaluator.emit(event_name)
    console.print(
        f"[loom.accent]Emitted[/loom.accent] '{event_name}' → fired triggers: {fired or ['(none)']}"
    )
    await session.stop()


# ---------------------------------------------------------------------------
# loom api commands
# ---------------------------------------------------------------------------


@cli.group()
def api() -> None:
    """REST API server for memory and autonomy."""


@api.command("start")
@click.option("--host", default="0.0.0.0", show_default=True)
@click.option("--port", default=8000, show_default=True)
@click.option("--db", default="~/.loom/memory.db", show_default=True)
@click.option("--reload", is_flag=True, default=False, help="Auto-reload on code changes (dev)")
def api_start(host: str, port: int, db: str, reload: bool) -> None:
    """Start the Loom REST API server (requires: pip install loom[api])."""
    try:
        from loom.platform.api.server import run_server
    except ImportError:
        console.print(
            "[loom.error]FastAPI not installed.[/loom.error] "
    # ==============================================================
    # SECTION 9 — API / DISCORD / MCP (api, discord_bot, mcp_cmd)
    # ==============================================================
            "Run:  [bold]pip install loom[api][/bold]"
        )
        raise SystemExit(1)
    console.print(
        f"[bold loom.accent]Loom API[/bold loom.accent]  "
        f"http://{host}:{port}  |  db: {db}\n"
        f"[loom.muted]Docs: http://{host}:{port}/docs[/loom.muted]"
    )
    run_server(host=host, port=port, db_path=db, reload=reload)


# ---------------------------------------------------------------------------
# Discord bot platform
# ---------------------------------------------------------------------------

@cli.group(name="discord")
def discord_bot() -> None:
    """Discord bot frontend for Loom."""


@discord_bot.command("start")
@click.option("--token", envvar="DISCORD_BOT_TOKEN", default="",
              help="Discord bot token (or set DISCORD_BOT_TOKEN in .env)")
@click.option("--channel", "channel_ids", multiple=True, type=int,
              help="Channel ID(s) to listen in (or set DISCORD_CHANNEL_ID in .env).")
@click.option("--user", "user_ids", multiple=True, type=int,
              help="User ID(s) to accept messages from (or set DISCORD_USER_ID in .env).")
@click.option("--model", default=None, show_default=True)
@click.option("--db", default="~/.loom/memory.db", show_default=True)
@click.option("--autonomy/--no-autonomy", default=False,
              help="Also start the autonomy daemon in the same process.")
@click.option("--autonomy-config", default="loom.toml", show_default=True,
              help="Path to loom.toml for autonomy trigger definitions.")
@click.option("--autonomy-interval", default=60, show_default=True, type=int,
              help="Autonomy daemon poll interval in seconds.")
@click.option("--notify-channel", "notify_channel_id", default=0, type=int,
              help="Discord channel ID for autonomy notifications. "
                   "Defaults to the first --channel value.")
def discord_start(
    token: str,
    channel_ids: tuple[int, ...],
    user_ids: tuple[int, ...],
    model: str,
    db: str,
    autonomy: bool,
    autonomy_config: str,
    autonomy_interval: int,
    notify_channel_id: int,
) -> None:
    """Start the Loom Discord bot (requires: pip install loom[discord]).

    Use --autonomy to also run the autonomy cron daemon in the same process,
    routing trigger results and confirmations through Discord.
    """
    try:
        from loom.platform.discord.bot import LoomDiscordBot
    except ImportError:
        console.print(
            "[loom.error]discord.py not installed.[/loom.error] "
            "Run:  [bold]pip install loom[discord][/bold]"
        )
        raise SystemExit(1)

    env = _load_env()

    resolved_token = token or env.get("DISCORD_BOT_TOKEN", "")
    if not resolved_token:
        console.print("[loom.error]No Discord bot token.[/loom.error] Set --token or DISCORD_BOT_TOKEN in .env")
        raise SystemExit(1)

    def _parse_ids(cli_ids: tuple[int, ...], env_key: str) -> list[int]:
        if cli_ids:
            return list(cli_ids)
        raw = env.get(env_key, "").strip()
        if raw:
            try:
                return [int(raw)]
            except ValueError:
                pass
        return []

    channel_list = _parse_ids(channel_ids, "DISCORD_CHANNEL_ID")
    user_list    = _parse_ids(user_ids,    "DISCORD_USER_ID")

    bot = LoomDiscordBot(
        model=model,
        db_path=db,
        channel_ids=channel_list or None,
        allowed_user_ids=user_list or None,
    )

    info_lines = [f"[bold loom.accent]Loom Discord Bot[/bold loom.accent]  model: {model}  |  db: {db}"]
    if channel_list:
        info_lines.append(f"[loom.muted]  Channel:  {channel_list}[/loom.muted]")
    else:
        info_lines.append("[loom.muted]  Channels: @mentions everywhere[/loom.muted]")
    if user_list:
        info_lines.append(f"[loom.muted]  Users:    {user_list}[/loom.muted]")
    else:
        info_lines.append("[loom.muted]  Users:    unrestricted[/loom.muted]")

    if autonomy:
        # Resolve the notification channel: explicit flag > first bot channel > error
        resolved_notify_ch = notify_channel_id or (channel_list[0] if channel_list else 0)
        if not resolved_notify_ch:
            console.print(
                "[loom.error]--autonomy requires a target channel.[/loom.error] "
                "Pass --channel <id> or --notify-channel <id>."
            )
            raise SystemExit(1)
        info_lines.append(
            f"[loom.muted]  Autonomy: [loom.success]on[/loom.success]  "
            f"config={autonomy_config}  notify-channel={resolved_notify_ch}[/loom.muted]"
        )
        console.print("\n".join(info_lines))
        asyncio.run(
            _discord_with_autonomy(
                bot, resolved_token, autonomy_config, model, db,
                resolved_notify_ch, autonomy_interval,
            )
        )
    else:
        console.print("\n".join(info_lines))
        asyncio.run(_discord_graceful_run(bot, resolved_token))


async def _discord_graceful_run(bot: "LoomDiscordBot", token: str) -> None:
    """Run the Discord bot and close all thread sessions on shutdown."""
    try:
        async with bot._client:
            await bot._client.start(token)
    finally:
        for tid in list(bot._sessions):
            await bot._close_session(tid)


async def _discord_with_autonomy(
    bot: "LoomDiscordBot",
    token: str,
    config_path: str,
    model: str,
    db: str,
    notify_channel_id: int,
    interval: int,
) -> None:
    """Run Discord bot + autonomy daemon in a single event loop."""
    from loom.autonomy.daemon import AutonomyDaemon
    from loom.notify.adapters.discord_bot import DiscordBotNotifier
    from loom.notify.confirm import ConfirmFlow
    from loom.notify.router import NotificationRouter

    discord_notifier = DiscordBotNotifier(bot._client, notify_channel_id)
    notify_router = NotificationRouter()
    notify_router.register(discord_notifier)

    confirm_flow = ConfirmFlow(
        send_fn=notify_router.send,
        wait_fn=discord_notifier.wait_reply,
    )

    # Autonomous session: separate from Discord thread sessions, shared db
    session = LoomSession(model=model, db_path=db)
    await session.start()

    # Patch autonomy session's confirm → Discord notify channel button,
    # same as thread sessions. Without this, GUARDED tool confirmations
    # fall through to the CLI prompt (Allow? [y/N]:) on shutdown.
    from loom.core.harness.middleware import BlastRadiusMiddleware as _BRM
    _confirm_fn = bot._make_confirm_fn(notify_channel_id)
    for _mw in session._pipeline._middlewares:
        if isinstance(_mw, _BRM):
            _mw._confirm = _confirm_fn
            break
    # Also patch skill check approval so it uses Discord confirm buttons
    session._confirm_fn = _confirm_fn

    daemon = AutonomyDaemon(
        notify_router=notify_router,
        confirm_flow=confirm_flow,
        loom_session=session,
        chime_delivery=bot.deliver_chime,
    )
    import logging
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")

    n = daemon.load_config(config_path)
    console.print(f"[loom.muted]Autonomy: {n} trigger(s) loaded from {config_path}[/loom.muted]")

    _background_tasks: set[asyncio.Task] = set()  # strong refs prevent GC

    async def _start_daemon_after_ready() -> None:
        # Wait for the Discord connection before the daemon begins polling,
        # so notifications can be delivered from the first fire onwards.
        await bot._client.wait_until_ready()
        console.print("[loom.muted]Autonomy daemon started.[/loom.muted]")
        _t = asyncio.ensure_future(daemon.start(poll_interval=float(interval)))
        _background_tasks.add(_t)
        _t.add_done_callback(_background_tasks.discard)

    try:
        async with bot._client:
            _t = asyncio.ensure_future(_start_daemon_after_ready())
            _background_tasks.add(_t)
            _t.add_done_callback(_background_tasks.discard)
            await bot._client.start(token)
    finally:
        for tid in list(bot._sessions):
            await bot._close_session(tid)
        await session.stop()  # autonomy session


# ---------------------------------------------------------------------------
# MCP (Model Context Protocol) — Issue #9
# ---------------------------------------------------------------------------

@cli.group(name="mcp")
def mcp_cmd() -> None:
    """MCP (Model Context Protocol) server and client commands."""


@mcp_cmd.command("serve")
@click.option("--db", default="~/.loom/memory.db", show_default=True,
              help="Path to Loom's memory database.")
@click.option("--model", default=None, show_default=True,
              help="Model used when starting the session.")
def mcp_serve(db: str, model: str) -> None:
    """Start Loom as an MCP stdio server.

    Exposes all SAFE (and optionally GUARDED) Loom tools to any MCP-compatible
    client such as Claude Desktop, Cursor, or Continue.

    Add to claude_desktop_config.json:

    \b
        {
          "mcpServers": {
            "loom": {
              "command": "loom",
              "args": ["mcp", "serve"],
              "env": {}
            }
          }
        }
    """
    try:
        from loom.extensibility.mcp_server import run_mcp_server
    except ImportError:
        console.print(
            "[loom.error]MCP SDK not installed.[/loom.error] "
            "Run: [bold]pip install 'loom[mcp]'[/bold]"
        )
        raise SystemExit(1)

    async def _run() -> None:
        session = LoomSession(model=model, db_path=db)
        await session.start()
        try:
            await run_mcp_server(
                session.registry,
                pipeline=session._pipeline,
                session_id=session.session_id,
            )
        finally:
            await session.stop()

    asyncio.run(_run())


@mcp_cmd.command("connect")
@click.argument("server_spec")
@click.option("--trust", default="safe", show_default=True,
              type=click.Choice(["safe", "guarded"], case_sensitive=False),
              help="Trust level for imported tools.")
@click.option("--db", default="~/.loom/memory.db", show_default=True)
@click.option("--model", default=None, show_default=True)
def mcp_connect(server_spec: str, trust: str, db: str, model: str) -> None:
    """Connect to an external MCP server and list its available tools.

    SERVER_SPEC is a command to start the MCP server process, e.g.:

    \b
        loom mcp connect "npx -y @modelcontextprotocol/server-filesystem /tmp"
        loom mcp connect "uvx mcp-server-git"
        loom mcp connect "python -m my_mcp_server"
    """
    try:
        from loom.extensibility.mcp_client import LoomMCPClient, MCPServerConfig
    except ImportError:
        console.print(
            "[loom.error]MCP SDK not installed.[/loom.error] "
            "Run: [bold]pip install 'loom[mcp]'[/bold]"
        )
        raise SystemExit(1)

    parts = server_spec.split()
    command = parts[0]
    args = parts[1:]

    cfg = MCPServerConfig(
        name="remote",
        command=command,
        args=args,
        trust_level=trust,
    )

    async def _run() -> None:
        client = LoomMCPClient(cfg)
        try:
            tools = await client.connect_and_list_tools()
        except Exception as exc:
            console.print(f"[loom.error]Failed to connect:[/loom.error] {exc}")
            raise SystemExit(1)
        finally:
            await client.disconnect()

        if not tools:
            console.print("[loom.warning]No tools found on this MCP server.[/loom.warning]")
            return

        console.print(
            f"[bold loom.accent]{len(tools)} tool(s)[/bold loom.accent] available from "
            f"[bold]{server_spec}[/bold]:\n"
        )
        for t in tools:
            desc = t.description or "(no description)"
            console.print(f"  [loom.success]{t.name}[/loom.success]  [loom.muted]{desc[:80]}[/loom.muted]")
        console.print(
            "\n[loom.muted]Add this server to loom.toml [[mcp.servers]] "
            "to load it automatically:[/loom.muted]"
        )
        console.print(
            f"\n  [loom.muted][[mcp.servers]]\n"
            f"  name    = \"remote\"\n"
            f"  command = \"{command}\"\n"
            f"  args    = {json.dumps(args)}\n"
            f"  trust_level = \"{trust}\"[/loom.muted]"
        )

    asyncio.run(_run())


if __name__ == "__main__":
    cli()
