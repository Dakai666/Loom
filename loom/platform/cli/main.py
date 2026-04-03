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
import time
import tomllib
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import click
from dotenv import dotenv_values
from rich.console import Console

# Force UTF-8 output on Windows so the Rich console can render full Unicode.
import sys as _sys

if _sys.platform == "win32":
    try:
        _sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        _sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, Exception):
        pass
from rich.markdown import Markdown
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.rule import Rule

from loom.core.cognition.context import ContextBudget
from loom.core.cognition.prompt_stack import PromptStack
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
from loom.core.memory.embeddings import build_embedding_provider
from loom.core.memory.episodic import EpisodicEntry, EpisodicMemory
from loom.core.memory.procedural import ProceduralMemory
from loom.core.memory.relational import RelationalMemory
from loom.core.memory.semantic import SemanticEntry, SemanticMemory
from loom.core.memory.store import SQLiteStore
from loom.core.memory.index import MemoryIndex, MemoryIndexer
from loom.core.memory.search import MemorySearch
from loom.platform.cli.tools import (
    BUILTIN_TOOLS,
    make_memorize_tool,
    make_query_relations_tool,
    make_recall_tool,
    make_relate_tool,
)
from loom.platform.cli.ui import (
    TextChunk,
    ToolBegin,
    ToolEnd,
    TurnDone,
    clear_line_escape,
    is_verbose_mode,
    make_prompt_session,
    render_cursor,
    render_header,
    status_bar,
    tool_begin_line,
    tool_end_line,
    tool_end_verbose_line,
    tool_running_line,
)

console = Console(highlight=False)

# ---------------------------------------------------------------------------
# Session compression (episodic → semantic)
# ---------------------------------------------------------------------------

COMPRESS_PROMPT = """\
Below are tool calls from an agent session.
Extract 3-7 concise, reusable facts or learnings that would be valuable in future sessions.
Format each on its own line starting with "FACT: ".
Ignore trivial or highly session-specific details.
IMPORTANT: Write every FACT in the same language as the session content below.

Session log:
{log}
"""

