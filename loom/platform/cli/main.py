"""
Loom CLI — Phase 2 entry point.

Changes from Phase 1
--------------------
* LLM calls now go through LLMRouter (MiniMax-M2.7 by default).
* Messages stored in OpenAI-canonical format (compatible with MiniMax).
* ContextBudget tracks token usage; warns when approaching limit.
* ReflectionAPI exposed via `loom reflect` command.
* API key loaded from .env (key: minimax.io_key).

Usage
-----
    loom chat                         # MiniMax-M2.7 (default)
    loom chat --model MiniMax-M2.7-highspeed
    loom chat --model claude-sonnet-4-6
    loom memory list
    loom reflect --session <id>
"""

import asyncio
import os
import uuid
from pathlib import Path
from typing import Any

import click
from dotenv import dotenv_values
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.rule import Rule

from loom.core.cognition.context import ContextBudget
from loom.core.cognition.providers import AnthropicProvider, MiniMaxProvider
from loom.core.cognition.reflection import ReflectionAPI
from loom.core.cognition.router import LLMRouter
from loom.core.harness.middleware import (
    BlastRadiusMiddleware,
    LogMiddleware,
    MiddlewarePipeline,
    ToolCall,
    ToolResult,
    TraceMiddleware,
)
from loom.core.harness.permissions import PermissionContext, TrustLevel
from loom.core.harness.registry import ToolRegistry
from loom.core.memory.episodic import EpisodicEntry, EpisodicMemory
from loom.core.memory.procedural import ProceduralMemory
from loom.core.memory.semantic import SemanticEntry, SemanticMemory
from loom.core.memory.store import SQLiteStore
from loom.platform.cli.tools import BUILTIN_TOOLS

console = Console()

# ---------------------------------------------------------------------------
# Session compression (episodic → semantic)
# ---------------------------------------------------------------------------

COMPRESS_PROMPT = """\
Below are tool calls from an agent session.
Extract 3-7 concise, reusable facts or learnings that would be valuable in future sessions.
Format each on its own line starting with "FACT: ".
Ignore trivial or highly session-specific details.

Session log:
{log}
"""


async def compress_session(
    session_id: str,
    episodic: EpisodicMemory,
    semantic: SemanticMemory,
    router: LLMRouter,
    model: str,
) -> int:
    entries = await episodic.read_session(session_id)
    if not entries:
        return 0

    log_text = "\n".join(
        f"[{e.event_type}] {e.content}"
        for e in entries[:60]
    )

    response = await router.chat(
        model=model,
        messages=[{"role": "user", "content": COMPRESS_PROMPT.format(log=log_text)}],
        max_tokens=1024,
    )

    raw = response.text or ""
    facts = [
        line[len("FACT:"):].strip()
        for line in raw.splitlines()
        if line.strip().startswith("FACT:")
    ]

    for i, fact in enumerate(facts):
        await semantic.upsert(SemanticEntry(
            key=f"session:{session_id}:fact:{i}",
            value=fact,
            confidence=0.8,
            source=f"session:{session_id}",
        ))

    return len(facts)


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------

def _load_soul() -> str | None:
    """
    Load SOUL.md from the project root (cwd or two levels above this file).
    Returns the content as a string, or None if not found.
    """
    candidates = [
        Path.cwd() / "SOUL.md",
        Path(__file__).parents[3] / "SOUL.md",
    ]
    for path in candidates:
        if path.exists():
            return path.read_text(encoding="utf-8")
    return None


def _load_env(project_root: Path | None = None) -> dict[str, str]:
    """Load .env from project root or current directory."""
    search = [
        Path.cwd() / ".env",
        Path(__file__).parents[3] / ".env",
    ]
    if project_root:
        search.insert(0, project_root / ".env")

    for path in search:
        if path.exists():
            return dict(dotenv_values(str(path)))
    return {}


def build_router(model: str) -> LLMRouter:
    env = _load_env()
    router = LLMRouter()

    # MiniMax — primary provider
    minimax_key = (
        env.get("minimax.io_key")
        or env.get("MINIMAX_API_KEY")
        or os.environ.get("MINIMAX_API_KEY", "")
    )
    if minimax_key:
        mm_model = model if model.startswith("MiniMax") else "MiniMax-M2.7"
        router.register(MiniMaxProvider(api_key=minimax_key, model=mm_model), default=True)

    # Anthropic — fallback
    anthropic_key = (
        env.get("ANTHROPIC_API_KEY")
        or os.environ.get("ANTHROPIC_API_KEY", "")
    )
    if anthropic_key:
        ant_model = model if model.startswith("claude") else "claude-sonnet-4-6"
        router.register(AnthropicProvider(api_key=anthropic_key, model=ant_model))

    if not router.providers:
        raise RuntimeError(
            "No LLM provider configured. "
            "Add MINIMAX_API_KEY or ANTHROPIC_API_KEY to .env"
        )
    return router


