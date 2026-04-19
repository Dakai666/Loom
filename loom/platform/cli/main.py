"""
Loom CLI — thin platform wrapper.

``LoomSession`` and all session logic live in ``loom.core.session``.
This module provides the click command group, TUI integration, and
Rich-rendered streaming output that are specific to the terminal interface.

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
from loom.platform.cli.ui import (
    ActionRolledBack,
    ActionStateChange,
    CompressDone,
    TextChunk,
    ThinkCollapsed,
    ToolBegin,
    ToolEnd,
    TurnDone,
    TurnDropped,
    TurnPaused,
    clear_line,
    make_prompt_session,
    render_cursor,
    render_header,
    status_bar,
    tool_begin_line,
    tool_end_line,
    tool_running_line,
)

console = Console(highlight=False)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------


@click.group()
def cli() -> None:
    """Loom — harness-first agent framework."""


@cli.command()
@click.option("--model", default=None, show_default=True)
@click.option("--db", default="~/.loom/memory.db", show_default=True)
@click.option("--tui", is_flag=True, default=False, help="Use Textual TUI interface.")
@click.option("--resume", is_flag=True, default=False, help="Resume the most recent session.")
@click.option("--session", "resume_id", default=None, metavar="ID", help="Resume a specific session by ID.")
def chat(model: str, db: str, tui: bool, resume: bool, resume_id: str | None) -> None:
    """Start an interactive agent session."""
    asyncio.run(_resolve_and_chat(model, db, tui, resume, resume_id))


async def _resolve_and_chat(
    model: str,
    db: str,
    tui: bool,
    resume: bool,
    resume_id: str | None,
) -> None:
    """Resolve --resume / --session flags, then launch the appropriate interface."""
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
            console.print(f"[dim]Resuming session [cyan]{resolved_id}[/cyan]: {title}[/dim]")
        else:
            console.print("[dim]No sessions found — starting a new session.[/dim]")

    if tui:
        await _chat_tui(model, db, resume_session_id=resolved_id)
    else:
        await _chat(model, db, resume_session_id=resolved_id)


async def _chat(model: str, db: str, resume_session_id: str | None = None) -> None:
    session = LoomSession(model=model, db_path=db, resume_session_id=resume_session_id)
    await session.start()

    # Issue #120 PR1: show diagnostic summaries inline in the CLI.
    async def _cli_diagnostic(diagnostic):
        vis = session._reflection_visibility
        if vis == "off":
            return
        console.print(f"[dim]  ⇢ diagnosed {diagnostic.one_line_summary()}[/dim]")
        if vis == "verbose" and diagnostic.mutation_suggestions:
            for hint in diagnostic.mutation_suggestions[:2]:
                console.print(f"[dim]      · {hint}[/dim]")

    session.subscribe_diagnostic(_cli_diagnostic)

    # Issue #120 PR3: surface skill lifecycle transitions inline.
    async def _cli_promotion(event) -> None:
        colour = {
            "promote": "green",
            "rollback": "yellow",
            "auto_shadow": "cyan",
            "deprecate": "red",
        }.get(event.kind, "white")
        console.print(
            f"[dim]  ⇢ [/dim][{colour}]{event.one_line_summary()}[/{colour}]"
        )

    session.subscribe_promotion(_cli_promotion)

    console.print(render_header(model, db))

    if not session._memory_index.is_empty:
        console.print(
            Panel(
                session._memory_index.render(),
                title="[cyan]Memory[/cyan]",
                border_style="dim",
            )
        )

    prompt_session = make_prompt_session()

    try:
        while True:
            # ── Read user input (prompt_toolkit — history + autocomplete) ──
            try:
                user_input: str = await prompt_session.prompt_async(
                    "\nyou> ",
                    style=None,
                )
            except (EOFError, KeyboardInterrupt):
                break

            if not user_input.strip():
                continue

            if user_input.strip().lower() in {"exit", "quit", "q"}:
                break

            # ── Slash commands ────────────────────────────────────────────
            if user_input.startswith("/"):
                await _handle_slash(user_input.strip(), session)
                continue

            # ── Streaming turn with Rich Live display ─────────────────────
            console.print()
            await _run_streaming_turn(session, user_input)

    finally:
        await session.stop()
        console.print("\n[dim]Session ended. Goodbye.[/dim]")


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
    Shared /scope logic for CLI and TUI.

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
            console.print("[dim]  No active scope grants.[/dim]")
            if purged:
                console.print(f"[dim]  ({purged} expired grant{'s' if purged != 1 else ''} removed)[/dim]")
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
            console.print(f"[dim]  ({purged} expired grant{'s' if purged != 1 else ''} removed)[/dim]")
    else:
        # Delegate revoke/clear/help to shared core
        _scope_command_core(perm, args, lambda msg: console.print(f"[dim]  {msg}[/dim]"))


async def _handle_slash(cmd: str, session: "LoomSession") -> None:
    """Dispatch a slash command and print feedback."""
    parts = cmd.split(maxsplit=1)
    command = parts[0]
    arg = parts[1].strip() if len(parts) > 1 else ""

    if command == "/model":
        if not arg:
            providers = ", ".join(session.router.providers)
            console.print(
                f"[dim]Current model: [bold]{session.model}[/bold]  "
                f"providers: {providers}[/dim]\n"
                "[dim]  MiniMax-*        requires MINIMAX_API_KEY in .env (Anthropic-compatible endpoint)[/dim]\n"
                "[dim]  claude-*         requires ANTHROPIC_API_KEY in .env[/dim]\n"
                "[dim]  ollama/<name>    enable [providers.ollama] in loom.toml[/dim]\n"
                "[dim]  lmstudio/<name>  enable [providers.lmstudio] in loom.toml[/dim]"
            )
        else:
            ok = session.set_model(arg)
            if ok:
                console.print(f"[dim]Model switched to: [bold]{arg}[/bold][/dim]")
            else:
                console.print(
                    f"[red]Could not switch to '{arg}'.[/red] "
                    "[dim]Either the prefix is not recognised, or the provider is not registered "
                    "(check API key in .env or enable in loom.toml).[/dim]"
                )

    if command == "/personality":
        if not arg:
            p = session.current_personality
            avail = session._stack.available_personalities()
            console.print(
                f"[dim]Active: [bold]{p or '(none)'}[/bold]  "
                f"Available: {', '.join(avail) or '(none)'}[/dim]"
            )
        elif arg == "off":
            session.switch_personality("off")
            console.print("[dim]Personality cleared.[/dim]")
        else:
            ok = session.switch_personality(arg)
            if ok:
                console.print(f"[dim]Personality -> [bold]{arg}[/bold][/dim]")
            else:
                avail = session._stack.available_personalities()
                console.print(
                    f"[red]Unknown personality '{arg}'.[/red] "
                    f"[dim]Available: {', '.join(avail) or '(none)'}[/dim]"
                )

    elif command == "/think":
        think = session._last_think
        if think:
            console.print(
                Panel(think, title="[dim]Reasoning chain[/dim]", border_style="dim")
            )
        else:
            console.print("[dim]No reasoning chain captured for the last turn.[/dim]")

    elif command == "/compact":
        pct = session.budget.usage_fraction * 100
        console.print(f"[dim]  Compacting context ({pct:.1f}% used)…[/dim]")
        await session._smart_compact()

    elif command == "/stop":
        # In CLI the turn is a blocking await — the user can't type while it runs.
        # /stop typed before a turn starts is a no-op; the real interrupt is Ctrl+C.
        console.print(
            "[dim]  /stop interrupts a running turn.  "
            "In CLI mode, press [yellow]Ctrl+C[/yellow] while the agent is responding.[/dim]"
        )

    elif command == "/auto":
        if not session._strict_sandbox:
            console.print(
                "[yellow]  /auto requires strict_sandbox = true in loom.toml.[/yellow]\n"
                "[dim]  Without workspace confinement, auto-approving run_bash "
                "would grant unrestricted shell access.[/dim]"
            )
        else:
            session.perm.exec_auto = not session.perm.exec_auto
            state = "on" if session.perm.exec_auto else "off"
            if session.perm.exec_auto:
                console.print(
                    f"[dim]Exec auto-approve: [green]{state}[/green] — "
                    "run_bash pre-authorized within workspace. "
                    "Absolute paths that escape the workspace still require confirmation.[/dim]"
                )
            else:
                console.print(f"[dim]Exec auto-approve: [yellow]{state}[/yellow] — run_bash will confirm every call.[/dim]")

    elif command.startswith("/scope"):
        _scope_args = command[len("/scope"):].strip()
        _handle_scope_command(session, _scope_args, console)

    elif command == "/pause":
        # Toggle HITL mode (auto-pause after every tool batch)
        session.hitl_mode = not session.hitl_mode
        state = "on" if session.hitl_mode else "off"
        console.print(
            f"[dim]HITL pause mode: [{'yellow' if session.hitl_mode else 'green'}]{state}"
            f"[/{'yellow' if session.hitl_mode else 'green'}][/dim]"
        )
        if session.hitl_mode:
            console.print(
                "[dim]  The agent will pause after each tool batch for your input.[/dim]\n"
                "[dim]  At pause> :  r(esume) · c(ancel) · <message>(redirect)[/dim]"
            )

    elif command == "/help":
        console.print(
            Panel(
                "[bold]Session[/bold]\n\n"
                "  Start a new session:    [yellow]loom chat[/yellow]\n"
                "  Resume last session:    [yellow]loom chat --resume[/yellow]\n"
                "  Resume specific:        [yellow]loom chat --session <id>[/yellow]\n"
                "  List sessions:          [yellow]loom sessions list[/yellow]\n\n"
                "[bold]Slash commands[/bold]\n\n"
                "  [yellow]/new[/yellow]                       Start a fresh session\n"
                "  [yellow]/sessions[/yellow]                  Browse and switch sessions\n"
                "  [yellow]/model[/yellow]                     Show current model + registered providers\n"
                "  [yellow]/model[/yellow] [dim]<name>[/dim]              Switch model at runtime\n"
                "    [dim]MiniMax-M2.7            → MiniMax via Anthropic SDK (MINIMAX_API_KEY)[/dim]\n"
                "    [dim]claude-sonnet-4-6       → Anthropic (ANTHROPIC_API_KEY)[/dim]\n"
                "    [dim]ollama/<model>          → local Ollama  (enable in loom.toml)[/dim]\n"
                "    [dim]lmstudio/<model>        → local LM Studio  (enable in loom.toml)[/dim]\n"
                "  [yellow]/personality[/yellow] [dim]<name>[/dim]      Switch cognitive persona\n"
                "  [yellow]/personality off[/yellow]           Remove active persona\n"
                "  [yellow]/think[/yellow]                     View last turn's reasoning chain\n"
                "  [yellow]/compact[/yellow]                   Compress older context\n"
                "  [yellow]/auto[/yellow]                      Toggle run_bash auto-approve (requires strict_sandbox)\n"
                "  [yellow]/scope[/yellow]                     List active scope grants (leases)\n"
                "  [yellow]/scope revoke <N>[/yellow]          Revoke a specific grant\n"
                "  [yellow]/scope clear[/yellow]               Revoke all non-system grants\n"
                "  [yellow]/pause[/yellow]                     Toggle HITL pause after each tool batch\n"
                "  [yellow]/stop[/yellow]                      Immediately cancel a running turn (CLI: use Ctrl+C)\n"
                "  [yellow]/help[/yellow]                      Show this message\n\n"
                "[bold]Keyboard shortcuts[/bold]\n\n"
                "  [dim]Ctrl-L[/dim]       Clear screen\n"
                "  [dim]up / down[/dim]    Browse input history\n"
                "  [dim]Tab[/dim]          Autocomplete slash commands\n"
                "  [dim]exit / Ctrl-C[/dim]  End session",
                title="[yellow] Loom — command reference [/yellow]",
                border_style="yellow",
            )
        )

    else:
        console.print(f"[dim]Unknown command '{command}'. Type /help for help.[/dim]")


# ---------------------------------------------------------------------------
# Textual TUI integration
# ---------------------------------------------------------------------------


class LoomChatApp:
    """
    Subclass of LoomApp that wires a live LoomSession to the Textual component
    tree.  Instantiated lazily to avoid importing Textual at module load time
    (keeps `loom chat` startup fast for users without the TUI).
    """

    @staticmethod
    def create(session: "LoomSession") -> "Any":
        """Return a configured LoomApp instance bound to *session*."""
        from loom.platform.cli.tui import LoomApp
        from loom.platform.cli.tui.events import (
            TurnStart,
            TextChunk as TuiChunk,
            ToolBegin as TuiToolBegin,
            ToolEnd as TuiToolEnd,
            TurnDone as TuiTurnDone,
            TurnPaused as TuiTurnPaused,
            ThinkCollapsed as TuiThinkCollapsed,
            ActionStateChange as TuiActionStateChange,
            ActionRolledBack as TuiActionRolledBack,
            EnvelopeStarted as TuiEnvelopeStarted,
            EnvelopeUpdated as TuiEnvelopeUpdated,
            EnvelopeCompleted as TuiEnvelopeCompleted,
            GrantsUpdate as TuiGrantsUpdate,
            GrantInfo as TuiGrantInfo,
        )
        from loom.platform.cli.ui import (
            TextChunk,
            ThinkCollapsed,
            ToolBegin,
            ToolEnd,
            TurnDone,
            TurnPaused,
            ActionStateChange,
            ActionRolledBack,
        )
        from loom.core.events import (
            EnvelopeStarted,
            EnvelopeUpdated,
            EnvelopeCompleted,
            GrantsSnapshot,
        )

        class _App(LoomApp):
            def __init__(self) -> None:
                super().__init__(
                    model=session.model,
                    db_path=str(session._store.path),
                )
                self._session = session
                # HITL: the worker awaits this; on_loom_app_hitl_decision sets it
                self._hitl_event: asyncio.Event = asyncio.Event()
                self._hitl_decision: str | None = None

            def on_loom_app_hitl_decision(self, msg: Any) -> None:
                self._hitl_decision = msg.decision
                self._hitl_event.set()

            from textual import work
            @work(exclusive=True)
            async def action_time_travel(self) -> None:
                async with self._session._store.connect() as conn:
                    cursor = await conn.execute(
                        "SELECT turn_index, role, content FROM session_log WHERE session_id = ? ORDER BY turn_index ASC, id ASC",
                        (self._session.session_id,)
                    )
                    rows = await cursor.fetchall()

                from collections import defaultdict
                turns = defaultdict(list)
                for t_idx, role, content in rows:
                    if not content: continue
                    cont = str(content)
                    if role == "tool":
                        cont = f"[tool] {cont[:40]}"
                    turns[t_idx].append((role, cont.strip()))

                turns_data = []
                for t_idx, items in sorted(turns.items()):
                    user_text = ""
                    agent_texts = []
                    for r, c in items:
                        if r == "user":
                            user_text = c[:80].replace("\n", " ")
                        else:
                            agent_texts.append(c[:60].replace("\n", " "))
                    
                    sum_text = f"[bold yellow]Turn {t_idx}[/] [cyan]{user_text}[/]"
                    if agent_texts:
                        sum_text += f"\n   [dim]↳ {' | '.join(agent_texts)[:120]}[/]"
                    
                    turns_data.append((t_idx, sum_text))
                
                if not turns_data:
                    self.notify("No history to time travel.", severity="information")
                    return

                from loom.platform.cli.tui.components.minimap_modal import MiniMapModal
                target_turn = await self.push_screen_wait(MiniMapModal(turns_data))
                
                if target_turn is not None:
                    old_id = self._session.session_id
                    import uuid
                    new_id = f"{old_id}-fork-{uuid.uuid4().hex[:6]}"
                    async with self._session._store.connect() as conn:
                        from loom.core.memory.session_log import SessionLog
                        await SessionLog(conn).fork_session(old_id, new_id, target_turn)
                    
                    self.workers.cancel_all()
                    self.exit(new_id)

            async def on_mount(self) -> None:
                """Replay history on startup and seed the Budget panel."""
                from loom.platform.cli.tui.components.message_list import (
                    MessageList,
                    Role,
                )
                from textual.css.query import NoMatches

                # Replay session history if resuming
                if session._resume and session.messages:
                    try:
                        msg_list = self.query_one("#message-list", MessageList)
                        for msg in session.messages:
                            role = msg.get("role")
                            content = msg.get("content", "")
                            if not content:
                                continue
                            if role == "user":
                                msg_list.add_message(Role.USER, content)
                            elif role == "assistant":
                                msg_list.add_message(Role.ASSISTANT, content)
                    except (NoMatches, Exception):
                        pass  # TUI not fully composed yet — skip replay

                # Seed Budget panel with current token state
                try:
                    from loom.platform.cli.tui.components import WorkspacePanel
                    frac = session.budget.usage_fraction
                    used = session.budget.used_tokens
                    total = session.budget.total_tokens
                    ws = self.query_one("#workspace-panel", WorkspacePanel)
                    ws.update_budget(
                        fraction=frac,
                        used_tokens=used,
                        max_tokens=total,
                        input_tokens=0,
                        output_tokens=0,
                    )
                except Exception:
                    pass  # budget not ready yet — panel stays at defaults

            async def on_unmount(self) -> None:
                await self._session.stop()

            def on_input_area_submit(self, event: Any) -> None:
                """Override LoomApp relay — drive session via Textual worker."""
                text = event.text.strip()
                if not text:
                    return
                # exclusive=True cancels any in-progress turn; exit_on_error=False
                # keeps the app alive if _run_turn raises unexpectedly.
                self.run_worker(
                    self._run_turn(text),
                    exclusive=True,
                    exit_on_error=False,
                )

            async def _run_turn(self, text: str) -> None:
                try:
                    if text.startswith("/"):
                        await _handle_slash_tui(text, self._session, self)
                        return

                    await self.dispatch_stream_event(
                        TurnStart(
                            user_input=text,
                            context_pct=self._session.budget.usage_fraction,
                        )
                    )

                    # call_id → write path (for artifact tracking)
                    _pending_writes: dict[str, str] = {}
                    # call_id → primary arg preview (for ActivityLog args column)
                    _tool_args_preview: dict[str, str] = {}

                    async for ev in self._session.stream_turn(text):
                        if isinstance(ev, TextChunk):
                            await self.dispatch_stream_event(TuiChunk(text=ev.text))
                        elif isinstance(ev, ThinkCollapsed):
                            await self.dispatch_stream_event(
                                TuiThinkCollapsed(summary=ev.summary, full=ev.full)
                            )
                        elif isinstance(ev, ToolBegin):
                            # Capture primary arg for ActivityLog display
                            _primary_arg = ""
                            if ev.args:
                                first_val = next(iter(ev.args.values()), "")
                                if isinstance(first_val, str):
                                    _primary_arg = first_val[:40].replace("\n", "↵")
                            _tool_args_preview[ev.call_id] = _primary_arg

                            await self.dispatch_stream_event(
                                TuiToolBegin(
                                    name=ev.name,
                                    args=ev.args,
                                    call_id=ev.call_id,
                                )
                            )
                            if ev.name == "write_file":
                                _pending_writes[ev.call_id] = ev.args.get("path", "")
                        elif isinstance(ev, ToolEnd):
                            # Patch args_preview into the ToolEnd event for ActivityLog
                            _args_preview = _tool_args_preview.pop(ev.call_id, "")
                            _tui_tool_end = TuiToolEnd(
                                name=ev.name,
                                success=ev.success,
                                output=ev.output,
                                duration_ms=ev.duration_ms,
                                call_id=ev.call_id,
                            )
                            # Stash preview on the event object so app._on_tool_end can use it
                            _tui_tool_end._args_preview = _args_preview  # type: ignore[attr-defined]
                            await self.dispatch_stream_event(_tui_tool_end)

                            if ev.name == "write_file" and ev.success:
                                from loom.platform.cli.tui.components import ArtifactState
                                path = _pending_writes.pop(ev.call_id, "")
                                if path:
                                    self.add_artifact(path, ArtifactState.MODIFIED)
                        elif isinstance(ev, TurnPaused):
                            # Show PauseModal and wait for the user's decision
                            self._hitl_event.clear()
                            self._hitl_decision = None
                            await self.dispatch_stream_event(
                                TuiTurnPaused(tool_count_so_far=ev.tool_count_so_far)
                            )
                            await self._hitl_event.wait()
                            decision = self._hitl_decision
                            if decision == "__cancel__":
                                self._session.cancel()
                            elif decision:
                                self._session.resume_with(decision)
                            else:
                                self._session.resume()
                        elif isinstance(ev, TurnDone):
                            budget = self._session.budget
                            await self.dispatch_stream_event(
                                TuiTurnDone(
                                    tool_count=ev.tool_count,
                                    input_tokens=ev.input_tokens,
                                    output_tokens=ev.output_tokens,
                                    elapsed_ms=ev.elapsed_ms,
                                    context_pct=budget.usage_fraction,
                                    used_tokens=budget.used_tokens,
                                    max_tokens=budget.total_tokens,
                                    think_text=self._session._last_think,
                                    stop_reason=ev.stop_reason,
                                )
                            )
                        elif isinstance(ev, ActionStateChange):
                            await self.dispatch_stream_event(
                                TuiActionStateChange(
                                    action_id=ev.action_id,
                                    tool_name=ev.tool_name,
                                    call_id=ev.call_id,
                                    old_state=ev.old_state,
                                    new_state=ev.new_state,
                                    reason=ev.reason,
                                )
                            )
                        elif isinstance(ev, ActionRolledBack):
                            await self.dispatch_stream_event(
                                TuiActionRolledBack(
                                    action_id=ev.action_id,
                                    tool_name=ev.tool_name,
                                    call_id=ev.call_id,
                                    rollback_success=ev.rollback_success,
                                    message=ev.message,
                                )
                            )
                        elif isinstance(ev, EnvelopeStarted):
                            await self.dispatch_stream_event(
                                TuiEnvelopeStarted(envelope=ev.envelope)
                            )
                        elif isinstance(ev, EnvelopeUpdated):
                            await self.dispatch_stream_event(
                                TuiEnvelopeUpdated(envelope=ev.envelope)
                            )
                        elif isinstance(ev, EnvelopeCompleted):
                            await self.dispatch_stream_event(
                                TuiEnvelopeCompleted(envelope=ev.envelope)
                            )
                        elif isinstance(ev, GrantsSnapshot):
                            tui_grants = [
                                TuiGrantInfo(
                                    grant_id=g.grant_id,
                                    tool_name=g.tool_name,
                                    selector=g.selector,
                                    source=g.source,
                                    expires_at=g.expires_at,
                                )
                                for g in ev.grants
                            ]
                            await self.dispatch_stream_event(
                                TuiGrantsUpdate(
                                    active_count=ev.active_count,
                                    next_expiry_secs=ev.next_expiry_secs,
                                    grants=tui_grants,
                                )
                            )
                except asyncio.CancelledError:
                    pass
                except Exception as exc:
                    import traceback as _tb

                    _log = Path.home() / ".loom" / "tui_error.log"
                    _log.parent.mkdir(parents=True, exist_ok=True)
                    with open(_log, "a") as _f:
                        _f.write(_tb.format_exc())
                    self.notify(
                        f"Error: {exc}  (details in ~/.loom/tui_error.log)",
                        severity="error",
                        timeout=20,
                    )

        return _App()


async def _handle_slash_tui(cmd: str, session: "LoomSession", app: Any) -> None:
    """Slash command handler for TUI mode — sends feedback via app.notify()."""
    parts = cmd.split(maxsplit=1)
    command = parts[0]
    arg = parts[1].strip() if len(parts) > 1 else ""

    if command == "/model":
        if not arg:
            providers = ", ".join(session.router.providers)
            app.notify(
                f"Model: {session.model}  |  providers: {providers}\n"
                "Prefixes: MiniMax-*  claude-*  ollama/<name>  lmstudio/<name>"
            )
        else:
            ok = session.set_model(arg)
            if ok:
                app.notify(f"Model switched to: {arg}")
            else:
                app.notify(
                    f"Cannot switch to '{arg}' — prefix not recognised or provider "
                    "not registered (check .env key or loom.toml [providers.*]).",
                    severity="error",
                )

    if command == "/personality":
        if not arg:
            p = session.current_personality
            avail = session._stack.available_personalities()
            app.notify(
                f"Active: {p or '(none)'}  |  Available: {', '.join(avail) or '(none)'}"
            )
        elif arg == "off":
            session.switch_personality("off")
            app.notify("Personality cleared.")
        else:
            ok = session.switch_personality(arg)
            if ok:
                app.notify(f"Personality → {arg}")
            else:
                avail = session._stack.available_personalities()
                app.notify(
                    f"Unknown personality '{arg}'. Available: {', '.join(avail) or '(none)'}",
                    severity="error",
                )

    elif command == "/compact":
        pct = session.budget.usage_fraction * 100
        app.notify(f"Compacting context ({pct:.1f}% used)…")
        await session._smart_compact()
        app.notify("Context compacted.")

    elif command == "/auto":
        if not session._strict_sandbox:
            app.notify(
                "/auto requires strict_sandbox = true in loom.toml. "
                "Without workspace confinement, auto-approving run_bash "
                "would grant unrestricted shell access.",
                severity="warning",
                timeout=6,
            )
        else:
            session.perm.exec_auto = not session.perm.exec_auto
            state = "on" if session.perm.exec_auto else "off"
            msg = (
                f"Exec auto-approve: {state} — run_bash pre-authorized within workspace. "
                "Absolute paths that escape the workspace still require confirmation."
                if session.perm.exec_auto
                else f"Exec auto-approve: {state} — run_bash will confirm every call."
            )
            app.notify(msg, timeout=5)

    elif command.startswith("/scope"):
        _scope_args = command[len("/scope"):].strip()
        _scope_command_core(
            session.perm, _scope_args,
            lambda msg: app.notify(msg, timeout=5),
        )

    elif command == "/pause":
        session.hitl_mode = not session.hitl_mode
        state = "on" if session.hitl_mode else "off"
        app.notify(
            f"HITL pause mode: {state}  "
            + ("— agent will pause after each tool batch." if session.hitl_mode else ""),
            timeout=3,
        )

    elif command == "/stop":
        # Immediate cancel — same as pressing Escape
        app.action_interrupt()

    elif command == "/new":
        # Exit with sentinel None so _chat_tui restart loop creates a fresh session.
        app.exit("__new__")

    elif command == "/sessions":
        # Show session picker; if a different session is chosen, exit so
        # _chat_tui() loop can restart with the new session_id.
        from loom.core.memory.session_log import SessionLog as _SL
        from loom.platform.cli.tui.components.session_picker import SessionPickerModal

        async def _update_title(sid: str, title: str) -> None:
            async with session._store.connect() as conn:
                await _SL(conn).update_title(sid, title)

        async with session._store.connect() as conn:
            rows = await _SL(conn).list_sessions(limit=20)
        selected = await app.push_screen_wait(
            SessionPickerModal(rows, update_title_fn=_update_title)
        )
        if selected and selected != session.session_id:
            app.exit(selected)  # _chat_tui restart loop picks this up
        elif selected == session.session_id:
            app.notify("Already in this session.", severity="information")

    elif command == "/help":
        from loom.platform.cli.tui.components.help_modal import HelpModal
        await app.push_screen_wait(HelpModal())

    else:
        app.notify(f"Unknown command '{command}'. Type /help.", severity="warning")


async def _chat_tui(model: str, db: str, resume_session_id: str | None = None) -> None:
    """Launch the Textual TUI chat session.

    If no resume_session_id is given, auto-resume the most recent saved session
    so users continue where they left off without extra flags.
    """
    if model is None:
        from loom.core.cognition.router import get_default_model
        model = get_default_model()
    db_path = str(Path(db).expanduser())

    # Auto-resume last session when no explicit target is given
    if resume_session_id is None:
        store = SQLiteStore(db_path)
        await store.initialize()
        async with store.connect() as conn:
            rows = await SessionLog(conn).list_sessions(limit=1)
        if rows:
            resume_session_id = rows[0]["session_id"]

    # Session switch loop: /sessions command exits the app with the new session_id.
    # We restart the whole setup with the requested session.
    next_session_id: str | None = resume_session_id
    while True:
        session = LoomSession(model=model, db_path=db_path,
                              resume_session_id=next_session_id)
        await session.start()

        app = LoomChatApp.create(session)

        # Replace BlastRadiusMiddleware's confirm_fn with a TUI-aware version that
        # shows an inline widget dialog — no terminal suspension needed.
        from loom.core.harness.middleware import BlastRadiusMiddleware
        from loom.core.harness.scope import ConfirmDecision
        from loom.platform.cli.tui.components.interactive_widgets import InlineConfirmWidget
        import asyncio

        async def _tui_confirm(call: "ToolCall") -> "ConfirmDecision":
            args_copy = dict(call.args)
            justification = args_copy.pop("justification", None)

            args_preview = "  ".join(
                f"{k}={str(v)[:40]}" for k, v in args_copy.items()
            )[:120]

            msg_list = app.query_one("#message-list")
            future: asyncio.Future[ConfirmDecision] = asyncio.Future()
            widget = InlineConfirmWidget(
                tool_name=call.tool_name,
                trust_label=call.trust_level.plain,
                args_preview=args_preview,
                future=future,
                justification=str(justification) if justification else None,
                call_id=call.id,
            )
            msg_list.mount(widget)
            msg_list.scroll_end(animate=False)

            return await future

        for mw in session._pipeline._middlewares:
            if isinstance(mw, BlastRadiusMiddleware):
                mw._confirm = _tui_confirm
                break
        # Also patch skill check approval so it uses TUI confirm widgets
        session._confirm_fn = _tui_confirm

        # Issue #120 PR1: surface TaskDiagnostic as a TUI notification so
        # skill reflections aren't buried in background tasks.
        async def _tui_diagnostic(diagnostic):
            vis = session._reflection_visibility
            if vis == "off":
                return
            try:
                if vis == "verbose" and diagnostic.mutation_suggestions:
                    body = (
                        f"{diagnostic.one_line_summary()}\n"
                        f"→ {diagnostic.mutation_suggestions[0][:100]}"
                    )
                    app.notify(body, title="Skill diagnostic", timeout=6)
                else:
                    app.notify(
                        diagnostic.one_line_summary(),
                        title="Skill diagnostic",
                        timeout=4,
                    )
            except Exception:
                pass

        session.subscribe_diagnostic(_tui_diagnostic)

        # Issue #120 PR3: notify on skill lifecycle transitions.
        async def _tui_promotion(event) -> None:
            try:
                severity = {
                    "rollback": "warning",
                    "deprecate": "warning",
                }.get(event.kind, "information")
                app.notify(
                    event.one_line_summary(),
                    title="Skill lifecycle",
                    severity=severity,
                    timeout=5,
                )
            except Exception:
                pass

        session.subscribe_promotion(_tui_promotion)

        result = await app.run_async()
        # /sessions exits with a session_id string → resume that session.
        # /new exits with "__new__" sentinel → start a fresh session (no resume).
        # Any other exit (Ctrl+C, quit) → done.
        if result == "__new__":
            next_session_id = None
        elif isinstance(result, str):
            next_session_id = result
        else:
            break


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

    # ── Opening rule ──────────────────────────────────────────────────────────
    pct = session.budget.usage_fraction * 100
    ctx_color = "green" if pct < 60 else "yellow" if pct < 85 else "red"
    persona_tag = (
        f"  [dim]|  persona: {session.current_personality}[/dim]"
        if session.current_personality
        else ""
    )
    console.print(
        Rule(
            f"[bold green]loom[/bold green]"
            f"[dim]  |  [{ctx_color}]context {pct:.1f}%[/{ctx_color}][/dim]"
            f"{persona_tag}",
            style="green",
        )
    )

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

    try:
        async for event in session.stream_turn(user_input):
            if isinstance(event, TextChunk):
                # Clear cursor from previous position
                clear_line()
                # Print text chunk
                console.print(event.text, end="", markup=False, highlight=False)
                text_buffer += event.text
                at_line_start = event.text.endswith("\n")
                # Print streaming cursor at end
                console.print(render_cursor(), end="")

            elif isinstance(event, ThinkCollapsed):
                # Show condensed reasoning summary inline; use /think for full content.
                clear_line()
                if not at_line_start:
                    console.print()
                console.print(
                    Text.from_markup(f"[dim]💭 {event.summary}[/dim]")
                )
                at_line_start = True

            elif isinstance(event, ToolBegin):
                # Cancel any running spinner
                _cancel_spinner()
                # Ensure tool rows start on a fresh line
                if not at_line_start:
                    console.print()
                    at_line_start = True
                active_tool = event.name
                frame_index = 0
                console.print(tool_begin_line(event.name, event.args))
                # Start spinner animation
                spinner_task = asyncio.create_task(_spin_loop())

            elif isinstance(event, ToolEnd):
                # Cancel spinner and clear its line
                _cancel_spinner()
                clear_line()
                console.print(
                    tool_end_line(event.name, event.success, event.duration_ms)
                )
                at_line_start = True
                active_tool = None
                console.print()

            elif isinstance(event, TurnPaused):
                # ── HITL pause ────────────────────────────────────────────
                _cancel_spinner()
                clear_line()
                if not at_line_start:
                    console.print()
                console.print(
                    Rule(
                        f"[yellow]⏸  Paused[/yellow]  [dim]({event.tool_count_so_far} tool(s) so far)[/dim]",
                        style="yellow",
                    )
                )
                console.print(
                    "[dim]  r[/dim] resume  [dim]·[/dim]  "
                    "[dim]c[/dim] cancel  [dim]·[/dim]  "
                    "[dim]<message>[/dim] redirect and resume"
                )
                try:
                    raw = await asyncio.get_event_loop().run_in_executor(
                        None, lambda: input("pause> ").strip()
                    )
                except (EOFError, KeyboardInterrupt):
                    raw = "c"

                if raw in ("c", "cancel"):
                    session.cancel()
                elif raw in ("r", "resume", ""):
                    session.resume()
                else:
                    session.resume_with(raw)
                    console.print(f"[dim]  Injected: {raw[:80]}[/dim]")

            elif isinstance(event, TurnDone):
                # Cancel any running spinner and clear cursor
                _cancel_spinner()
                clear_line()
                if not at_line_start:
                    console.print()
                elapsed = time.monotonic() - t0
                console.print(
                    status_bar(
                        context_fraction=session.budget.usage_fraction,
                        input_tokens=event.input_tokens,
                        output_tokens=event.output_tokens,
                        elapsed_ms=elapsed * 1000,
                        tool_count=event.tool_count,
                    )
                )

    except Exception as exc:
        _cancel_spinner()
        clear_line()
        console.print()
        console.print(f"[red]Error: {exc}[/red]")


# ---------------------------------------------------------------------------
# sessions commands
# ---------------------------------------------------------------------------


@cli.group()
def sessions() -> None:
    """Manage saved conversation sessions."""


@sessions.command("list")
@click.option("--db", default="~/.loom/memory.db", show_default=True)
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
        console.print("[dim]No sessions found.[/dim]")
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
            r["title"] or "[dim](no title)[/dim]",
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
        console.print(f"[red]Session '{session_id}' not found.[/red]")
        return

    console.print(Rule(f"[cyan]Session {session_id}[/cyan]"))
    console.print(
        f"[dim]Model: {meta['model']}  |  "
        f"Turns: {meta['turn_count']}  |  "
        f"Started: {meta['started_at'][:16].replace('T', ' ')}[/dim]"
    )
    console.print()

    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "") or ""
        if role == "user":
            console.print(f"[bold yellow]you>[/bold yellow] {content}")
        elif role == "assistant":
            if content:
                console.print(Markdown(content))
        elif role == "tool":
            console.print(f"[dim]  [tool] {str(content)[:300]}[/dim]")
        console.print()


async def _sessions_rm(session_id: str, db: str) -> None:
    store = SQLiteStore(db)
    await store.initialize()
    async with store.connect() as conn:
        sl = SessionLog(conn)
        meta = await sl.get_session(session_id)
        if meta is None:
            console.print(f"[red]Session '{session_id}' not found.[/red]")
            return
        await sl.delete_session(session_id)
    console.print(f"[dim]Session [cyan]{session_id}[/cyan] deleted.[/dim]")


# ---------------------------------------------------------------------------


@cli.group()
def memory() -> None:
    """Inspect the memory store."""


@memory.command("list")
@click.option("--db", default="~/.loom/memory.db", show_default=True)
@click.option("--limit", default=20, show_default=True)
def memory_list(db: str, limit: int) -> None:
    """Show recent semantic memories."""
    asyncio.run(_memory_list(db, limit))


async def _memory_list(db: str, limit: int) -> None:
    store = SQLiteStore(db)
    await store.initialize()
    async with store.connect() as conn:
        sem = SemanticMemory(conn)
        entries = await sem.list_recent(limit)

    if not entries:
        console.print("[dim]No semantic memories yet.[/dim]")
        return

    console.print(Rule("[cyan]Semantic Memory[/cyan]"))
    for e in entries:
        c = "green" if e.confidence > 0.7 else "yellow" if e.confidence > 0.4 else "red"
        console.print(
            f"  [{c}]{e.confidence:.2f}[/{c}]  [dim]{e.key}[/dim]\n       {e.value}\n"
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
            console.print("[dim]No session ID given — showing skill health only.[/dim]")
        else:
            summary = await api.session_summary(session_id)
            console.print(Panel(summary, title=f"[cyan]Session {session_id}[/cyan]"))

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
                    f"  [green]{s['confidence']:.2f}[/green]  "
                    f"[bold]{s['name']}[/bold]  "
                    f"[dim]used {s['usage_count']}×  "
                    f"tags: {s['tags']}[/dim]"
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
        console.print(f"[dim]No diagnostics found{where}.[/dim]")
        return

    console.print(Rule("[cyan]Recent skill diagnostics[/cyan]"))
    for e in entries:
        try:
            diag = TaskDiagnostic.from_json(e.value)
        except Exception:
            console.print(f"  [red]![/red] [dim]{e.key}[/dim]  (unparseable)")
            continue

        score_color = (
            "green" if diag.quality_score >= 4.0
            else "yellow" if diag.quality_score >= 2.5
            else "red"
        )
        ts = diag.timestamp.strftime("%Y-%m-%d %H:%M")
        console.print(
            f"[dim]{ts}[/dim]  "
            f"[bold cyan]{diag.skill_name}[/bold cyan]  "
            f"[dim]{diag.task_type}[/dim]  "
            f"[{score_color}]{diag.quality_score:.1f}[/{score_color}]"
        )
        if diag.instructions_violated:
            for v in diag.instructions_violated[:3]:
                console.print(f"   [red]✗[/red] {v}")
        if diag.mutation_suggestions:
            console.print("   [bold]→ suggestions:[/bold]")
            for s in diag.mutation_suggestions[:3]:
                console.print(f"     • {s}")
        console.print()


# ---------------------------------------------------------------------------
# loom skill commands (Issue #120 PR 2)
# ---------------------------------------------------------------------------


@cli.group()
def skill() -> None:
    """Inspect skill genomes and candidate revisions."""


@skill.command("candidates")
@click.option("--skill", "skill_name", default=None, metavar="NAME",
              help="Filter by parent skill name.")
@click.option("--status", default=None,
              type=click.Choice(
                  ["generated", "shadow", "promoted", "deprecated", "rolled_back"],
                  case_sensitive=False,
              ),
              help="Filter by candidate status.")
@click.option("--limit", default=20, show_default=True, type=int)
@click.option("--show-body", is_flag=True, default=False,
              help="Also print the full candidate body.")
@click.option("--db", default="~/.loom/memory.db", show_default=True)
def skill_candidates(
    skill_name: str | None,
    status: str | None,
    limit: int,
    show_body: bool,
    db: str,
) -> None:
    """List proposed SKILL.md revisions from the candidate pool."""
    asyncio.run(_skill_candidates(skill_name, status, limit, show_body, db))


async def _skill_candidates(
    skill_name: str | None,
    status: str | None,
    limit: int,
    show_body: bool,
    db: str,
) -> None:
    from loom.core.memory.procedural import ProceduralMemory

    store = SQLiteStore(db)
    await store.initialize()
    async with store.connect() as conn:
        proc = ProceduralMemory(conn)
        candidates = await proc.list_candidates(
            parent_skill_name=skill_name,
            status=status.lower() if status else None,
            limit=limit,
        )

    if not candidates:
        where: list[str] = []
        if skill_name:
            where.append(f"skill='{skill_name}'")
        if status:
            where.append(f"status='{status.lower()}'")
        suffix = f" ({', '.join(where)})" if where else ""
        console.print(f"[dim]No skill candidates found{suffix}.[/dim]")
        return

    console.print(Rule("[cyan]Skill candidates[/cyan]"))
    status_color = {
        "generated": "yellow",
        "shadow": "cyan",
        "promoted": "green",
        "deprecated": "red",
        "rolled_back": "magenta",
    }
    for c in candidates:
        ts = c.created_at.strftime("%Y-%m-%d %H:%M")
        colour = status_color.get(c.status, "white")
        score_bits = ", ".join(
            f"{k}={v:.1f}" for k, v in c.pareto_scores.items()
        ) or "—"
        console.print(
            f"[dim]{ts}[/dim]  "
            f"[bold cyan]{c.parent_skill_name}[/bold cyan] v{c.parent_version}  "
            f"[{colour}]{c.status}[/{colour}]  "
            f"[dim]{c.mutation_strategy}[/dim]  "
            f"[dim]scores={score_bits}[/dim]  "
            f"[dim]id={c.id[:8]}[/dim]"
        )
        if c.notes:
            console.print(f"   [dim]note:[/dim] {c.notes}")
        if c.diagnostic_keys:
            console.print(
                f"   [dim]from:[/dim] {', '.join(c.diagnostic_keys[:2])}"
                + (" …" if len(c.diagnostic_keys) > 2 else "")
            )
        if show_body:
            console.print(Rule(style="dim"))
            console.print(c.candidate_body)
            console.print(Rule(style="dim"))
        console.print()


@skill.command("promote")
@click.argument("candidate_id")
@click.option("--reason", default=None, help="Audit note attached to the transition.")
@click.option("--db", default="~/.loom/memory.db", show_default=True)
def skill_promote(candidate_id: str, reason: str | None, db: str) -> None:
    """Swap the parent SKILL.md for the given candidate body."""
    asyncio.run(_skill_promote(candidate_id, reason, db))


async def _skill_promote(candidate_id: str, reason: str | None, db: str) -> None:
    from loom.core.cognition.skill_promoter import SkillPromoter
    from loom.core.memory.procedural import ProceduralMemory

    store = SQLiteStore(db)
    await store.initialize()
    async with store.connect() as conn:
        proc = ProceduralMemory(conn)
        # Resolve short (8-char) prefixes for operator ergonomics.
        resolved = await _resolve_candidate_id(proc, candidate_id)
        if resolved is None:
            console.print(f"[red]No candidate matches id prefix {candidate_id!r}.[/red]")
            return
        promoter = SkillPromoter(procedural=proc)
        try:
            parent = await promoter.promote(resolved, reason=reason)
        except ValueError as exc:
            console.print(f"[red]Refused:[/red] {exc}")
            return
    if parent is None:
        console.print(f"[red]Promotion failed — candidate or parent missing.[/red]")
        return
    console.print(
        f"[green]Promoted[/green] [bold cyan]{parent.name}[/bold cyan] "
        f"→ v{parent.version}"
    )


@skill.command("rollback")
@click.argument("skill_name")
@click.option("--to-version", type=int, default=None,
              help="Target version. Defaults to the most recently archived body.")
@click.option("--reason", default=None, help="Audit note attached to the transition.")
@click.option("--db", default="~/.loom/memory.db", show_default=True)
def skill_rollback(
    skill_name: str, to_version: int | None, reason: str | None, db: str,
) -> None:
    """Restore a previous SKILL.md body from history."""
    asyncio.run(_skill_rollback(skill_name, to_version, reason, db))


async def _skill_rollback(
    skill_name: str, to_version: int | None, reason: str | None, db: str,
) -> None:
    from loom.core.cognition.skill_promoter import SkillPromoter
    from loom.core.memory.procedural import ProceduralMemory

    store = SQLiteStore(db)
    await store.initialize()
    async with store.connect() as conn:
        proc = ProceduralMemory(conn)
        promoter = SkillPromoter(procedural=proc)
        parent = await promoter.rollback(skill_name, to_version=to_version, reason=reason)
    if parent is None:
        suffix = f" v{to_version}" if to_version else ""
        console.print(
            f"[red]Rollback failed — no history entry for {skill_name}{suffix}.[/red]"
        )
        return
    console.print(
        f"[yellow]Rolled back[/yellow] [bold cyan]{parent.name}[/bold cyan] "
        f"→ v{parent.version}"
    )


@skill.command("history")
@click.argument("skill_name")
@click.option("--limit", default=20, show_default=True, type=int)
@click.option("--db", default="~/.loom/memory.db", show_default=True)
def skill_history(skill_name: str, limit: int, db: str) -> None:
    """Show archived SKILL.md versions for a skill."""
    asyncio.run(_skill_history(skill_name, limit, db))


async def _skill_history(skill_name: str, limit: int, db: str) -> None:
    from loom.core.memory.procedural import ProceduralMemory

    store = SQLiteStore(db)
    await store.initialize()
    async with store.connect() as conn:
        proc = ProceduralMemory(conn)
        records = await proc.list_history(skill_name, limit=limit)

    if not records:
        console.print(f"[dim]No archived versions for {skill_name}.[/dim]")
        return

    console.print(Rule(f"[cyan]{skill_name} — version history[/cyan]"))
    reason_color = {"promote": "green", "rollback": "yellow", "manual": "dim"}
    for r in records:
        ts = r.archived_at.strftime("%Y-%m-%d %H:%M")
        colour = reason_color.get(r.reason, "white")
        src = (
            f"  [dim]from candidate {r.source_candidate_id[:8]}[/dim]"
            if r.source_candidate_id else ""
        )
        console.print(
            f"[dim]{ts}[/dim]  v{r.version}  "
            f"[{colour}]{r.reason}[/{colour}]{src}"
        )


@skill.command("set-maturity")
@click.argument("skill_name")
@click.argument("tag", required=False, default=None)
@click.option("--clear", is_flag=True, help="Clear the maturity_tag (equivalent to tag='clear').")
@click.option("--db", default="~/.loom/memory.db", show_default=True)
def skill_set_maturity(
    skill_name: str, tag: str | None, clear: bool, db: str,
) -> None:
    """Label a skill 'mature' / 'needs_improvement' — the meta-skill-engineer
    termination signal.  Use --clear (or tag='clear') to unset."""
    if clear:
        tag = None
    elif tag is None:
        pass
    else:
        # Same normalisation as the agent tool: case-insensitive, space/-
        # collapsed to underscores.
        normalised = tag.strip().lower().replace(" ", "_").replace("-", "_")
        if normalised in ("", "clear", "none", "null"):
            tag = None
        elif normalised in ("mature", "needs_improvement"):
            tag = normalised
        else:
            console.print(
                f"[red]Invalid tag {tag!r}.[/red] Use 'mature', 'needs_improvement', or --clear."
            )
            return
    asyncio.run(_skill_set_maturity(skill_name, tag, db))


async def _skill_set_maturity(skill_name: str, tag: str | None, db: str) -> None:
    from loom.core.memory.procedural import ProceduralMemory

    store = SQLiteStore(db)
    await store.initialize()
    async with store.connect() as conn:
        proc = ProceduralMemory(conn)
        ok = await proc.update_maturity_tag(skill_name, tag)
    if not ok:
        console.print(f"[red]Skill {skill_name!r} not found.[/red]")
        return
    display = tag if tag is not None else "(cleared)"
    console.print(
        f"[green]Updated[/green] [bold cyan]{skill_name}[/bold cyan] "
        f"maturity_tag → {display}"
    )


async def _resolve_candidate_id(proc: "ProceduralMemory", prefix: str) -> str | None:
    """Accept either a full uuid or a short prefix (≥4 chars).

    If the prefix matches exactly one candidate, return its full id.  Longer
    matches or non-matches return ``None`` so the caller can surface a clean
    error.
    """
    if len(prefix) >= 32:
        return prefix
    if len(prefix) < 4:
        return None
    rows = await proc.list_candidates(limit=500)
    matches = [c.id for c in rows if c.id.startswith(prefix)]
    if len(matches) == 1:
        return matches[0]
    return None


# ---------------------------------------------------------------------------
# loom review command
# ---------------------------------------------------------------------------


@cli.command("review")
@click.argument("skill_name")
@click.option("--db", default="~/.loom/memory.db", show_default=True)
def skill_review(skill_name: str, db: str) -> None:
    """One-stop report: genome status, candidate pool, eval history."""
    asyncio.run(_skill_review(skill_name, db))


async def _skill_review(skill_name: str, db: str) -> None:
    from loom.core.memory.procedural import ProceduralMemory
    from loom.core.memory.semantic import SemanticMemory

    store = SQLiteStore(db)
    await store.initialize()
    async with store.connect() as conn:
        proc = ProceduralMemory(conn)
        sem = SemanticMemory(conn)

        genome = await proc.get(skill_name)
        candidates = await proc.list_candidates(parent_skill_name=skill_name, limit=20)
        history_records = await proc.list_history(skill_name, limit=5)
        eval_entries = await sem.list_by_prefix(f"skill:{skill_name}:eval:", limit=20)
        insight_entries = await sem.list_by_prefix(f"skill:{skill_name}:insight:", limit=5)

    console.print(Rule(f"[bold cyan]{skill_name}[/bold cyan] — skill review"))

    # ── Genome ──────────────────────────────────────────────────────────
    if genome is None:
        # Empty state: most of the time the skill name is a typo or the
        # skill has been generated but never loaded.  Point the operator
        # at the next action instead of just reporting absence.
        console.print(f"[red]No SkillGenome found for '{skill_name}'.[/red]")
        suggestions: list[str] = []
        if candidates:
            suggestions.append(
                f"  • {len(candidates)} candidate(s) exist in the pool — "
                f"run [cyan]loom skill candidates {skill_name}[/cyan] to inspect."
            )
        if eval_entries or insight_entries:
            suggestions.append(
                f"  • Grader / Analyzer records exist for this name — "
                f"the genome may have been deprecated. Check [cyan]loom skill list[/cyan]."
            )
        if not suggestions:
            suggestions.extend([
                "  • Check the name with [cyan]loom skill list[/cyan] "
                "(typos are the most common cause).",
                "  • If this is a new skill, run the meta-skill-engineer workflow: "
                "[cyan]loom chat[/cyan] → ask Loom to create a skill.",
                "  • Pending candidates can still be listed via "
                "[cyan]loom skill candidates[/cyan].",
            ])
        console.print("\n".join(suggestions))
        return
    maturity = (
        f"  [bold yellow]{genome.maturity_tag}[/bold yellow]"
        if genome.maturity_tag else ""
    )
    console.print(
        f"  v{genome.version}  confidence=[cyan]{genome.confidence:.2f}[/cyan]"
        f"  usage={genome.usage_count}{maturity}"
    )

    # ── Eval history ────────────────────────────────────────────────────
    if eval_entries:
        console.print(Rule("[dim]Grader eval history[/dim]", style="dim"))
        for e in sorted(eval_entries, key=lambda x: x.key):
            ts = e.created_at.strftime("%Y-%m-%d") if e.created_at else "?"
            console.print(f"  [dim]{ts}[/dim]  [bold]{e.key.split(':')[-1]}[/bold]  {e.value[:120]}")
    else:
        console.print("[dim]  No Grader eval records yet.[/dim]")

    # ── Insights ────────────────────────────────────────────────────────
    if insight_entries:
        console.print(Rule("[dim]Analyzer insights[/dim]", style="dim"))
        for e in insight_entries:
            console.print(f"  [dim]{e.key}[/dim]  {e.value[:120]}")

    # ── Candidate pool ──────────────────────────────────────────────────
    if candidates:
        console.print(Rule("[dim]Candidate pool[/dim]", style="dim"))
        status_color = {
            "generated": "white", "shadow": "cyan", "promoted": "green",
            "deprecated": "dim", "rolled_back": "yellow",
        }
        for c in candidates:
            ts = c.created_at.strftime("%Y-%m-%d %H:%M")
            col = status_color.get(c.status, "white")
            ft = " [bold yellow]⚡fast-track[/bold yellow]" if c.fast_track else ""
            console.print(
                f"  [dim]{ts}[/dim]  [{col}]{c.status}[/{col}]"
                f"  {c.id[:8]}  {c.mutation_strategy}{ft}"
            )
    else:
        console.print("[dim]  No candidates in pool.[/dim]")

    # ── Version history ─────────────────────────────────────────────────
    if history_records:
        console.print(Rule("[dim]Recent version history[/dim]", style="dim"))
        for r in history_records:
            ts = r.archived_at.strftime("%Y-%m-%d %H:%M")
            console.print(f"  [dim]{ts}[/dim]  v{r.version}  [{r.reason}]")


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
        console.print(f"[red]Could not read '{raw_path}': {exc}[/red]")
        return

    # Extract via lens
    result = lens_registry.extract(source, lens_name=lens_name)
    if result is None:
        avail = ", ".join(lens_registry.registered_names) or "(none)"
        console.print(
            f"[red]No lens matched this file.[/red] "
            f"[dim]Available: {avail}. Use --lens to specify one.[/dim]"
        )
        return

    console.print(f"[dim]Lens:[/dim] [cyan]{result.source}[/cyan]  "
                  f"[dim]File:[/dim] {raw_path.name}")

    if result.warnings:
        for w in result.warnings:
            console.print(f"  [yellow]⚠[/yellow]  {w}")

    if result.is_empty:
        console.print("[dim]Nothing to import.[/dim]")
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
                marker = "[dim](dry-run)[/dim]" if dry_run else "[green]✓[/green]"
                console.print(
                    f"  {marker} [cyan]{d.skill_name}[/cyan]  "
                    f"[dim]conf={d.adjusted_confidence:.2f}[/dim]"
                )
            for d in rejected:
                console.print(
                    f"  [dim]✗[/dim] [dim]{d.skill_name}[/dim]  "
                    f"[red]{d.reason}[/red]"
                )

            if not dry_run and approved:
                count = await pipeline.import_approved(decisions, result.skills)
                console.print(
                    f"\n  [green]{count} skill(s) written to ProceduralMemory.[/green]"
                )

        # ── Tool adapters ────────────────────────────────────────────────────
        if result.platform_adapters:
            console.print(f"\n[bold]Tool adapters[/bold] ({len(result.platform_adapters)} found)")
            for a in result.platform_adapters:
                trust_color = {"safe": "green", "guarded": "yellow", "critical": "red"}.get(
                    a.get("trust_level", "safe"), "white"
                )
                console.print(
                    f"  [dim]·[/dim] [cyan]{a['name']}[/cyan]  "
                    f"[{trust_color}]{a.get('trust_level', 'safe').upper()}[/{trust_color}]  "
                    f"[dim]{a.get('description', '')[:60]}[/dim]"
                )
            if dry_run:
                console.print(
                    "  [dim](dry-run) Adapters listed but not installed into any session.[/dim]"
                )
            else:
                console.print(
                    "  [dim]Adapters listed. Use AdapterRegistry.from_lens_result() "
                    "in code, or place tools in loom_tools.py for auto-loading.[/dim]"
                )

        # ── Middleware patterns (informational) ──────────────────────────────
        if result.middleware_patterns:
            console.print(
                f"\n[bold]Middleware patterns[/bold] "
                f"[dim](informational — not imported)[/dim]"
            )
            for m in result.middleware_patterns:
                console.print(f"  [dim]·[/dim] {m['name']}  {m.get('description', '')[:60]}")


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
        console.print(f"[dim]  Discord notifier registered.[/dim]")

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
            f"[bold cyan]Loom Autonomy Daemon[/bold cyan]\n"
            f"Loaded [green]{n}[/green] trigger(s) from [dim]{config}[/dim]\n"
            f"Poll interval: {interval}s  |  model: {model}\n"
            f"[dim]Press Ctrl-C to stop.[/dim]",
            border_style="cyan",
        )
    )

    try:
        await daemon.start(poll_interval=float(interval))
    except KeyboardInterrupt:
        pass
    finally:
        await session.stop()
        console.print("[dim]Autonomy daemon stopped.[/dim]")


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

    console.print(Rule("[cyan]Registered Triggers[/cyan]"))
    if not triggers:
        console.print(
            "[dim]No triggers found (check autonomy.enabled in loom.toml)[/dim]"
        )
        return

    for t in triggers:
        color = "green" if t["enabled"] else "dim"
        console.print(
            f"  [{color}]{t['name']}[/{color}]  "
            f"[dim]{t['kind']}[/dim]  "
            f"trust=[yellow]{t['trust_level']}[/yellow]\n"
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
        f"[cyan]Emitted[/cyan] '{event_name}' → fired triggers: {fired or ['(none)']}"
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
            "[red]FastAPI not installed.[/red] "
            "Run:  [bold]pip install loom[api][/bold]"
        )
        raise SystemExit(1)
    console.print(
        f"[bold cyan]Loom API[/bold cyan]  "
        f"http://{host}:{port}  |  db: {db}\n"
        f"[dim]Docs: http://{host}:{port}/docs[/dim]"
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
            "[red]discord.py not installed.[/red] "
            "Run:  [bold]pip install loom[discord][/bold]"
        )
        raise SystemExit(1)

    env = _load_env()

    resolved_token = token or env.get("DISCORD_BOT_TOKEN", "")
    if not resolved_token:
        console.print("[red]No Discord bot token.[/red] Set --token or DISCORD_BOT_TOKEN in .env")
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

    info_lines = [f"[bold cyan]Loom Discord Bot[/bold cyan]  model: {model}  |  db: {db}"]
    if channel_list:
        info_lines.append(f"[dim]  Channel:  {channel_list}[/dim]")
    else:
        info_lines.append("[dim]  Channels: @mentions everywhere[/dim]")
    if user_list:
        info_lines.append(f"[dim]  Users:    {user_list}[/dim]")
    else:
        info_lines.append("[dim]  Users:    unrestricted[/dim]")

    if autonomy:
        # Resolve the notification channel: explicit flag > first bot channel > error
        resolved_notify_ch = notify_channel_id or (channel_list[0] if channel_list else 0)
        if not resolved_notify_ch:
            console.print(
                "[red]--autonomy requires a target channel.[/red] "
                "Pass --channel <id> or --notify-channel <id>."
            )
            raise SystemExit(1)
        info_lines.append(
            f"[dim]  Autonomy: [green]on[/green]  "
            f"config={autonomy_config}  notify-channel={resolved_notify_ch}[/dim]"
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
    )
    import logging
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")

    n = daemon.load_config(config_path)
    console.print(f"[dim]Autonomy: {n} trigger(s) loaded from {config_path}[/dim]")

    _background_tasks: set[asyncio.Task] = set()  # strong refs prevent GC

    async def _start_daemon_after_ready() -> None:
        # Wait for the Discord connection before the daemon begins polling,
        # so notifications can be delivered from the first fire onwards.
        await bot._client.wait_until_ready()
        console.print("[dim]Autonomy daemon started.[/dim]")
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
            "[red]MCP SDK not installed.[/red] "
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
            "[red]MCP SDK not installed.[/red] "
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
            console.print(f"[red]Failed to connect:[/red] {exc}")
            raise SystemExit(1)
        finally:
            await client.disconnect()

        if not tools:
            console.print("[yellow]No tools found on this MCP server.[/yellow]")
            return

        console.print(
            f"[bold cyan]{len(tools)} tool(s)[/bold cyan] available from "
            f"[bold]{server_spec}[/bold]:\n"
        )
        for t in tools:
            desc = t.description or "(no description)"
            console.print(f"  [green]{t.name}[/green]  [dim]{desc[:80]}[/dim]")
        console.print(
            "\n[dim]Add this server to loom.toml [[mcp.servers]] "
            "to load it automatically:[/dim]"
        )
        console.print(
            f"\n  [dim][[mcp.servers]]\n"
            f"  name    = \"remote\"\n"
            f"  command = \"{command}\"\n"
            f"  args    = {json.dumps(args)}\n"
            f"  trust_level = \"{trust}\"[/dim]"
        )

    asyncio.run(_run())


if __name__ == "__main__":
    cli()