COMPACT_PROMPT = """\
You are summarizing an AI assistant conversation for context compression.
Produce a concise but complete summary that preserves all context the assistant needs to continue seamlessly.

Include:
- Key facts, decisions, and outcomes discussed
- Tool calls made and their results (brief)
- Any ongoing tasks or user goals
- Important context established

Output as flowing prose. Be dense and accurate. Do not include any preamble.
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

    log_text = "\n".join(f"[{e.event_type}] {e.content}" for e in entries[:60])

    response = await router.chat(
        model=model,
        messages=[{"role": "user", "content": COMPRESS_PROMPT.format(log=log_text)}],
        max_tokens=1024,
    )

    raw = response.text or ""
    facts = [
        line[len("FACT:") :].strip()
        for line in raw.splitlines()
        if line.strip().startswith("FACT:")
    ]
    # Fallback: if the LLM didn't use FACT: prefixes, save the raw text as one fact.
    if not facts and raw.strip():
        facts = [raw.strip()[:800]]

    for i, fact in enumerate(facts):
        await semantic.upsert(
            SemanticEntry(
                key=f"session:{session_id}:fact:{i}",
                value=fact,
                confidence=0.8,
                source=f"session:{session_id}",
            )
        )

    return len(facts)


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def _load_loom_config() -> dict:
    """Load loom.toml from cwd or the package root; return {} on miss."""
    candidates = [
        Path.cwd() / "loom.toml",
        Path(__file__).parents[3] / "loom.toml",
    ]
    for path in candidates:
        if path.exists():
            with open(path, "rb") as fh:
                return tomllib.load(fh)
    return {}


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
        router.register(
            MiniMaxProvider(api_key=minimax_key, model=mm_model), default=True
        )

    # Anthropic — fallback
    anthropic_key = env.get("ANTHROPIC_API_KEY") or os.environ.get(
        "ANTHROPIC_API_KEY", ""
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

        # Build prompt stack from loom.toml [identity] config
        config = _load_loom_config()
        self._stack = PromptStack.from_config(config)
        system_prompt = self._stack.load()

        # OpenAI-canonical message history, seeded with composed system prompt
        self.messages: list[dict[str, Any]] = (
            [{"role": "system", "content": system_prompt}] if system_prompt else []
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
        self._relational: RelationalMemory | None = None
        self._reflection: ReflectionAPI | None = None
        self._pipeline: MiddlewarePipeline | None = None
        self._memory_index: MemoryIndex = MemoryIndex()

    # ------------------------------------------------------------------
    # Personality management
    # ------------------------------------------------------------------

    def switch_personality(self, name: str) -> bool:
        """
        Switch to a named personality and update the system message.
        Pass ``"off"`` to remove the personality layer.
        Returns True on success.
        """
        if name == "off":
            self._stack.clear_personality()
        else:
            if not self._stack.switch_personality(name):
                return False

        new_prompt = self._stack.composed_prompt
        if self.messages and self.messages[0]["role"] == "system":
            if new_prompt:
                self.messages[0]["content"] = new_prompt
            else:
                self.messages.pop(0)
        elif new_prompt:
            self.messages.insert(0, {"role": "system", "content": new_prompt})
        return True

    @property
    def current_personality(self) -> str | None:
        return self._stack.current_personality

    async def start(self) -> None:
        await self._store.initialize()
        self._db = await self._store.connect().__aenter__()
        self._episodic = EpisodicMemory(self._db)
        emb_provider = build_embedding_provider(_load_env())
        self._semantic = SemanticMemory(self._db, embedding_provider=emb_provider)
        self._procedural = ProceduralMemory(self._db)
        self._relational = RelationalMemory(self._db)
        self._reflection = ReflectionAPI(self._episodic, self._procedural)

        # Build MemoryIndex and inject into system prompt
        indexer = MemoryIndexer(
            self._semantic, self._procedural, self._episodic, self._relational
        )
        self._memory_index = await indexer.build()
        if not self._memory_index.is_empty:
            index_text = self._memory_index.render()
            if self.messages and self.messages[0]["role"] == "system":
                self.messages[0]["content"] += f"\n\n{index_text}"
            else:
                self.messages.insert(0, {"role": "system", "content": index_text})

        # Register memory tools with injected stores
        search = MemorySearch(self._semantic, self._procedural)
        self.registry.register(make_recall_tool(search))
        self.registry.register(make_memorize_tool(self._semantic))
        self.registry.register(make_relate_tool(self._relational))
        self.registry.register(make_query_relations_tool(self._relational))

        # LogMiddleware is omitted here: stream_turn() yields ToolBegin/ToolEnd
        # events that the UI renders, providing richer display without duplication.
        self._pipeline = MiddlewarePipeline(
            [
                TraceMiddleware(on_trace=self._on_trace),
                BlastRadiusMiddleware(
                    perm_ctx=self.perm, confirm_fn=self._confirm_tool
                ),
            ]
        )

    async def stop(self) -> None:
        if self._db is None:
            return
        console.print(Rule("[dim]Compressing session to memory…[/dim]"))
        count = await compress_session(
            self.session_id,
            self._episodic,
            self._semantic,
            self.router,
            self.model,
        )
        if count:
            console.print(f"[dim]  Saved {count} fact(s) to semantic memory.[/dim]")
        await self._db.close()

    # ------------------------------------------------------------------
    # Streaming agent loop
    # ------------------------------------------------------------------

    async def stream_turn(
        self, user_input: str
    ) -> AsyncIterator[TextChunk | ToolBegin | ToolEnd | TurnDone]:
        """
        Run one complete agent turn and yield typed UI events.

        Yields
        ------
        TextChunk   — each fragment of streaming LLM text
        ToolBegin   — just before a tool call executes
        ToolEnd     — just after a tool call finishes
        TurnDone    — once all tool loops are resolved
        """
        self.messages.append({"role": "user", "content": user_input})

        await self._episodic.write(
            EpisodicEntry(
                session_id=self.session_id,
                event_type="message",
                content=f"User: {user_input[:200]}",
            )
        )

        # Compress before the first LLM call if already over threshold.
        # (budget.used_tokens reflects the last response's actual token count,
        # so this check is accurate from turn 2 onward.)
        if self.budget.should_compress():
            console.print(
                f"[yellow]  Context at {self.budget.usage_fraction * 100:.0f}% — "
                f"compressing…[/yellow]"
            )
            await self._smart_compact()

        tools = self.registry.to_openai_schema()
        tool_count = 0
        input_tokens = 0
        output_tokens = 0
        t0 = time.monotonic()

        while True:
            response: Any = None

            async for chunk, final in self.router.stream_chat(
                model=self.model,
                messages=self.messages,
                tools=tools,
                max_tokens=8096,
            ):
                if final is None:
                    if chunk:
                        yield TextChunk(text=chunk)
                else:
                    response = final

            if response is None:
                break

            # Replace (not accumulate) — input_tokens is the total context this call.
            self.budget.record_response(response.input_tokens, response.output_tokens)
            self.messages.append(response.raw_message)
            input_tokens = response.input_tokens  # report latest actual value
            output_tokens += response.output_tokens

            if response.stop_reason == "end_turn":
                yield TurnDone(
                    tool_count=tool_count,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    elapsed_ms=(time.monotonic() - t0) * 1000,
                )
                return

            if response.stop_reason == "tool_use":
                # Attempt parallel dispatch when multiple tools are requested.
                # Falls back to sequential if any tool needs interactive confirmation
                # (GUARDED/CRITICAL not yet authorized) — parallel confirmation prompts
                # would interleave on the CLI.
                parallel = len(response.tool_uses) > 1 and self._all_authorized(
                    response.tool_uses
                )

                if parallel:
                    # Announce all tools, then run concurrently
                    for tu in response.tool_uses:
                        yield ToolBegin(name=tu.name, args=tu.args, call_id=tu.id)
                    dispatched = await self._dispatch_parallel(response.tool_uses)
                    for tu, result, duration_ms in dispatched:
                        tool_count += 1
                        yield ToolEnd(
                            name=tu.name,
                            success=result.success,
                            output=(str(result.output)[:200] if result.output else ""),
                            duration_ms=duration_ms,
                            call_id=tu.id,
                        )
                        self.messages.append(
                            self.router.format_tool_result(
                                self.model,
                                tu.id,
                                str(result.output)
                                if result.success
                                else (result.error or ""),
                                result.success,
                            )
                        )
                else:
                    # Sequential: single tool, or needs interactive confirmation
                    for tu in response.tool_uses:
                        yield ToolBegin(name=tu.name, args=tu.args, call_id=tu.id)
                        ts = time.monotonic()
                        result = await self._dispatch(tu.name, tu.args, tu.id)
                        duration_ms = (time.monotonic() - ts) * 1000
                        tool_count += 1
                        yield ToolEnd(
                            name=tu.name,
                            success=result.success,
                            output=(str(result.output)[:200] if result.output else ""),
                            duration_ms=duration_ms,
                            call_id=tu.id,
                        )
                        self.messages.append(
                            self.router.format_tool_result(
                                self.model,
                                tu.id,
                                str(result.output)
                                if result.success
                                else (result.error or ""),
                                result.success,
                            )
                        )

                # Check budget after tool results are appended — the next LLM
                # call in this loop will include them and may push over the limit.
                if self.budget.should_compress():
                    console.print(
                        f"[yellow]  Context at {self.budget.usage_fraction * 100:.0f}%"
                        f" mid-turn — compressing…[/yellow]"
                    )
                    await self._smart_compact()
            else:
                break

        yield TurnDone(
            tool_count=tool_count,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            elapsed_ms=(time.monotonic() - t0) * 1000,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _dispatch(self, tool_name: str, args: dict, call_id: str) -> ToolResult:
        tool_def = self.registry.get(tool_name)
        if tool_def is None:
            return ToolResult(
                call_id=call_id,
                tool_name=tool_name,
                success=False,
                error=f"Unknown tool: {tool_name}",
            )
        call = ToolCall(
            id=call_id,
            tool_name=tool_name,
            args=args,
            trust_level=tool_def.trust_level,
            session_id=self.session_id,
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

        await self._episodic.write(
            EpisodicEntry(
                session_id=self.session_id,
                event_type="tool_result",
                content=summary,
                metadata={
                    "tool_name": call.tool_name,
                    "success": result.success,
                    "duration_ms": result.duration_ms,
                },
            )
        )

        # Skill evaluation loop: if this tool_name matches a SkillGenome,
        # update its confidence via EMA and persist the updated genome.
        if self._procedural is not None:
            try:
                skill = await self._procedural.get(call.tool_name)
                if skill is not None:
                    skill.record_outcome(result.success)
                    await self._procedural.upsert(skill)
            except Exception:
                pass  # Never let skill accounting block the trace callback

    async def _confirm_tool(self, call: ToolCall) -> bool:
        # No Rich Live is active during _run_streaming_turn (we removed it),
        # so plain console output + prompt_toolkit prompt_async() work cleanly.
        console.print()
        console.print(
            Panel(
                f"[bold]{call.tool_name}[/bold]  {call.trust_level.label}\n"
                f"[dim]args: {call.args}[/dim]",
                title="[yellow]  Tool requires confirmation[/yellow]",
                border_style="yellow",
            )
        )
        # Use prompt_toolkit so the prompt renders correctly on all terminals.
        from prompt_toolkit import prompt as pt_prompt

        try:
            answer = await asyncio.get_event_loop().run_in_executor(
                None, pt_prompt, "Allow? [y/N]: "
            )
        except (EOFError, KeyboardInterrupt):
            answer = ""
        return answer.strip().lower() in {"y", "yes"}

    def _all_authorized(self, tool_uses: list) -> bool:
        """
        Return True only if every tool in the list is pre-authorized for
        immediate execution (no interactive confirmation required).

        SAFE tools are always authorized.
        GUARDED tools are authorized once the user has confirmed them this session.
        CRITICAL tools always require a fresh confirmation — never parallelizable.
        """
        for tu in tool_uses:
            tool_def = self.registry.get(tu.name)
            if tool_def is None:
                continue  # unknown tools fail at dispatch; don't block parallel
            if not self.perm.is_authorized(tu.name, tool_def.trust_level):
                return False
        return True

    async def _dispatch_parallel(self, tool_uses: list) -> list[tuple]:
        """
        Dispatch multiple independent tool calls concurrently via TaskGraph.

        All tool_uses are treated as independent nodes (one DAG level), so they
        execute via asyncio.gather under the TaskScheduler.  Results are returned
        in the original tool_uses order.

        Returns
        -------
        list of (ToolUse, ToolResult, duration_ms) tuples.
        """
        from loom.core.tasks.graph import TaskGraph, TaskNode
        from loom.core.tasks.scheduler import TaskScheduler

        graph = TaskGraph()
        pairs: list[tuple] = []  # (ToolUse, TaskNode)
        timings: dict[str, tuple[float, ToolResult]] = {}

        for tu in tool_uses:
            node = graph.add(tu.name, metadata={"tu": tu})
            pairs.append((tu, node))

        async def _execute(node: TaskNode) -> ToolResult:
            tu = node.metadata["tu"]
            t0 = time.monotonic()
            result = await self._dispatch(tu.name, tu.args, tu.id)
            timings[node.id] = ((time.monotonic() - t0) * 1000, result)
            return result

        plan = graph.compile()
        scheduler = TaskScheduler(executor=_execute, stop_on_failure=False)
        await scheduler.run(plan)

        return [(tu, timings[node.id][1], timings[node.id][0]) for tu, node in pairs]

    async def _smart_compact(self) -> None:
        """
        LLM-based context compaction.

        Summarizes the oldest half of the conversation into a concise summary
        pair (user summary + assistant ack), preserving semantic content while
        reducing token count.  Falls back to turn-boundary dropping if the
        summarization call fails.
        """
        system = [m for m in self.messages if m["role"] == "system"]
        non_sys = [m for m in self.messages if m["role"] != "system"]
        user_positions = [i for i, m in enumerate(non_sys) if m["role"] == "user"]

        # Need at least 3 user turns to meaningfully compact the first one
        if len(user_positions) < 3:
            await self._compress_context()
            return

        # Summarize the first half of user turns
        mid = len(user_positions) // 2
        split_at = user_positions[mid]
        to_compact = non_sys[:split_at]
        to_keep = non_sys[split_at:]

        # Render to compact to readable text for the LLM
        conv_lines: list[str] = []
        for m in to_compact:
            role = m.get("role", "?")
            content = m.get("content") or ""
            if isinstance(content, list):
                parts: list[str] = []
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type", "")
                    if btype == "text":
                        parts.append(block.get("text", ""))
                    elif btype == "tool_use":
                        parts.append(
                            f"[tool_call: {block.get('name')}({block.get('input', {})})]"
                        )
                    elif btype == "tool_result":
                        parts.append(
                            f"[tool_result: {str(block.get('content', ''))[:300]}]"
                        )
                content = " ".join(parts)
            if role == "tool":
                content = f"[tool_result]: {str(content)[:300]}"
            if m.get("tool_calls"):
                names = [tc["function"]["name"] for tc in m["tool_calls"]]
                content += f" [calls: {', '.join(names)}]"
            conv_lines.append(f"{role.upper()}: {str(content)[:600]}")

        conv_text = "\n\n".join(conv_lines)
        before_tokens = self.budget.used_tokens

        try:
            response = await self.router.chat(
                model=self.model,
                messages=[
                    {"role": "system", "content": COMPACT_PROMPT},
                    {"role": "user", "content": f"Summarize:\n\n{conv_text}"},
                ],
                max_tokens=2048,
            )
            summary = (response.text or "").strip()
        except Exception as exc:
            console.print(
                f"[dim]  Smart compact failed ({exc}), falling back to turn drop.[/dim]"
            )
            await self._compress_context()
            return

        if not summary:
            await self._compress_context()
            return

        summary_pair = [
            {
                "role": "user",
                "content": (
                    "[Earlier conversation — compacted for context efficiency]\n\n"
                    + summary
                ),
            },
            {
                "role": "assistant",
                "content": "Understood. I have the context from our earlier conversation.",
            },
        ]

        self.messages = system + summary_pair + to_keep
        self.budget.record_messages(self.messages)

        saved = before_tokens - self.budget.used_tokens
        console.print(
            f"[dim]  Context compacted: {len(to_compact)} msgs summarized "
            f"(-~{saved:,} tokens), {self.budget.usage_fraction * 100:.1f}% used.[/dim]"
        )

    async def _compress_context(self) -> None:
        """
        Fallback: drop the oldest complete turns until context is under 60%.

        Only cuts at ``role == "user"`` boundaries so we never orphan a
        ``role == "tool"`` result without its matching tool_call id.
        The system message is always preserved.
        """
        system = [m for m in self.messages if m["role"] == "system"]
        non_sys = [m for m in self.messages if m["role"] != "system"]
        user_positions = [i for i, m in enumerate(non_sys) if m["role"] == "user"]

        before = len(non_sys)
        while len(user_positions) > 1 and self.budget.usage_fraction > 0.60:
            next_user = user_positions[1]
            non_sys = non_sys[next_user:]
            self.messages = system + non_sys
            self.budget.record_messages(self.messages)
            user_positions = [i for i, m in enumerate(non_sys) if m["role"] == "user"]

        dropped = before - len(non_sys)
        if dropped:
            console.print(
                f"[dim]  Context trimmed: dropped {dropped} messages, "
                f"{len(self.messages)} remaining "
                f"({self.budget.usage_fraction * 100:.1f}%).[/dim]"
            )

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
@click.option("--model", default="MiniMax-M2.7", show_default=True)
@click.option("--db", default="~/.loom/memory.db", show_default=True)
@click.option("--tui", is_flag=True, default=False, help="Use Textual TUI interface.")
def chat(model: str, db: str, tui: bool) -> None:
    """Start an interactive agent session."""
    if tui:
        asyncio.run(_chat_tui(model, db))
    else:
        asyncio.run(_chat(model, db))


async def _chat(model: str, db: str) -> None:
    session = LoomSession(model=model, db_path=db)
    await session.start()

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


async def _handle_slash(cmd: str, session: "LoomSession") -> None:
    """Dispatch a slash command and print feedback."""
    parts = cmd.split(maxsplit=1)
    command = parts[0]
    arg = parts[1].strip() if len(parts) > 1 else ""

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

    elif command == "/compact":
        pct = session.budget.usage_fraction * 100
        console.print(f"[dim]  Compacting context ({pct:.1f}% used)…[/dim]")
        await session._smart_compact()

    elif command == "/verbose":
        from loom.platform.cli.ui import toggle_verbose_mode, is_verbose_mode

        state = toggle_verbose_mode()
        console.print(
            f"[dim]Tool output: {'[green]verbose[/green]' if state else '[yellow]compact[/yellow]'}[/dim]"
        )

    elif command == "/help":
        console.print(
            Panel(
                "[bold]Slash commands[/bold]\n\n"
                "  [cyan]/personality[/cyan] [dim]<name>[/dim]   Switch cognitive persona\n"
                "  [cyan]/personality off[/cyan]        Remove active persona\n"
                "  [cyan]/personality[/cyan]             Show active + available personas\n"
                "  [cyan]/compact[/cyan]                 Summarize older context (smart compress)\n"
                "  [cyan]/verbose[/cyan]                 Toggle tool output verbosity\n"
                "  [cyan]/help[/cyan]                    Show this message\n\n"
                "[bold]Keyboard shortcuts[/bold]\n\n"
                "  [dim]Ctrl-L[/dim]  Clear screen\n"
                "  [dim]Ctrl-O[/dim]  Toggle tool output verbosity\n"
                "  [dim]up / down[/dim]  Browse input history\n"
                "  [dim]Tab[/dim]    Autocomplete slash commands\n"
                "  [dim]exit[/dim] / [dim]Ctrl-C[/dim]  End session",
                title="[cyan]Help[/cyan]",
                border_style="cyan",
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
        )
        from loom.platform.cli.ui import (
            TextChunk,
            ToolBegin,
            ToolEnd,
            TurnDone,
        )

        class _App(LoomApp):
            def __init__(self) -> None:
                super().__init__(
                    model=session.model,
                    db_path=str(session._store.path),
                )
                self._session = session

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

                    async for ev in self._session.stream_turn(text):
                        if isinstance(ev, TextChunk):
                            await self.dispatch_stream_event(TuiChunk(text=ev.text))
                        elif isinstance(ev, ToolBegin):
                            await self.dispatch_stream_event(
                                TuiToolBegin(
                                    name=ev.name,
                                    args=ev.args,
                                    call_id=ev.call_id,
                                )
                            )
                        elif isinstance(ev, ToolEnd):
                            await self.dispatch_stream_event(
                                TuiToolEnd(
                                    name=ev.name,
                                    success=ev.success,
                                    output=ev.output,
                                    duration_ms=ev.duration_ms,
                                    call_id=ev.call_id,
                                )
                            )
                        elif isinstance(ev, TurnDone):
                            await self.dispatch_stream_event(
                                TuiTurnDone(
                                    tool_count=ev.tool_count,
                                    input_tokens=ev.input_tokens,
                                    output_tokens=ev.output_tokens,
                                    elapsed_ms=ev.elapsed_ms,
                                    context_pct=self._session.budget.usage_fraction,
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

    elif command == "/verbose":
        app.action_toggle_verbose()

    elif command == "/help":
        app.notify(
            "Commands: /personality [name|off], /compact, /verbose, /help  |  "
            "Keys: Ctrl+L clear · Ctrl+O verbose · Ctrl+W workspace"
        )

    else:
        app.notify(f"Unknown command '{command}'. Type /help.", severity="warning")


async def _chat_tui(model: str, db: str) -> None:
    """Launch the Textual TUI chat session."""
    db_path = str(Path(db).expanduser())
    session = LoomSession(model=model, db_path=db_path)
    await session.start()

    app = LoomChatApp.create(session)

    # Replace BlastRadiusMiddleware's confirm_fn with a TUI-aware version that
    # suspends the Textual app, prompts in the raw terminal, then resumes.
    from loom.core.harness.middleware import BlastRadiusMiddleware
    from prompt_toolkit import prompt as pt_prompt

    async def _tui_confirm(call: ToolCall) -> bool:
        async with app.suspend():
            console.print()
            console.print(
                Panel(
                    f"[bold]{call.tool_name}[/bold]  {call.trust_level.label}\n"
                    f"[dim]args: {call.args}[/dim]",
                    title="[yellow]  Tool requires confirmation[/yellow]",
                    border_style="yellow",
                )
            )
            try:
                answer = await asyncio.get_event_loop().run_in_executor(
                    None, pt_prompt, "Allow? [y/N]: "
                )
            except (EOFError, KeyboardInterrupt):
                answer = ""
        return answer.strip().lower() in {"y", "yes"}

    for mw in session._pipeline._middlewares:
        if isinstance(mw, BlastRadiusMiddleware):
            mw._confirm = _tui_confirm
            break

    await app.run_async()


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
        console.print(clear_line_escape(), end="")
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

    try:
        async for event in session.stream_turn(user_input):
            if isinstance(event, TextChunk):
                # Clear cursor from previous position
                console.print(clear_line_escape(), end="")
                # Print text chunk
                console.print(event.text, end="", markup=False, highlight=False)
                text_buffer += event.text
                at_line_start = event.text.endswith("\n")
                # Print streaming cursor at end
                console.print(render_cursor(), end="")

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
                console.print(clear_line_escape(), end="")
                # Print tool result (verbose if enabled)
                if is_verbose_mode() and event.output:
                    console.print(
                        tool_end_verbose_line(
                            event.name, event.success, event.duration_ms, event.output
                        )
                    )
                else:
                    console.print(
                        tool_end_line(event.name, event.success, event.duration_ms)
                    )
                at_line_start = True
                active_tool = None
                console.print()

            elif isinstance(event, TurnDone):
                # Cancel any running spinner and clear cursor
                _cancel_spinner()
                console.print(clear_line_escape(), end="")
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
        console.print(clear_line_escape(), end="")
        console.print()
        console.print(f"[red]Error: {exc}[/red]")


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
@click.option(
    "--config", default="loom.toml", show_default=True, help="Path to loom.toml"
)
@click.option("--model", default="MiniMax-M2.7", show_default=True)
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
@click.option("--model", default="MiniMax-M2.7", show_default=True)
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


if __name__ == "__main__":
    cli()
