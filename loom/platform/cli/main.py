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
import json
import os
import time
import tomllib
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, UTC
from pathlib import Path
from typing import Any, Callable

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
from loom.core.cognition.counter_factual import CounterFactualReflector
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
from loom.core.harness.validation import SchemaValidationMiddleware
from loom.core.harness.permissions import PermissionContext, TrustLevel
from loom.core.infra import AbortController, wait_aborted
from loom.core.harness.registry import ToolRegistry
from loom.core.memory.embeddings import build_embedding_provider
from loom.core.memory.episodic import EpisodicEntry, EpisodicMemory
from loom.core.memory.procedural import ProceduralMemory
from loom.core.memory.relational import RelationalMemory
from loom.core.memory.semantic import SemanticEntry, SemanticMemory
from loom.core.memory.store import SQLiteStore
from loom.core.memory.index import MemoryIndex, MemoryIndexer
from loom.core.memory.search import MemorySearch
from loom.core.memory.session_log import SessionLog
from loom.platform.cli.tools import (
    make_exec_escape_fn,
    make_fetch_url_tool,
    make_filesystem_tools,
    make_memorize_tool,
    make_query_relations_tool,
    make_recall_tool,
    make_relate_tool,
    make_run_bash_tool,
    make_spawn_agent_tool,
    make_web_search_tool,
)
from loom.platform.cli.ui import (
    CompressDone,
    TextChunk,
    ToolBegin,
    ToolEnd,
    TurnDone,
    TurnPaused,
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
    """Compress unprocessed episodic entries to semantic facts, then delete them.

    Uses a timestamp in the semantic key so repeated compressions (mid-session
    and on close) never overwrite each other.  Episodic entries are deleted
    after a successful compression to prevent redundant re-processing.
    """
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

    # Use a timestamp suffix so repeated compressions don't overwrite each other
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    for i, fact in enumerate(facts):
        await semantic.upsert(
            SemanticEntry(
                key=f"session:{session_id}:{ts}:fact:{i}",
                value=fact,
                confidence=0.8,
                source=f"session:{session_id}",
            )
        )

    # Delete processed episodic entries so they aren't re-compressed on next stop()
    if facts:
        await episodic.delete_session(session_id)

    return len(facts)


# ---------------------------------------------------------------------------
# Think-block filter helpers
# ---------------------------------------------------------------------------


def _find_partial_tag_suffix(text: str, tag: str) -> int:
    """Return the length of the longest suffix of *text* that is a prefix of *tag*.

    Used to detect `<think>` / `</think>` tags split across streaming chunks so
    we can hold the partial tag in a lookahead buffer instead of emitting it.
    """
    for n in range(min(len(tag) - 1, len(text)), 0, -1):
        if text[-n:] == tag[:n]:
            return n
    return 0


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
    def __init__(
        self,
        model: str,
        db_path: str,
        resume_session_id: str | None = None,
        workspace: Path | None = None,
    ) -> None:
        self.model = model
        self.session_id = resume_session_id or str(uuid.uuid4())[:8]
        self._resume = resume_session_id is not None
        # Workspace root — all relative file paths resolve here; defaults to CWD
        self.workspace: Path = (workspace or Path.cwd()).resolve()
        self.router = build_router(model)

        # Build prompt stack from loom.toml [identity] config
        config = _load_loom_config()
        self._stack = PromptStack.from_config(config)
        self._episodic_compress_threshold: int = (
            config.get("memory", {}).get("episodic_compress_threshold", 30)
        )
        system_prompt = self._stack.load()

        # Inject workspace context into system prompt
        workspace_note = (
            f"\n\n## Workspace\nYour working directory is: `{self.workspace}`\n"
            "ALWAYS save files inside this directory. "
            "Use relative paths (e.g. `report.md`) which resolve to the workspace. "
            "NEVER write to `~`, `/tmp`, or paths outside the workspace unless "
            "explicitly instructed by the user."
        )
        if system_prompt:
            system_prompt = system_prompt + workspace_note
        else:
            system_prompt = workspace_note.strip()

        # OpenAI-canonical message history, seeded with composed system prompt
        self.messages: list[dict[str, Any]] = (
            [{"role": "system", "content": system_prompt}] if system_prompt else []
        )

        # Registry — run_bash + workspace-aware filesystem tools
        _strict_sandbox: bool = config.get("harness", {}).get("strict_sandbox", False)
        self._strict_sandbox = _strict_sandbox
        self.registry = ToolRegistry()
        _run_bash_tool = make_run_bash_tool(self.workspace, strict_sandbox=_strict_sandbox)
        self.registry.register(_run_bash_tool)
        _fs_tools = make_filesystem_tools(self.workspace)
        for tool in _fs_tools:
            self.registry.register(tool)

        # Permission context (SAFE tools pre-authorized)
        self.perm = PermissionContext(session_id=self.session_id)
        # run_bash is GUARDED — no pre-auth
        for tool in _fs_tools:
            if tool.trust_level == TrustLevel.SAFE:
                self.perm.authorize(tool.name)

        # Context budget (MiniMax M2.7 has 204 800 token context)
        self.budget = ContextBudget(total_tokens=204_800, compression_threshold=0.80)

        # Memory
        self._store = SQLiteStore(db_path)
        self._db = None
        self._db_ctx = None
        self._episodic: EpisodicMemory | None = None
        self._semantic: SemanticMemory | None = None
        self._procedural: ProceduralMemory | None = None
        self._relational: RelationalMemory | None = None
        self._reflection: ReflectionAPI | None = None
        self._reflector: CounterFactualReflector | None = None
        self._pipeline: MiddlewarePipeline | None = None
        self._memory_index: MemoryIndex = MemoryIndex()
        self._session_log: SessionLog | None = None
        self._turn_index: int = 0  # increments once per completed stream_turn()
        self._last_think: str = ""  # accumulated <think>…</think> content from the last turn
        self._cancel_spinner_fn: "Callable[[], None] | None" = None  # injected by CLI run loop

        # HITL pause/resume — stream_turn() checks _pause_requested at each
        # tool-batch boundary.  The consumer calls pause() / resume() / cancel().
        self._pause_requested: bool = False
        self._cancel_requested: bool = False
        self._resume_event: asyncio.Event = asyncio.Event()
        # When True, stream_turn() auto-pauses after every tool batch.
        self.hitl_mode: bool = False
        # Abort controller for cancellation of in-flight LLM streaming calls.
        self._abort = AbortController()

        # Issue #28: Predictive memory pre-fetcher — default off (opt-in)
        self._prefetch_enabled: bool = (
            config.get("session", {}).get("prefetch_enabled", False)
        )
        self._prefetch_top_n: int = (
            config.get("session", {}).get("prefetch_top_n", 3)
        )

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
        self._db_ctx = self._store.connect()
        self._db = await self._db_ctx.__aenter__()
        self._episodic = EpisodicMemory(self._db)
        emb_provider = build_embedding_provider(_load_env())
        self._semantic = SemanticMemory(self._db, embedding_provider=emb_provider)
        self._procedural = ProceduralMemory(self._db)
        self._relational = RelationalMemory(self._db)
        self._reflection = ReflectionAPI(self._episodic, self._procedural)
        self._reflector = CounterFactualReflector(
            router=self.router,
            model=self.model,
            procedural=self._procedural,
            semantic=self._semantic,
            relational=self._relational,
        )
        self._session_log = SessionLog(self._db)

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

        if not self._resume:
            await self._session_log.create_session(self.session_id, self.model)
        else:
            # Load persisted history. System message is always rebuilt fresh
            # (PromptStack + MemoryIndex) — never re-loaded from session_log.
            loaded = await self._session_log.load_messages(self.session_id)
            system_msgs = [m for m in self.messages if m["role"] == "system"]
            self.messages = system_msgs + loaded
            # Strip any incomplete tool_use sequences left by abrupt close or
            # sessions saved before the raw_message fix.
            self._sanitize_history()
            # Resume turn_index after the last saved turn
            meta = await self._session_log.get_session(self.session_id)
            self._turn_index = meta["turn_count"] if meta else 0

        # Register memory tools with injected stores
        search = MemorySearch(self._semantic, self._procedural)
        self.registry.register(make_recall_tool(search))
        self.registry.register(make_memorize_tool(self._semantic))
        self.registry.register(make_relate_tool(self._relational))
        self.registry.register(make_query_relations_tool(self._relational))

        # Register web tools (Phase 5D)
        self.registry.register(make_fetch_url_tool())
        env = _load_env()
        brave_key = env.get("brave_search_key") or env.get("BRAVE_SEARCH_KEY", "")
        if brave_key:
            self.registry.register(make_web_search_tool(brave_key))

        # Register sub-agent tool (Phase 5E)
        self.registry.register(make_spawn_agent_tool(self))

        # Plugin scan (4D): load ~/.loom/plugins/*.py + workspace loom_tools.py.
        # New plugin files require one-time GUARDED approval stored in
        # RelationalMemory; previously approved files load silently.
        await self._load_plugins()

        # LogMiddleware is omitted here: stream_turn() yields ToolBegin/ToolEnd
        # events that the UI renders, providing richer display without duplication.
        # Wire escape detector only when strict_sandbox is on — that's the
        # only case where /auto pre-authorizes EXEC tools within workspace.
        _exec_escape_fn = (
            make_exec_escape_fn(self.workspace) if self._strict_sandbox else None
        )
        self._pipeline = MiddlewarePipeline(
            [
                TraceMiddleware(on_trace=self._on_trace),
                SchemaValidationMiddleware(registry=self.registry),
                BlastRadiusMiddleware(
                    perm_ctx=self.perm,
                    confirm_fn=self._confirm_tool,
                    exec_escape_fn=_exec_escape_fn,
                ),
            ]
        )

    # ------------------------------------------------------------------
    # HITL pause / resume / cancel
    # ------------------------------------------------------------------

    def pause(self) -> None:
        """Request a pause at the next tool-batch boundary in stream_turn()."""
        self._pause_requested = True
        self._cancel_requested = False

    def resume(self) -> None:
        """Resume a paused stream_turn() — continue the loop as-is."""
        self._pause_requested = False
        self._cancel_requested = False
        self._resume_event.set()

    def resume_with(self, message: str) -> None:
        """
        Resume and inject a human message into the conversation before continuing.

        The message is appended to self.messages as a user turn so the next
        LLM call will see it as context / a redirect.
        """
        now_str = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
        self.messages.append({"role": "user", "content": f"[{now_str}]\n{message}"})
        asyncio.ensure_future(self._log_message("user", message))
        self.resume()

    def cancel(self) -> None:
        """Abandon the rest of a paused stream_turn() — yields TurnDone and exits."""
        self._abort.abort()
        self._cancel_requested = True
        self._pause_requested = False
        self._resume_event.set()

    async def stop(self) -> None:
        if self._db is None:
            return
        # Grab the connection reference and immediately clear self._db so any
        # concurrent or re-entrant call (e.g. /new → on_unmount) hits the guard above.
        db, self._db = self._db, None
        db_ctx, self._db_ctx = self._db_ctx, None
        try:
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
            if self._session_log is not None:
                first_user = next(
                    (m["content"] for m in self.messages if m["role"] == "user"), None
                )
                title = first_user[:60] if isinstance(first_user, str) else None
                await self._session_log.update_session(
                    self.session_id,
                    turn_count=self._turn_index,
                    last_active=datetime.now(UTC).isoformat(),
                    title=title,
                )
        except Exception:
            # DB connection may already be invalid when Textual cancels workers
            # during shutdown — swallow the error so the process exits cleanly.
            pass
        finally:
            try:
                if db_ctx is not None:
                    await db_ctx.__aexit__(None, None, None)
                elif db is not None:
                    await db.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Streaming agent loop
    # ------------------------------------------------------------------

    async def stream_turn(
        self,
        user_input: str,
        *,
        abort_signal: "asyncio.Event | None" = None,
    ) -> AsyncIterator[TextChunk | ToolBegin | ToolEnd | TurnPaused | TurnDone]:
        """
        Run one complete agent turn and yield typed UI events.

        Yields
        ------
        TextChunk   — each fragment of streaming LLM text
        ToolBegin   — just before a tool call executes
        ToolEnd     — just after a tool call finishes
        TurnDone    — once all tool loops are resolved
        """
        # Prepend current datetime so the LLM always has temporal context.
        # The UI shows the original user_input; the history gets the annotated version.
        now_str = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
        annotated = f"[{now_str}]\n{user_input}"
        self.messages.append({"role": "user", "content": annotated})
        asyncio.ensure_future(self._log_message("user", annotated))

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

        # Guard against corrupted history before sending to the API.
        self._sanitize_history()

        tools = self.registry.to_openai_schema()
        tool_count = 0
        input_tokens = 0
        output_tokens = 0
        t0 = time.monotonic()

        # Think-block filter state — persists across the whole turn so multi-step
        # reasoning (think → tool use → think again) is handled correctly.
        _think_in = False       # currently inside <think>…</think>?
        _think_shown = False    # emitted the collapsed indicator for this streaming call?
        _tbuf = ""              # partial-tag lookahead buffer
        _think_parts: list[str] = []  # accumulates think content for /think command

        while True:
            # Check abort signal at top of each LLM call iteration.
            # abort_signal is external (e.g. from AutonomyDaemon);
            # self._abort.signal is internal (from cancel()).
            sig = abort_signal if abort_signal is not None else self._abort.signal
            if sig.is_set():
                break
            response: Any = None
            _think_shown = False  # reset per streaming call (each tool round can think again)

            # Repair any orphaned tool_call entries before each LLM call.
            # This handles mid-turn cancellation (e.g. /stop while awaiting a
            # Discord confirm button) that leaves the assistant message without
            # matching tool results, which would produce a 2013 API error.
            self._sanitize_history()

            async for chunk, final in self.router.stream_chat(
                model=self.model,
                messages=self.messages,
                tools=tools,
                max_tokens=8096,
                abort_signal=abort_signal,
            ):
                if final is None:
                    if chunk:
                        _tbuf += chunk
                        out_parts: list[str] = []
                        while _tbuf:
                            if _think_in:
                                close_idx = _tbuf.find("</think>")
                                if close_idx >= 0:
                                    _think_parts.append(_tbuf[:close_idx])
                                    _think_in = False
                                    _tbuf = _tbuf[close_idx + len("</think>"):]
                                    if not _think_shown:
                                        out_parts.append("▸ thinking…\n")
                                        _think_shown = True
                                else:
                                    # Partial </think> at end? Accumulate up to lookahead.
                                    partial = _find_partial_tag_suffix(_tbuf, "</think>")
                                    if partial:
                                        _think_parts.append(_tbuf[: len(_tbuf) - partial])
                                        _tbuf = _tbuf[len(_tbuf) - partial :]
                                    else:
                                        _think_parts.append(_tbuf)
                                        _tbuf = ""
                                    break
                            else:
                                open_idx = _tbuf.find("<think>")
                                if open_idx >= 0:
                                    before = _tbuf[:open_idx]
                                    if before:
                                        out_parts.append(before)
                                    _think_in = True
                                    _tbuf = _tbuf[open_idx + len("<think>"):]
                                else:
                                    # Partial <think> at end? Keep lookahead.
                                    partial = _find_partial_tag_suffix(_tbuf, "<think>")
                                    if partial:
                                        before = _tbuf[: len(_tbuf) - partial]
                                        if before:
                                            out_parts.append(before)
                                        _tbuf = _tbuf[len(_tbuf) - partial :]
                                    else:
                                        out_parts.append(_tbuf)
                                        _tbuf = ""
                                    break
                        text = "".join(out_parts)
                        if text:
                            yield TextChunk(text=text)
                else:
                    response = final

            # Flush any buffered non-think content after each streaming call ends.
            if _tbuf and not _think_in:
                yield TextChunk(text=_tbuf)
                _tbuf = ""

            if response is None:
                break

            # Replace (not accumulate) — input_tokens is the total context this call.
            self.budget.record_response(response.input_tokens, response.output_tokens)
            self.messages.append(response.raw_message)
            input_tokens = response.input_tokens  # report latest actual value
            output_tokens += response.output_tokens

            # Log the full raw_message as JSON so tool_calls are preserved for resume.
            # New path: raw_message goes into the dedicated `raw_json` column.
            # The content field gets a plain-text representation for human readability
            # (e.g. in observability queries). Legacy format=raw_message flag is
            # preserved so old rows can still be replayed without migration.
            _raw_json_str = json.dumps(response.raw_message, ensure_ascii=False)
            _content_text = (
                response.text or "[tool_use]"
            )
            asyncio.ensure_future(self._log_message(
                "assistant",
                _content_text,
                {"format": "raw_message"},
                raw_json=_raw_json_str,
            ))

            if response.stop_reason == "end_turn":
                self._turn_index += 1

                # Mid-session episodic compression: configurable via loom.toml
                # [memory] episodic_compress_threshold (default 30).
                try:
                    ep_count = await self._episodic.count_session(self.session_id)
                    if ep_count >= self._episodic_compress_threshold:
                        fact_count = await compress_session(
                            self.session_id, self._episodic, self._semantic,
                            self.router, self.model,
                        )
                        if fact_count:
                            yield CompressDone(fact_count=fact_count)
                        # Rebuild MemoryIndex so long-running sessions (Discord)
                        # see updated fact/anti-pattern counts without restarting.
                        await self._refresh_memory_index()
                except Exception:
                    pass  # never block the turn on compression failure

                self._last_think = "".join(_think_parts).strip()
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
                        tool_output = str(result.output) if result.success else (result.error or "")
                        yield ToolEnd(
                            name=tu.name,
                            success=result.success,
                            output=tool_output[:200],
                            duration_ms=duration_ms,
                            call_id=tu.id,
                        )
                        self.messages.append(
                            self.router.format_tool_result(
                                self.model, tu.id, tool_output, result.success,
                            )
                        )
                        asyncio.ensure_future(self._log_message(
                            "tool", tool_output[:500],
                            {"tool_call_id": tu.id, "tool_name": tu.name},
                        ))
                else:
                    # Sequential: single tool, or needs interactive confirmation
                    for tu in response.tool_uses:
                        yield ToolBegin(name=tu.name, args=tu.args, call_id=tu.id)
                        ts = time.monotonic()
                        try:
                            result = await self._dispatch(tu.name, tu.args, tu.id)
                        except Exception as _dispatch_exc:
                            # An unexpected exception in dispatch (e.g. TUI confirm crash)
                            # must still produce a tool result message — otherwise the
                            # assistant's tool_calls entry becomes orphaned and the next
                            # API call gets a 2013 "tool id not found" error.
                            result = ToolResult(
                                call_id=tu.id,
                                tool_name=tu.name,
                                success=False,
                                error=f"Internal dispatch error: {_dispatch_exc}",
                                failure_type="execution_error",
                            )
                        duration_ms = (time.monotonic() - ts) * 1000
                        tool_count += 1
                        tool_output = str(result.output) if result.success else (result.error or "")
                        yield ToolEnd(
                            name=tu.name,
                            success=result.success,
                            output=tool_output[:200],
                            duration_ms=duration_ms,
                            call_id=tu.id,
                        )
                        self.messages.append(
                            self.router.format_tool_result(
                                self.model, tu.id, tool_output, result.success,
                            )
                        )
                        asyncio.ensure_future(self._log_message(
                            "tool", tool_output[:500],
                            {"tool_call_id": tu.id, "tool_name": tu.name},
                        ))

                # Check budget after tool results are appended — the next LLM
                # call in this loop will include them and may push over the limit.
                if self.budget.should_compress():
                    console.print(
                        f"[yellow]  Context at {self.budget.usage_fraction * 100:.0f}%"
                        f" mid-turn — compressing…[/yellow]"
                    )
                    await self._smart_compact()

                # ── HITL check point ─────────────────────────────────────
                # Fires after every tool batch, before the next LLM call.
                # pause() sets _pause_requested; hitl_mode auto-sets it each batch.
                if self.hitl_mode:
                    self._pause_requested = True
                if self._pause_requested:
                    self._pause_requested = False
                    self._resume_event.clear()
                    yield TurnPaused(tool_count_so_far=tool_count)
                    await self._resume_event.wait()
                    self._resume_event.clear()
                    if self._cancel_requested:
                        self._cancel_requested = False
                        self._last_think = "".join(_think_parts).strip()
                        self._turn_index += 1
                        yield TurnDone(
                            tool_count=tool_count,
                            input_tokens=input_tokens,
                            output_tokens=output_tokens,
                            elapsed_ms=(time.monotonic() - t0) * 1000,
                        )
                        return
            else:
                break

        self._last_think = "".join(_think_parts).strip()
        self._turn_index += 1
        yield TurnDone(
            tool_count=tool_count,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            elapsed_ms=(time.monotonic() - t0) * 1000,
        )

    # ------------------------------------------------------------------
    # Plugin loader
    # ------------------------------------------------------------------

    async def _load_plugins(self) -> None:
        """
        Discover and load plugins from two locations (in order):

        1. ``~/.loom/plugins/*.py``  — global plugin directory; Loom itself
           can write new plugins here via write_file.
        2. ``<workspace>/loom_tools.py`` — local convenience file (backward
           compatible; treated as a plugin bundle).

        Safety gate
        -----------
        The first time a plugin file is seen (not in RelationalMemory), the
        user is prompted to approve it.  Approval is stored as:
            (subject="plugin:<stem>", predicate="approved", object="true")
        Subsequent sessions load approved plugins silently.
        GUARDED trust — user must explicitly allow unknown code.
        """
        import importlib.util as _ilu
        import loom as _loom_pkg

        plugin_dir = Path("~/.loom/plugins").expanduser()
        plugin_dir.mkdir(parents=True, exist_ok=True)

        # Collect candidate files: global plugins first, then local loom_tools.py
        candidates: list[tuple[Path, str]] = []
        for p in sorted(plugin_dir.glob("*.py")):
            candidates.append((p, f"plugin:{p.stem}"))
        local = self.workspace / "loom_tools.py"
        if local.exists():
            candidates.append((local, f"plugin:loom_tools"))

        if not candidates:
            return

        for plugin_path, rel_key in candidates:
            approved = False
            if self._relational is not None:
                entry = await self._relational.get(rel_key, "approved")
                approved = entry is not None and entry.object == "true"

            if not approved:
                # First-time: show file summary and ask for confirmation
                console.print(
                    f"\n[yellow]  New plugin:[/yellow] [bold]{plugin_path.name}[/bold]"
                )
                console.print(f"  [dim]{plugin_path}[/dim]")
                try:
                    lines = plugin_path.read_text(encoding="utf-8").splitlines()
                    preview = "\n".join(f"    {l}" for l in lines[:12])
                    if len(lines) > 12:
                        preview += f"\n    … ({len(lines) - 12} more lines)"
                    console.print(f"[dim]{preview}[/dim]")
                except Exception:
                    pass

                allow = Confirm.ask(
                    "  [yellow]Allow this plugin?[/yellow]", default=False
                )
                if not allow:
                    console.print(f"  [dim]Skipped {plugin_path.name}[/dim]")
                    continue

                # Persist approval
                if self._relational is not None:
                    from loom.core.memory.relational import RelationalEntry
                    await self._relational.upsert(RelationalEntry(
                        subject=rel_key,
                        predicate="approved",
                        object="true",
                        source="user",
                    ))

            # Load the file — this executes @loom.tool / loom.register_plugin() calls
            # No reset needed: ToolRegistry/PluginRegistry use name-keyed dicts, so
            # re-registration is a safe upsert. Resetting would wipe tools registered
            # by earlier plugin files in the same _load_plugins() loop.
            try:
                _spec = _ilu.spec_from_file_location(plugin_path.stem, plugin_path)
                if _spec and _spec.loader:
                    _mod = _ilu.module_from_spec(_spec)
                    _spec.loader.exec_module(_mod)  # type: ignore[union-attr]
            except Exception as exc:
                console.print(f"  [red]Error loading {plugin_path.name}: {exc}[/red]")
                continue

            # Install @loom.tool tools
            tool_count = _loom_pkg._get_default_registry().install_into(self.registry)

            # Install LoomPlugin instances
            plugin_summary = _loom_pkg._get_default_plugin_registry().install_into(self)
            plugin_count = sum(plugin_summary.values())
            named_plugins = [n for n in plugin_summary if n != "(anonymous)"]

            total = tool_count + plugin_count
            if total:
                detail = ""
                if named_plugins:
                    detail = f"  [dim]({', '.join(named_plugins)})[/dim]"
                console.print(
                    f"  [dim]✓ {plugin_path.name} — {total} tool(s) loaded{detail}[/dim]"
                )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _sanitize_history(self) -> None:
        """Remove incomplete tool_use sequences and fix malformed tool args.

        Two passes:
        1. Fix any tool_calls whose ``arguments`` field is not valid JSON
           (can happen when MiniMax truncates a streaming response mid-JSON).
           Re-serialize from an empty dict so the API accepts the message.
        2. Trim any assistant message whose tool_calls are not all followed by
           matching tool result messages (orphaned tool_calls → 2013 error).
        """
        msgs = self.messages

        # Pass 1: repair invalid arguments JSON in-place
        for msg in msgs:
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    fn = tc.get("function", {})
                    raw_args = fn.get("arguments", "{}")
                    try:
                        json.loads(raw_args)
                    except (json.JSONDecodeError, ValueError):
                        fn["arguments"] = "{}"

        # Pass 2: trim orphaned tool_call sequences
        i = len(msgs) - 1
        while i >= 0:
            msg = msgs[i]
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                expected_ids = {tc["id"] for tc in msg["tool_calls"]}
                found_ids: set[str] = set()
                j = i + 1
                while j < len(msgs) and msgs[j].get("role") == "tool":
                    tid = msgs[j].get("tool_call_id")
                    if tid in expected_ids:
                        found_ids.add(tid)
                    j += 1
                if found_ids != expected_ids:
                    self.messages = msgs[:i]
                    return
            i -= 1

    async def _log_message(
        self, role: str, content: str, metadata: dict | None = None,
        raw_json: str | None = None,
    ) -> None:
        """Fire-and-forget session_log write. Exceptions are swallowed inside log_message."""
        if self._session_log is None:
            return
        await self._session_log.log_message(
            self.session_id, self._turn_index, role, content, metadata or {},
            raw_json=raw_json,
        )

    async def _dispatch(self, tool_name: str, args: dict, call_id: str) -> ToolResult:
        tool_def = self.registry.get(tool_name)
        if tool_def is None:
            return ToolResult(
                call_id=call_id,
                tool_name=tool_name,
                success=False,
                error=f"Unknown tool: {tool_name}",
                failure_type="tool_not_found",
            )
        call = ToolCall(
            id=call_id,
            tool_name=tool_name,
            args=args,
            trust_level=tool_def.trust_level,
            capabilities=tool_def.capabilities,
            session_id=self.session_id,
            abort_signal=self._abort.signal,
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

        meta: dict[str, Any] = {
            "tool_name": call.tool_name,
            "success": result.success,
            "duration_ms": result.duration_ms,
        }
        if result.failure_type:
            meta["failure_type"] = result.failure_type
        await self._episodic.write(
            EpisodicEntry(
                session_id=self.session_id,
                event_type="tool_result",
                content=summary,
                metadata=meta,
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

        # Counter-factual reflection: fire-and-forget for execution_error failures.
        # Only triggers when a SkillGenome exists for the tool (checked inside reflector).
        if (
            self._reflector is not None
            and not result.success
            and result.failure_type == "execution_error"
        ):
            self._reflector.maybe_reflect(call, result, self.session_id)

    async def _confirm_tool(self, call: ToolCall) -> bool:
        # Stop any running spinner before printing the confirm panel so the
        # spinner animation doesn't overwrite the prompt input line.
        if self._cancel_spinner_fn is not None:
            self._cancel_spinner_fn()
            console.print(clear_line_escape(), end="")  # clear spinner line
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

        When ``_prefetch_enabled`` is True, a lightweight MemorySearch query is
        run before tool execution and results are injected as an ephemeral system
        message.  The message is removed immediately after tools complete so it
        never pollutes the persistent history.

        Returns
        -------
        list of (ToolUse, ToolResult, duration_ms) tuples.
        """
        from loom.core.tasks.graph import TaskGraph, TaskNode
        from loom.core.tasks.scheduler import TaskScheduler

        # Issue #28: pre-fetch relevant memory before executing tool batch
        _ephemeral_idx: int | None = None
        if self._prefetch_enabled and self._semantic is not None and self._procedural is not None:
            try:
                _query = " ".join(
                    tu.name + " " + " ".join(str(v) for v in tu.args.values())[:120]
                    for tu in tool_uses
                ).strip()[:300]
                if _query:
                    _search = MemorySearch(self._semantic, self._procedural)
                    _hits = await _search.recall(_query, limit=self._prefetch_top_n)
                    if _hits:
                        _snippets = "\n".join(
                            f"- {h.key}: {h.value[:200]}" for h in _hits
                        )
                        _ephemeral = (
                            "[Pre-loaded context for upcoming tools]\n"
                            f"{_snippets}"
                        )
                        # Insert ephemeral message just before the last assistant message
                        _insert_pos = len(self.messages)
                        self.messages.insert(_insert_pos, {
                            "role": "system",
                            "content": _ephemeral,
                            "_ephemeral": True,  # marker for removal
                        })
                        _ephemeral_idx = _insert_pos
            except Exception:
                pass  # pre-fetch must never block tool execution

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

        # Remove the ephemeral message — keep history clean
        if _ephemeral_idx is not None:
            try:
                if (
                    _ephemeral_idx < len(self.messages)
                    and self.messages[_ephemeral_idx].get("_ephemeral")
                ):
                    self.messages.pop(_ephemeral_idx)
            except Exception:
                pass

        return [(tu, timings[node.id][1], timings[node.id][0]) for tu, node in pairs]

    async def _refresh_memory_index(self) -> None:
        """
        Rebuild MemoryIndex and update the system prompt in-place.

        Called after mid-session episodic compression so long-running sessions
        (e.g. Discord bots that never restart) see fresh fact/anti-pattern counts
        without waiting for the next session start.

        Finds the existing MemoryIndex block in messages[0] by the sentinel line
        "Memory Index" and replaces everything from that line to the closing rule
        line. If no existing block is found, appends the new block as usual.
        """
        if self._semantic is None or self._procedural is None:
            return
        try:
            indexer = MemoryIndexer(
                self._semantic, self._procedural, self._episodic, self._relational
            )
            new_index = await indexer.build()
            if new_index.is_empty:
                return
            self._memory_index = new_index
            new_text = new_index.render()

            if not self.messages or self.messages[0]["role"] != "system":
                return

            current = self.messages[0]["content"]
            sentinel = "Memory Index\n"
            pos = current.find(sentinel)
            if pos != -1:
                # Replace from "Memory Index\n" to end of the block.
                # The block ends after the second rule line (─────…).
                # Find the second occurrence of the rule separator after pos.
                rule = "─" * 45
                first_rule = current.find(rule, pos)
                second_rule = current.find(rule, first_rule + len(rule)) if first_rule != -1 else -1
                if second_rule != -1:
                    # Keep everything before the block and after the closing rule line.
                    end = second_rule + len(rule)
                    # Also swallow trailing newlines and hint lines until next blank line.
                    tail = current[end:]
                    # Drop the hint lines that were part of the old block.
                    lines = tail.splitlines(keepends=True)
                    skip = 0
                    for line in lines:
                        stripped = line.strip()
                        if stripped.startswith("Use ") or stripped == "":
                            skip += len(line)
                        else:
                            break
                    self.messages[0]["content"] = (
                        current[:pos].rstrip("\n") + "\n\n" + new_text + current[end + skip:]
                    )
                else:
                    # Fallback: just replace from sentinel to end
                    self.messages[0]["content"] = current[:pos].rstrip("\n") + "\n\n" + new_text
            else:
                # No existing block — append
                self.messages[0]["content"] += f"\n\n{new_text}"
        except Exception:
            pass  # never block the session on a MemoryIndex refresh failure

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

    elif command == "/verbose":
        from loom.platform.cli.ui import toggle_verbose_mode, is_verbose_mode

        state = toggle_verbose_mode()
        console.print(
            f"[dim]Tool output: {'[green]verbose[/green]' if state else '[yellow]compact[/yellow]'}[/dim]"
        )

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
                "  [yellow]/personality[/yellow] [dim]<name>[/dim]      Switch cognitive persona\n"
                "  [yellow]/personality off[/yellow]           Remove active persona\n"
                "  [yellow]/think[/yellow]                     View last turn's reasoning chain\n"
                "  [yellow]/compact[/yellow]                   Compress older context\n"
                "  [yellow]/verbose[/yellow]                   Toggle tool output verbosity\n"
                "  [yellow]/auto[/yellow]                      Toggle run_bash auto-approve (requires strict_sandbox)\n"
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
        )
        from loom.platform.cli.ui import (
            TextChunk,
            ToolBegin,
            ToolEnd,
            TurnDone,
            TurnPaused,
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

    elif command == "/think":
        think = session._last_think
        if think:
            from loom.platform.cli.tui.components.think_modal import ThinkModal
            await app.push_screen_wait(ThinkModal(think))
        else:
            app.notify("No reasoning chain captured for the last turn.", severity="information")

    elif command == "/compact":
        pct = session.budget.usage_fraction * 100
        app.notify(f"Compacting context ({pct:.1f}% used)…")
        await session._smart_compact()
        app.notify("Context compacted.")

    elif command == "/verbose":
        app.action_toggle_verbose()

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

        async with session._store.connect() as conn:
            rows = await _SL(conn).list_sessions(limit=20)
        selected = await app.push_screen_wait(SessionPickerModal(rows))
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
        # shows a ModalScreen dialog — no terminal suspension needed.
        from loom.core.harness.middleware import BlastRadiusMiddleware
        from loom.platform.cli.tui.components.interactive_widgets import InlineConfirmWidget
        import asyncio

        async def _tui_confirm(call: "ToolCall") -> bool:
            args_preview = "  ".join(
                f"{k}={str(v)[:40]}" for k, v in call.args.items()
            )[:120]
            
            msg_list = app.query_one("#message-list")
            future = asyncio.Future()
            widget = InlineConfirmWidget(
                tool_name=call.tool_name,
                trust_label=call.trust_level.plain,
                args_preview=args_preview,
                future=future
            )
            msg_list.mount(widget)
            msg_list.scroll_end(animate=False)
            
            return await future

        for mw in session._pipeline._middlewares:
            if isinstance(mw, BlastRadiusMiddleware):
                mw._confirm = _tui_confirm
                break

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

    # Give the session a handle to cancel the spinner before confirm prompts.
    session._cancel_spinner_fn = _cancel_spinner

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

            elif isinstance(event, TurnPaused):
                # ── HITL pause ────────────────────────────────────────────
                _cancel_spinner()
                console.print(clear_line_escape(), end="")
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
@click.option("--model", default="MiniMax-M2.7", show_default=True)
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

    daemon = AutonomyDaemon(
        notify_router=notify_router,
        confirm_flow=confirm_flow,
        loom_session=session,
    )
    import logging
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")

    n = daemon.load_config(config_path)
    console.print(f"[dim]Autonomy: {n} trigger(s) loaded from {config_path}[/dim]")

    async def _start_daemon_after_ready() -> None:
        # Wait for the Discord connection before the daemon begins polling,
        # so notifications can be delivered from the first fire onwards.
        await bot._client.wait_until_ready()
        console.print("[dim]Autonomy daemon started.[/dim]")
        asyncio.ensure_future(daemon.start(poll_interval=float(interval)))

    try:
        async with bot._client:
            asyncio.ensure_future(_start_daemon_after_ready())
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
@click.option("--model", default="MiniMax-M2.7", show_default=True,
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
            await run_mcp_server(session.registry)
        finally:
            await session.stop()

    asyncio.run(_run())


@mcp_cmd.command("connect")
@click.argument("server_spec")
@click.option("--trust", default="safe", show_default=True,
              type=click.Choice(["safe", "guarded"], case_sensitive=False),
              help="Trust level for imported tools.")
@click.option("--db", default="~/.loom/memory.db", show_default=True)
@click.option("--model", default="MiniMax-M2.7", show_default=True)
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