# ---------------------------------------------------------------------------
# LoomSession
# ---------------------------------------------------------------------------

class LoomSession:
    def __init__(self, model: str, db_path: str) -> None:
        self.model = model
        self.session_id = str(uuid.uuid4())[:8]
        self.router = build_router(model)

        # OpenAI-canonical message history
        # Seed with SOUL.md as the system prompt if available
        soul = _load_soul()
        self.messages: list[dict[str, Any]] = (
            [{"role": "system", "content": soul}] if soul else []
        )

        # Registry
        self.registry = ToolRegistry()
        for tool in BUILTIN_TOOLS:
            self.registry.register(tool)

        # Permission context (SAFE tools pre-authorized)
        self.perm = PermissionContext(session_id=self.session_id)
        for tool in BUILTIN_TOOLS:
            if tool.trust_level == TrustLevel.SAFE:
                self.perm.authorize(tool.name)

        # Context budget (MiniMax M2.7 has 204 800 token context)
        self.budget = ContextBudget(total_tokens=204_800, compression_threshold=0.80)

        # Memory
        self._store = SQLiteStore(db_path)
        self._db = None
        self._episodic: EpisodicMemory | None = None
        self._semantic: SemanticMemory | None = None
        self._procedural: ProceduralMemory | None = None
        self._reflection: ReflectionAPI | None = None
        self._pipeline: MiddlewarePipeline | None = None

    async def start(self) -> None:
        await self._store.initialize()
        self._db = await self._store.connect().__aenter__()
        self._episodic  = EpisodicMemory(self._db)
        self._semantic  = SemanticMemory(self._db)
        self._procedural = ProceduralMemory(self._db)
        self._reflection = ReflectionAPI(self._episodic, self._procedural)

        self._pipeline = MiddlewarePipeline([
            LogMiddleware(console),
            TraceMiddleware(on_trace=self._on_trace),
            BlastRadiusMiddleware(perm_ctx=self.perm, confirm_fn=self._confirm_tool),
        ])

    async def stop(self) -> None:
        if self._db is None:
            return
        console.print(Rule("[dim]Compressing session to memory…[/dim]"))
        count = await compress_session(
            self.session_id, self._episodic, self._semantic,
            self.router, self.model,
        )
        if count:
            console.print(f"[dim]  Saved {count} fact(s) to semantic memory.[/dim]")
        await self._db.close()

    # ------------------------------------------------------------------
    # Agent loop
    # ------------------------------------------------------------------

    async def run_turn(self, user_input: str) -> str:
        self.messages.append({"role": "user", "content": user_input})
        self.budget.add(len(user_input) // 4)

        await self._episodic.write(EpisodicEntry(
            session_id=self.session_id,
            event_type="message",
            content=f"User: {user_input[:200]}",
        ))

        if self.budget.should_compress():
            console.print("[dim]  Context approaching limit — compressing…[/dim]")
            await self._compress_context()

        tools = self.registry.to_openai_schema()

        while True:
            response = await self.router.chat(
                model=self.model,
                messages=self.messages,
                tools=tools,
                max_tokens=8096,
            )

            self.budget.record_response(response.input_tokens, response.output_tokens)
            self.messages.append(response.raw_message)

            if response.stop_reason == "end_turn":
                return response.text or ""

            if response.stop_reason == "tool_use":
                for tu in response.tool_uses:
                    result = await self._dispatch(tu.name, tu.args, tu.id)
                    self.messages.append(
                        self.router.format_tool_result(
                            self.model, tu.id,
                            str(result.output) if result.success else (result.error or ""),
                            result.success,
                        )
                    )
            else:
                break

        return ""

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _dispatch(self, tool_name: str, args: dict, call_id: str) -> ToolResult:
        tool_def = self.registry.get(tool_name)
        if tool_def is None:
            return ToolResult(call_id=call_id, tool_name=tool_name,
                              success=False, error=f"Unknown tool: {tool_name}")
        call = ToolCall(
            id=call_id, tool_name=tool_name, args=args,
            trust_level=tool_def.trust_level, session_id=self.session_id,
        )
        return await self._pipeline.execute(call, tool_def.executor)

    async def _on_trace(self, call: ToolCall, result: ToolResult) -> None:
        summary = (
            f"Tool '{call.tool_name}' "
            f"({'ok' if result.success else 'failed'}, "
            f"{result.duration_ms:.0f}ms)"
        )
        if result.error:
            summary += f" — {result.error}"
        elif result.output and isinstance(result.output, str):
            summary += f" → {result.output[:120].replace(chr(10), ' ')}"

        await self._episodic.write(EpisodicEntry(
            session_id=self.session_id,
            event_type="tool_result",
            content=summary,
            metadata={
                "tool_name": call.tool_name,
                "success": result.success,
                "duration_ms": result.duration_ms,
            },
        ))

    async def _confirm_tool(self, call: ToolCall) -> bool:
        console.print(Panel(
            f"[bold]{call.tool_name}[/bold]  {call.trust_level.label}\n"
            f"[dim]args: {call.args}[/dim]",
            title="[yellow]⚠ Tool requires confirmation[/yellow]",
            border_style="yellow",
        ))
        return Confirm.ask("Allow?", default=False)

    async def _compress_context(self) -> None:
        """Keep only the last 20 messages to free context budget."""
        if len(self.messages) > 20:
            self.messages = self.messages[-20:]
        self.budget.record_messages(self.messages)

    @property
    def reflection(self) -> ReflectionAPI:
        return self._reflection


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------

@click.group()
def cli() -> None:
    """Loom — harness-first agent framework."""


@cli.command()
@click.option("--model",  default="MiniMax-M2.7", show_default=True)
@click.option("--db",     default="~/.loom/memory.db", show_default=True)
def chat(model: str, db: str) -> None:
    """Start an interactive agent session."""
    asyncio.run(_chat(model, db))


async def _chat(model: str, db: str) -> None:
    console.print(Panel(
        f"[bold cyan]Loom[/bold cyan]  [dim]v0.2.0[/dim]\n"
        f"model [green]{model}[/green]  |  memory [dim]{db}[/dim]\n"
        f"[dim]Type 'exit' or Ctrl-C to quit.[/dim]",
        border_style="cyan",
    ))

    session = LoomSession(model=model, db_path=db)
    await session.start()

    try:
        while True:
            try:
                user_input = Prompt.ask("\n[bold cyan]you[/bold cyan]")
            except (EOFError, KeyboardInterrupt):
                break

            if user_input.strip().lower() in {"exit", "quit", "q"}:
                break
            if not user_input.strip():
                continue

            console.print()
            try:
                response = await session.run_turn(user_input)
            except Exception as exc:
                console.print(f"[red]Error: {exc}[/red]")
                continue

            if response:
                console.print(Panel(
                    Markdown(response),
                    title="[bold green]loom[/bold green]",
                    border_style="green",
                ))

            # Show budget status
            console.print(
                f"[dim]  context: {session.budget.usage_fraction*100:.1f}% used[/dim]"
            )
    finally:
        await session.stop()
        console.print("\n[dim]Session ended. Goodbye.[/dim]")


# ---------------------------------------------------------------------------

@cli.group()
def memory() -> None:
    """Inspect the memory store."""


@memory.command("list")
@click.option("--db",    default="~/.loom/memory.db", show_default=True)
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
            f"  [{c}]{e.confidence:.2f}[/{c}]  [dim]{e.key}[/dim]\n"
            f"       {e.value}\n"
        )


# ---------------------------------------------------------------------------

@cli.command()
@click.option("--session", default=None, help="Session ID (latest if omitted)")
@click.option("--db",      default="~/.loom/memory.db", show_default=True)
def reflect(session: str | None, db: str) -> None:
    """Show reflection report for a session."""
    asyncio.run(_reflect(session, db))


async def _reflect(session_id: str | None, db: str) -> None:
    store = SQLiteStore(db)
    await store.initialize()
    async with store.connect() as conn:
        ep = EpisodicMemory(conn)
        pr = ProceduralMemory(conn)
        api = ReflectionAPI(ep, pr)

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
# loom autonomy commands
# ---------------------------------------------------------------------------

@cli.group()
def autonomy() -> None:
    """Manage the autonomous action engine."""


@autonomy.command("start")
@click.option("--config", default="loom.toml", show_default=True,
              help="Path to loom.toml")
@click.option("--model",  default="MiniMax-M2.7", show_default=True)
@click.option("--db",     default="~/.loom/memory.db", show_default=True)
@click.option("--interval", default=60, show_default=True,
              help="Poll interval in seconds")
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
    n = daemon.load_config(config)
    console.print(Panel(
        f"[bold cyan]Loom Autonomy Daemon[/bold cyan]\n"
        f"Loaded [green]{n}[/green] trigger(s) from [dim]{config}[/dim]\n"
        f"Poll interval: {interval}s  |  model: {model}\n"
        f"[dim]Press Ctrl-C to stop.[/dim]",
        border_style="cyan",
    ))

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
        console.print("[dim]No triggers found (check autonomy.enabled in loom.toml)[/dim]")
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
@click.option("--model",  default="MiniMax-M2.7", show_default=True)
@click.option("--db",     default="~/.loom/memory.db", show_default=True)
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
    console.print(f"[cyan]Emitted[/cyan] '{event_name}' → fired triggers: {fired or ['(none)']}")
    await session.stop()


if __name__ == "__main__":
    cli()
