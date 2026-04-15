"""Core LoomSession — the live agent runtime.

This module is the authoritative home of ``LoomSession``.  All platform
consumers (CLI, TUI, Discord) import from here.

Architecture position
---------------------
``LoomSession`` sits at the intersection of every subsystem:

    Platform  →  LoomSession  →  Cognition  →  Harness  →  Memory

It owns the main agent loop (``stream_turn``), wires all middleware, and
manages the full session lifecycle (``start`` / ``stop``).

Note on platform tool coupling
-------------------------------
``LoomSession.start()`` currently registers tools from
``loom.platform.cli.tools`` via lazy imports.  This is a known transitional
coupling: those tools belong in ``loom.core.tools`` and will be migrated in a
follow-up.  See issue #69 for context.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import tomllib
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, UTC
from pathlib import Path
from typing import Any, Callable

from dotenv import dotenv_values
from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule

from loom.core.cognition.context import ContextBudget
from loom.core.cognition.prompt_stack import PromptStack
from loom.core.cognition.providers import AnthropicProvider
from loom.core.cognition.counter_factual import CounterFactualReflector
from loom.core.cognition.reflection import ReflectionAPI
from loom.core.cognition.router import LLMRouter
from loom.core.events import (
    ActionRolledBack,
    ActionStateChange,
    CompressDone,
    EnvelopeCompleted,
    EnvelopeStarted,
    EnvelopeUpdated,
    ExecutionEnvelopeView,
    ExecutionNodeView,
    GrantSummary,
    GrantsSnapshot,
    TextChunk,
    ThinkCollapsed,
    ToolBegin,
    ToolEnd,
    TurnDone,
    TurnDropped,
    TurnPaused,
)
from loom.core.harness.lifecycle import ActionRecord, ExecutionEnvelope, LIFECYCLE_CTX_KEY
from loom.core.harness.middleware import (
    BlastRadiusMiddleware,
    LifecycleGateMiddleware,
    LifecycleMiddleware,
    MiddlewarePipeline,
    ToolCall,
    ToolResult,
    TraceMiddleware,
)
from loom.core.harness.permissions import PermissionContext, TrustLevel
from loom.core.harness.registry import ToolRegistry
from loom.core.harness.validation import SchemaValidationMiddleware
from loom.core.infra import AbortController, wait_aborted
from loom.core.memory.embeddings import build_embedding_provider
from loom.core.memory.episodic import EpisodicEntry, EpisodicMemory
from loom.core.memory.governance import MemoryGovernor
from loom.core.memory.index import MemoryIndex, MemoryIndexer, SkillCatalogEntry
from loom.core.memory.procedural import ProceduralMemory
from loom.core.memory.relational import RelationalMemory
from loom.core.memory.search import MemorySearch
from loom.core.memory.semantic import SemanticEntry, SemanticMemory
from loom.core.memory.session_log import SessionLog
from loom.core.memory.store import SQLiteStore

console = Console(highlight=False)
logger = logging.getLogger(__name__)

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
    *,
    governor: "MemoryGovernor | None" = None,
) -> int:
    """Compress unprocessed episodic entries to semantic facts, then delete them.

    Uses a timestamp in the semantic key so repeated compressions (mid-session
    and on close) never overwrite each other.  Episodic entries are deleted
    after a successful compression to prevent redundant re-processing.

    When a MemoryGovernor is provided, candidate facts are filtered through
    the admission gate before being written to semantic memory (Issue #43).
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

    # Issue #43: Admission gate — filter facts through governance
    if governor is not None and facts:
        admission_results = await governor.evaluate_admission(
            facts, source=f"session:{session_id}",
        )
        facts = [r.fact for r in admission_results if r.admitted]

    # Use a timestamp suffix so repeated compressions don't overwrite each other
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    source = f"session:{session_id}"
    for i, fact in enumerate(facts):
        entry = SemanticEntry(
            key=f"session:{session_id}:{ts}:fact:{i}",
            value=fact,
            confidence=0.8,
            source=source,
        )
        if governor is not None:
            await governor.governed_upsert(entry)
        else:
            await semantic.upsert(entry)

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
        Path(__file__).parents[2] / "loom.toml",
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
        Path(__file__).parents[2] / ".env",
    ]
    if project_root:
        search.insert(0, project_root / ".env")

    for path in search:
        if path.exists():
            return dict(dotenv_values(str(path)))
    return {}


def _parse_skill_frontmatter(raw: str) -> tuple[str, str, list[str], list[dict]]:
    """
    Parse YAML frontmatter from a SKILL.md file.

    Returns (name, description, tags, precondition_check_refs).
    On parse failure returns empty strings/lists.
    Follows Agent Skills spec lenient validation: best-effort parsing.
    """
    if not raw.startswith("---"):
        return "", "", [], []

    parts = raw.split("---", 2)
    if len(parts) < 3:
        return "", "", [], []

    yaml_text = parts[1].strip()
    try:
        import yaml
        data = yaml.safe_load(yaml_text)
        if not isinstance(data, dict):
            return "", "", [], []
    except Exception:
        return "", "", [], []

    name = str(data.get("name", "")).strip()
    description = str(data.get("description", "")).strip()
    tags = data.get("tags", [])
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",")]
    elif not isinstance(tags, list):
        tags = []

    # Issue #64 Phase B: skill-declared precondition checks
    pc_refs = data.get("precondition_checks", [])
    if not isinstance(pc_refs, list):
        pc_refs = []
    # Validate each entry has at minimum 'ref' and 'applies_to'
    valid_refs: list[dict] = []
    for entry in pc_refs:
        if isinstance(entry, dict) and entry.get("ref") and entry.get("applies_to"):
            valid_refs.append(entry)

    return name, description, tags, valid_refs


def build_router() -> LLMRouter:
    """
    Build the LLM router with all available providers registered.

    Providers are registered if their credentials / config exist.
    The session's ``model`` attribute controls which provider is active at
    runtime via ``switch_model()``.

    Cloud providers (require API keys in .env):
      MiniMax   — MINIMAX_API_KEY  (uses Anthropic-compatible endpoint, name="minimax")
      Anthropic — ANTHROPIC_API_KEY

    Local providers (no key needed; enable in loom.toml or via env):
      Ollama    — [providers.ollama] enabled=true  or  OLLAMA_BASE_URL
      LM Studio — [providers.lmstudio] enabled=true  or  LMSTUDIO_BASE_URL
    """
    from loom.core.cognition.router import get_default_model
    from loom.core.cognition.providers import OllamaProvider, LMStudioProvider

    env = _load_env()
    cfg = _load_loom_config()
    router = LLMRouter()
    default = get_default_model()

    # MiniMax — Anthropic-compatible endpoint, registered under provider name "minimax"
    # so the routing table ("MiniMax-" → "minimax") and switch_model() continue to work.
    # NOTE: MINIMAX_API_KEY also drives MiniMaxEmbeddingProvider (separate /v1 endpoint).
    minimax_key = (
        env.get("minimax.io_key")
        or env.get("MINIMAX_API_KEY")
        or os.environ.get("MINIMAX_API_KEY", "")
    )
    if minimax_key:
        mm_model = default if default.startswith("MiniMax") else "MiniMax-M2.7"
        router.register(
            AnthropicProvider(
                api_key=minimax_key,
                model=mm_model,
                base_url="https://api.minimax.io/anthropic",
                name="minimax",
                timeout=120.0,
            ),
            default=True,
        )

    # Anthropic — registered with its own default model
    anthropic_key = env.get("ANTHROPIC_API_KEY") or os.environ.get(
        "ANTHROPIC_API_KEY", ""
    )
    if anthropic_key:
        ant_model = default if default.startswith("claude") else "claude-sonnet-4-6"
        router.register(AnthropicProvider(api_key=anthropic_key, model=ant_model))

    # Ollama — local server, no API key required
    ollama_cfg = cfg.get("providers", {}).get("ollama", {})
    ollama_url = (
        env.get("OLLAMA_BASE_URL")
        or os.environ.get("OLLAMA_BASE_URL", "")
        or ollama_cfg.get("base_url", "")
    )
    if ollama_url or ollama_cfg.get("enabled", False):
        base_url = ollama_url or OllamaProvider.DEFAULT_BASE_URL
        raw_model = ollama_cfg.get("default_model", OllamaProvider.DEFAULT_MODEL)
        ollama_model = (
            default if default.startswith("ollama/")
            else f"ollama/{raw_model}"
        )
        router.register(OllamaProvider(base_url=base_url, model=ollama_model))

    # LM Studio — local server, no API key required
    lmstudio_cfg = cfg.get("providers", {}).get("lmstudio", {})
    lmstudio_url = (
        env.get("LMSTUDIO_BASE_URL")
        or os.environ.get("LMSTUDIO_BASE_URL", "")
        or lmstudio_cfg.get("base_url", "")
    )
    if lmstudio_url or lmstudio_cfg.get("enabled", False):
        base_url = lmstudio_url or LMStudioProvider.DEFAULT_BASE_URL
        raw_model = lmstudio_cfg.get("default_model", LMStudioProvider.DEFAULT_MODEL)
        lmstudio_model = (
            default if default.startswith("lmstudio/")
            else f"lmstudio/{raw_model}"
        )
        router.register(LMStudioProvider(base_url=base_url, model=lmstudio_model))

    if not router.providers:
        raise RuntimeError(
            "No LLM provider configured. "
            "Add MINIMAX_API_KEY or ANTHROPIC_API_KEY to .env, "
            "or enable a local provider in loom.toml ([providers.ollama] / [providers.lmstudio])."
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
        if model is None:
            from loom.core.cognition.router import get_default_model
            model = get_default_model()
        self._model = model
        self.session_id = resume_session_id or str(uuid.uuid4())[:8]
        self._resume = resume_session_id is not None
        # Workspace root — all relative file paths resolve here; defaults to CWD
        self.workspace: Path = (workspace or Path.cwd()).resolve()
        self.router = build_router()

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
        # NOTE: tool factories are lazy-imported from loom.platform.cli.tools.
        # This coupling will be resolved when tools migrate to loom.core.tools.
        _strict_sandbox: bool = config.get("harness", {}).get("strict_sandbox", False)
        self._strict_sandbox = _strict_sandbox
        self.registry = ToolRegistry()
        from loom.platform.cli.tools import make_run_bash_tool, make_filesystem_tools
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
        self._governor: MemoryGovernor | None = None  # Issue #43
        self._turn_index: int = 0  # increments once per completed stream_turn()
        self._current_origin: str = "interactive"  # set per-turn by stream_turn()
        self._last_think: str = ""  # accumulated <think>…</think> content from the last turn
        self._cancel_spinner_fn: "Callable[[], None] | None" = None  # injected by CLI run loop
        # Platform-swappable confirmation callback.  Defaults to CLI prompt.
        # TUI replaces with InlineConfirmWidget; Discord replaces with button view.
        # Used by BlastRadiusMiddleware AND skill check approval.
        self._confirm_fn: "Callable[[ToolCall], Any] | None" = None  # set in start()
        # Issue #56: Skill outcome tracking
        self._skill_outcome_tracker: "SkillOutcomeTracker | None" = None
        self._mcp_clients: list[Any] = []

        # HITL pause/resume — stream_turn() checks _pause_requested at each
        # tool-batch boundary.  The consumer calls pause() / resume() / cancel().
        self._pause_requested: bool = False
        self._cancel_requested: bool = False
        self._resume_event: asyncio.Event = asyncio.Event()
        # When True, stream_turn() auto-pauses after every tool batch.
        self.hitl_mode: bool = False
        # Abort controller for cancellation of in-flight LLM streaming calls.
        self._abort = AbortController()

        # Issue #42: Action lifecycle tracking
        self._current_envelope: ExecutionEnvelope | None = None
        self._lifecycle_events: asyncio.Queue = asyncio.Queue()

        # Issue #106: Envelope-centric UI — incremental counter & history
        self._envelope_counter: int = 0
        self._recent_envelopes: list[ExecutionEnvelopeView] = []

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
        emb_provider = build_embedding_provider(_load_env(), _load_loom_config())
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

        # Issue #43: Memory Governance — always-on
        _gov_cfg = _load_loom_config().get("memory", {}).get("governance", {})
        self._governor = MemoryGovernor(
            semantic=self._semantic,
            procedural=self._procedural,
            relational=self._relational,
            episodic=self._episodic,
            db=self._db,
            config=_gov_cfg,
            session_id=self.session_id,
        )
        # Issue #133: initialize health tracking and load prior session issues
        await self._governor.health.ensure_table()
        await self._governor.health.load_prior()
        # Inject health tracker into subsystems that record events
        self._semantic._health = self._governor.health
        self._session_log._health = self._governor.health

        # Build MemoryIndex and inject into system prompt
        # Issue #56: auto-import skills from workspace/skills/ and ~/.loom/skills/
        skill_catalog = await self._auto_import_skills()
        indexer = MemoryIndexer(
            self._semantic, self._procedural, self._episodic, self._relational,
            skill_catalog=skill_catalog,
        )
        self._memory_index = await indexer.build()
        if not self._memory_index.is_empty:
            index_text = self._memory_index.render()
            if self.messages and self.messages[0]["role"] == "system":
                self.messages[0]["content"] += f"\n\n{index_text}"
            else:
                self.messages.insert(0, {"role": "system", "content": index_text})

        # Issue #133: inject memory health alert into system context
        # so the agent is aware of prior session failures.
        health_ctx = self._governor.health.report().render_agent_context()
        if health_ctx:
            if self.messages and self.messages[0]["role"] == "system":
                self.messages[0]["content"] += f"\n\n{health_ctx}"
            else:
                self.messages.insert(0, {"role": "system", "content": health_ctx})

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
        from loom.platform.cli.tools import (
            make_exec_escape_fn,
            make_fetch_url_tool,
            make_load_skill_tool,
            make_memorize_tool,
            make_memory_health_tool,
            make_query_relations_tool,
            make_recall_tool,
            make_relate_tool,
            make_spawn_agent_tool,
            make_web_search_tool,
        )
        search = MemorySearch(self._semantic, self._procedural)
        search._health = self._governor.health
        self.registry.register(make_recall_tool(search))
        self.registry.register(make_memorize_tool(self._semantic, governor=self._governor))
        self.registry.register(make_relate_tool(self._relational))
        self.registry.register(make_query_relations_tool(self._relational))
        self.registry.register(make_memory_health_tool(self._governor))

        # Issue #56: Register load_skill tool with outcome tracker
        from loom.core.memory.skill_outcome import SkillOutcomeTracker
        self._skill_outcome_tracker = SkillOutcomeTracker(
            procedural=self._procedural,
            semantic=self._semantic,
            session_id=self.session_id,
        )
        skills_dirs = [
            self.workspace / "skills",
            Path.home() / ".loom" / "skills",
        ]

        # Issue #64 Phase B: SkillCheckManager for dynamic precondition mounting
        from loom.core.harness.skill_checks import SkillCheckManager
        self._skill_check_manager = SkillCheckManager(self.registry)

        # Platform-swappable confirm — defaults to CLI.  TUI/Discord replace
        # this after start() via ``session._confirm_fn = <their_version>``.
        # BlastRadiusMiddleware._confirm is also patched by TUI/Discord;
        # skill check approval uses _confirm_fn so both paths stay in sync.
        self._confirm_fn = self._confirm_tool_cli

        self.registry.register(make_load_skill_tool(
            self._procedural, skills_dirs,
            outcome_tracker=self._skill_outcome_tracker,
            semantic=self._semantic,
            turn_index_fn=lambda: self._turn_index,
            skill_check_manager=self._skill_check_manager,
            relational=self._relational,
            confirm_fn=lambda call: self._confirm_fn(call),
        ))

        # Issue #64 Phase B: Register unload_skill tool
        from loom.platform.cli.tools import make_unload_skill_tool
        self.registry.register(make_unload_skill_tool(
            self._skill_check_manager,
        ))

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

        # Issue #9: MCP client — import tools from external MCP servers
        try:
            from loom.extensibility.mcp_client import load_mcp_servers_into_session

            self._mcp_clients = await load_mcp_servers_into_session(
                _load_loom_config(), self, extra_env=_load_env(),
            )
            if self._mcp_clients:
                names = ", ".join(c._cfg.name for c in self._mcp_clients)
                console.print(
                    f"[dim]  MCP: {len(self._mcp_clients)} server(s) connected ({names})[/dim]"
                )
        except Exception as exc:
            logger.warning("MCP servers failed to load: %s", exc)

        # Wire up LegitimacyGuardMiddleware before the rest of the pipeline.
        # Scope: write_file only (read-before-write contract, Issue #118).
        # run_bash and MCP tools are NOT added here — exec authorization belongs
        # to BlastRadiusMiddleware (EXEC capability / scope grants).
        from loom.core.harness.middleware import LegitimacyGuardMiddleware
        self._legitimacy_guard = LegitimacyGuardMiddleware()

        # LogMiddleware is omitted here: stream_turn() yields ToolBegin/ToolEnd
        # events that the UI renders, providing richer display without duplication.
        # Wire escape detector only when strict_sandbox is on — that's the
        # only case where /auto pre-authorizes EXEC tools within workspace.
        _exec_escape_fn = (
            make_exec_escape_fn(self.workspace) if self._strict_sandbox else None
        )

        self._pipeline = MiddlewarePipeline(
            [
                LifecycleMiddleware(
                    registry=self.registry,
                    on_lifecycle=self._on_lifecycle,
                    on_state_change=self._on_state_change,
                ),
                TraceMiddleware(on_trace=self._on_trace),
                SchemaValidationMiddleware(registry=self.registry),
                self._legitimacy_guard,
                BlastRadiusMiddleware(
                    perm_ctx=self.perm,
                    confirm_fn=self._confirm_tool_cli,
                    exec_escape_fn=_exec_escape_fn,
                    registry=self.registry,
                ),
                LifecycleGateMiddleware(registry=self.registry),
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
        # Issue #64: unmount all skill-declared checks before closing
        if hasattr(self, "_skill_check_manager"):
            self._skill_check_manager.unmount_all()
        # Grab the connection reference and immediately clear self._db so any
        # concurrent or re-entrant call (e.g. /new → on_unmount) hits the guard above.
        db, self._db = self._db, None
        db_ctx, self._db_ctx = self._db_ctx, None
        # ── Session shutdown: each step is independently guarded ──────
        # Issue #133: the previous monolithic try/except silently swallowed
        # all errors — a failure in compress_session would also prevent
        # session log update, evolution analysis, and decay from running.
        # Alias for concise health recording
        _health = self._governor.health if self._governor else None

        try:
            # Step 1: Compress session into semantic memory
            try:
                console.print(Rule("[dim]Compressing session to memory…[/dim]"))
                count = await compress_session(
                    self.session_id,
                    self._episodic,
                    self._semantic,
                    self.router,
                    self.model,
                    governor=self._governor,
                )
                if count:
                    console.print(f"[dim]  Saved {count} fact(s) to semantic memory.[/dim]")
                if _health:
                    _health.record_success("session_compress")
            except Exception as exc:
                logger.error(
                    "Session compress failed — episodic→semantic transfer lost: %s",
                    exc, exc_info=True,
                )
                if _health:
                    _health.record_failure("session_compress", str(exc))

            # Step 2: Skill evolution analysis (Issue #58)
            if self._procedural is not None and self._semantic is not None:
                try:
                    from loom.core.cognition.counter_factual import SkillEvolutionHook
                    evolution_hook = SkillEvolutionHook(
                        router=self.router, model=self.model,
                        procedural=self._procedural, semantic=self._semantic,
                    )
                    evolved = await evolution_hook.check_all_skills()
                    if evolved:
                        console.print(
                            f"[dim]  Queued evolution analysis for {evolved} skill(s).[/dim]"
                        )
                    if _health:
                        _health.record_success("skill_evolution")
                except Exception as exc:
                    logger.warning("Skill evolution analysis failed: %s", exc)
                    if _health:
                        _health.record_failure("skill_evolution", str(exc))

            # Step 3: Memory decay cycle (Issue #43)
            if self._governor is not None:
                try:
                    decay = await self._governor.run_decay_cycle()
                    if decay.total_pruned > 0:
                        console.print(
                            f"[dim]  Decayed {decay.total_pruned} stale entries "
                            f"(semantic={decay.semantic_pruned}, "
                            f"episodic={decay.episodic_pruned}, "
                            f"relational={decay.relational_pruned}).[/dim]"
                        )
                    if _health:
                        _health.record_success("decay_cycle")
                except Exception as exc:
                    logger.warning("Memory decay cycle failed: %s", exc)
                    if _health:
                        _health.record_failure("decay_cycle", str(exc))

            # Step 4: Update session log metadata
            if self._session_log is not None:
                try:
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
                except Exception as exc:
                    logger.warning("Session log update failed: %s", exc)

            # Step 5: Flush health state and show summary
            if _health:
                try:
                    await _health.flush()
                    report = _health.report()
                    if report.has_issues:
                        console.print(
                            f"[yellow dim]  ⚠ Memory health issues detected "
                            f"this session — check logs for details.[/yellow dim]"
                        )
                except Exception as exc:
                    logger.debug("Health tracker flush failed: %s", exc)
        finally:
            # Issue #61 Bug 2: wait for pending skill_eval background tasks
            # before closing the DB so their upsert writes can complete.
            pending_evals = [
                t for t in asyncio.all_tasks()
                if t.get_name().startswith("skill_eval:")
            ]
            if pending_evals:
                done, still_pending = await asyncio.wait(pending_evals, timeout=5.0)
                if still_pending:
                    logger.warning(
                        "%d skill evaluation(s) unfinished on shutdown",
                        len(still_pending),
                    )

            for client in self._mcp_clients:
                try:
                    await client.disconnect()
                except Exception as exc:
                    logger.debug("MCP client disconnect error: %s", exc)
            self._mcp_clients = []

            try:
                if db_ctx is not None:
                    await db_ctx.__aexit__(None, None, None)
                elif db is not None:
                    await db.close()
            except Exception as exc:
                logger.debug("DB close error during shutdown: %s", exc)

    # ------------------------------------------------------------------
    # Streaming agent loop
    # ------------------------------------------------------------------

    async def stream_turn(
        self,
        user_input: str,
        *,
        abort_signal: "asyncio.Event | None" = None,
        origin: str = "interactive",
    ) -> AsyncIterator[
        TextChunk | ToolBegin | ToolEnd | TurnPaused | TurnDone | TurnDropped
        | ActionStateChange | ActionRolledBack
        | EnvelopeStarted | EnvelopeUpdated | EnvelopeCompleted
        | GrantsSnapshot
    ]:
        """
        Run one complete agent turn and yield typed UI events.

        Yields
        ------
        TextChunk   — each fragment of streaming LLM text
        ToolBegin   — just before a tool call executes
        ToolEnd     — just after a tool call finishes
        TurnDone    — once all tool loops are resolved
        """
        self._current_origin = origin

        # Issue #131: Reset abort signal from previous turn so the session
        # is not permanently stuck after a circuit-breaker trip.
        self._abort.reset()
        self._cancel_requested = False

        # Issue #131: Reset per-turn deny counter so timeouts from a previous
        # turn don't carry over and immediately trip the circuit breaker.
        self.perm.recent_denies = 0

        if hasattr(self, "_legitimacy_guard"):
            self._legitimacy_guard.reset_probe()

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

        # Sanitize first so _smart_compact never sees orphaned tool_call sequences.
        # (Restart or mid-turn cancel can leave the assistant message in DB without
        # a matching tool result — sanitize drops it before compaction reads history.)
        self._sanitize_history()

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

        # Think-block filter state — persists across the whole turn so multi-step
        # reasoning (think → tool use → think again) is handled correctly.
        _think_in = False           # currently inside <think>…</think>?
        _think_shown = False        # emitted ThinkCollapsed for this streaming call?
        _tbuf = ""                  # partial-tag lookahead buffer
        _think_parts: list[str] = []  # accumulates think content for /think command
        _current_think_start = 0    # index into _think_parts where current block began

        _stream_retry = 0         # counts back-to-back stream_none retries
        _MAX_STREAM_RETRIES = 2  # auto-retry up to 2 times on response=None
        _stop_reason = "complete"  # tracks why the loop exits
        while True:
            # Check abort signal at top of each LLM call iteration.
            # abort_signal is external (e.g. from AutonomyDaemon);
            # self._abort.signal is internal (from cancel()).
            sig = abort_signal if abort_signal is not None else self._abort.signal
            if sig.is_set():
                _stop_reason = "cancelled"
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
                                        # Flush text accumulated before the think block,
                                        # then emit a dedicated ThinkCollapsed event so
                                        # each platform can render it in its own style.
                                        if out_parts:
                                            yield TextChunk(text="".join(out_parts))
                                            out_parts = []
                                        _think_full = "".join(
                                            _think_parts[_current_think_start:]
                                        ).strip()
                                        _think_summary = _think_full[:120].replace("\n", " ")
                                        yield ThinkCollapsed(
                                            summary=_think_summary,
                                            full=_think_full,
                                        )
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
                                    _current_think_start = len(_think_parts)
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
                # Stream ended without a final message — connection likely dropped.
                # Attempt a transparent retry (up to _MAX_STREAM_RETRIES) before
                # giving up so transient MiniMax/network glitches don't silently
                # kill long-running tasks.
                if _stream_retry < _MAX_STREAM_RETRIES:
                    _stream_retry += 1
                    logger.warning(
                        "stream_turn: response is None on attempt %d/%d — retrying…",
                        _stream_retry, _MAX_STREAM_RETRIES,
                    )
                    yield TurnDropped(
                        stop_reason="stream_none",
                        retry_count=_stream_retry,
                        tool_count=tool_count,
                    )
                    await asyncio.sleep(1.0 * _stream_retry)  # brief back-off
                    # Re-sanitize history before retry in case partial state was appended
                    self._sanitize_history()
                    continue
                # All retries exhausted
                logger.error(
                    "stream_turn: response is None after %d retries — dropping turn",
                    _MAX_STREAM_RETRIES,
                )
                yield TurnDropped(
                    stop_reason="stream_none",
                    retry_count=_stream_retry,
                    tool_count=tool_count,
                    exhausted=True,
                )
                break

            # Surface native thinking blocks (Anthropic extended thinking API).
            # stream.text_stream skips thinking content entirely — the blocks only
            # appear in raw_message["_thinking_blocks"] after streaming finishes.
            # Yield ThinkCollapsed *before* tool dispatch so the UI shows reasoning
            # context inline, ahead of the resulting tool calls.
            for _tb in response.raw_message.get("_thinking_blocks", []):
                _tb_text = _tb.get("thinking", "").strip()
                if not _tb_text:
                    continue
                _think_parts.append(_tb_text)   # include in /think output
                yield ThinkCollapsed(
                    summary=_tb_text[:120].replace("\n", " "),
                    full=_tb_text,
                )

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

                # Issue #58: trigger skill self-assessment before TurnDone
                self._trigger_skill_assessment()

                # Mid-session episodic compression: configurable via loom.toml
                # [memory] episodic_compress_threshold (default 30).
                try:
                    ep_count = await self._episodic.count_session(self.session_id)
                    if ep_count >= self._episodic_compress_threshold:
                        fact_count = await compress_session(
                            self.session_id, self._episodic, self._semantic,
                            self.router, self.model,
                            governor=self._governor,
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

                # ── Issue #106: Create envelope for this tool batch ────────
                self._envelope_counter += 1
                envelope = ExecutionEnvelope(
                    session_id=self.session_id,
                    turn_index=self._turn_index,
                )
                self._current_envelope = envelope
                _batch_t0 = time.monotonic()

                # Yield EnvelopeStarted *before* ToolBegin events
                yield EnvelopeStarted(envelope=self._build_envelope_view(_batch_t0))

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
                        # Drain lifecycle events queued during dispatch
                        while not self._lifecycle_events.empty():
                            yield self._lifecycle_events.get_nowait()
                        # Issue #106: Yield envelope update after each tool completes
                        yield EnvelopeUpdated(envelope=self._build_envelope_view(_batch_t0))
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
                    # Sequential: single tool, or needs interactive confirmation.
                    # Dispatched as a task so we can drain lifecycle events
                    # (e.g. AWAITING_CONFIRM) while the confirm widget blocks.
                    for tu in response.tool_uses:
                        yield ToolBegin(name=tu.name, args=tu.args, call_id=tu.id)
                        ts = time.monotonic()

                        dispatch_task = asyncio.create_task(
                            self._dispatch(tu.name, tu.args, tu.id)
                        )
                        # Drain lifecycle events while dispatch is in flight.
                        # This makes ⏳ awaiting_confirm visible in the TUI
                        # before the user responds to the confirm prompt (#109).
                        _last_envelope_yield = time.monotonic()
                        while not dispatch_task.done():
                            try:
                                await asyncio.wait_for(
                                    asyncio.shield(dispatch_task), timeout=0.15,
                                )
                            except asyncio.TimeoutError:
                                pass
                            except Exception:
                                break
                            # Drain any lifecycle events queued during this tick
                            drained = False
                            while not self._lifecycle_events.empty():
                                yield self._lifecycle_events.get_nowait()
                                drained = True
                            # Yield envelope update if state changed, or as a
                            # periodic fallback every ~1s so the TUI stays fresh
                            # even if no lifecycle events fired (#108 review).
                            now_mono = time.monotonic()
                            if drained or (now_mono - _last_envelope_yield) > 1.0:
                                yield EnvelopeUpdated(
                                    envelope=self._build_envelope_view(_batch_t0)
                                )
                                _last_envelope_yield = now_mono

                        # Collect the result
                        try:
                            result = dispatch_task.result()
                        except Exception as _dispatch_exc:
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
                        # Final drain of lifecycle events after dispatch
                        while not self._lifecycle_events.empty():
                            yield self._lifecycle_events.get_nowait()
                        # Issue #106: Yield envelope update after each tool completes
                        yield EnvelopeUpdated(envelope=self._build_envelope_view(_batch_t0))
                        self.messages.append(
                            self.router.format_tool_result(
                                self.model, tu.id, tool_output, result.success,
                            )
                        )
                        asyncio.ensure_future(self._log_message(
                            "tool", tool_output[:500],
                            {"tool_call_id": tu.id, "tool_name": tu.name},
                        ))

                # ── Issue #106: Envelope completed ─────────────────────────
                if self._current_envelope is not None:
                    self._current_envelope.complete()
                _completed_view = self._build_envelope_view(_batch_t0)
                yield EnvelopeCompleted(envelope=_completed_view)
                # Keep last 10 envelopes for TUI history display
                self._recent_envelopes.append(_completed_view)
                if len(self._recent_envelopes) > 10:
                    self._recent_envelopes = self._recent_envelopes[-10:]

                # ── Issue #108: Grants snapshot after each batch ──────────
                yield self._build_grants_snapshot()

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
                        # Issue #58: trigger skill self-assessment before TurnDone
                        self._trigger_skill_assessment()
                        yield TurnDone(
                            tool_count=tool_count,
                            input_tokens=input_tokens,
                            output_tokens=output_tokens,
                            elapsed_ms=(time.monotonic() - t0) * 1000,
                        )
                        return
            else:
                # Unexpected stop_reason (e.g. 'max_tokens', unknown provider value).
                # Log and surface via TurnDropped so platforms can show a warning
                # rather than silently dropping the turn.
                _raw_stop = getattr(response, "stop_reason", "unknown")
                logger.warning(
                    "stream_turn: unexpected stop_reason=%r after %d tool(s) — stopping",
                    _raw_stop, tool_count,
                )
                yield TurnDropped(
                    stop_reason=str(_raw_stop),
                    retry_count=0,
                    tool_count=tool_count,
                )
                break

        self._last_think = "".join(_think_parts).strip()
        self._turn_index += 1
        # Issue #58: trigger skill self-assessment before TurnDone
        self._trigger_skill_assessment()
        yield TurnDone(
            tool_count=tool_count,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            elapsed_ms=(time.monotonic() - t0) * 1000,
            stop_reason=_stop_reason,
        )

    # ------------------------------------------------------------------
    # Issue #58: Skill self-assessment trigger
    # ------------------------------------------------------------------

    def _trigger_skill_assessment(self) -> None:
        """
        Fire-and-forget: schedule skill self-assessment if a skill was
        activated during this session.

        Called at each TurnDone point in stream_turn(). The LLM self-assessment
        runs as a background task and never blocks the conversation.
        """
        if (
            self._skill_outcome_tracker is None
            or not self._skill_outcome_tracker.has_active_skills()
        ):
            return

        # Extract last assistant message as turn summary
        turn_summary = ""
        for msg in reversed(self.messages):
            if msg.get("role") == "assistant":
                content = msg.get("content", "")
                if isinstance(content, str):
                    turn_summary = content[:2000]
                break

        if not turn_summary:
            return

        self._skill_outcome_tracker.maybe_evaluate(
            router=self.router,
            model=self.model,
            turn_index=self._turn_index,
            turn_summary=turn_summary,
        )

    # ------------------------------------------------------------------
    # Issue #56: Auto-import skills from SKILL.md files
    # ------------------------------------------------------------------

    async def _auto_import_skills(self) -> list["SkillCatalogEntry"]:
        """
        Scan skills/ directories for SKILL.md files and auto-import them
        into ProceduralMemory.  Returns a catalog of discovered skills
        for injection into the system prompt (Agent Skills spec Tier 1).

        Scan locations (in priority order):
          1. <workspace>/skills/*/SKILL.md  — project-level skills
          2. ~/.loom/skills/*/SKILL.md      — user-level skills

        Follows lenient validation: malformed YAML → skip, name mismatch → warn.
        """
        import yaml  # lazy import — only needed for frontmatter parsing

        catalog: list[SkillCatalogEntry] = []
        seen_names: set[str] = set()

        scan_dirs = [
            self.workspace / "skills",
            Path.home() / ".loom" / "skills",
        ]

        for scan_dir in scan_dirs:
            if not scan_dir.is_dir():
                continue

            for skill_dir in sorted(scan_dir.iterdir()):
                if not skill_dir.is_dir():
                    continue

                skill_md = skill_dir / "SKILL.md"
                if not skill_md.is_file():
                    continue

                try:
                    raw = skill_md.read_text(encoding="utf-8")
                except Exception:
                    continue

                # Parse YAML frontmatter
                name, description, tags, pc_refs = _parse_skill_frontmatter(raw)
                if not name:
                    # Fallback: use directory name
                    name = skill_dir.name
                if not description:
                    logger.debug("Skipping skill '%s': no description", name)
                    continue

                # Dedup: project-level skills take priority
                if name in seen_names:
                    continue
                seen_names.add(name)

                # Upsert to ProceduralMemory if new or stale
                from loom.core.memory.procedural import SkillGenome

                existing = await self._procedural.get(name)
                file_mtime = skill_md.stat().st_mtime

                needs_update = (
                    existing is None
                    or (existing.updated_at and existing.updated_at.timestamp() < file_mtime)
                    or existing.body != raw
                )

                if needs_update:
                    genome = SkillGenome(
                        name=name,
                        body=raw,
                        version=(existing.version + 1) if existing else 1,
                        confidence=existing.confidence if existing else 0.8,
                        usage_count=existing.usage_count if existing else 0,
                        success_rate=existing.success_rate if existing else 0.0,
                        tags=tags or (existing.tags if existing else []),
                        precondition_check_refs=pc_refs,
                    )
                    await self._procedural.upsert(genome)
                    logger.debug("Auto-imported skill '%s' from %s", name, skill_md)

                # Add to catalog for Tier 1 disclosure
                catalog.append(SkillCatalogEntry(
                    name=name,
                    description=description,
                    location=str(skill_md),
                ))

        return catalog

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

        Note: the interactive prompt (Confirm.ask) is CLI-specific.  Non-CLI
        platforms (Discord, TUI) override confirm_fn on BlastRadiusMiddleware
        but this approval path is not yet injectable.  New plugins will block
        on non-terminal platforms until this is addressed.
        """
        import importlib.util as _ilu
        import loom as _loom_pkg
        from rich.prompt import Confirm

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
            if msg.get("role") != "assistant":
                continue
            for tc in msg.get("tool_calls", []):
                fn = tc.get("function", {})
                raw_args = fn.get("arguments", "{}")
                if not isinstance(raw_args, str):
                    fn["arguments"] = json.dumps(raw_args, ensure_ascii=False)
                    continue
                try:
                    json.loads(raw_args)
                except (json.JSONDecodeError, ValueError):
                    fn["arguments"] = "{}"

        # Pass 2: remove assistant messages with orphaned tool_calls
        # Build set of all tool result ids present in the history.
        result_ids: set[str] = set()
        for msg in msgs:
            if msg.get("role") == "tool":
                tid = msg.get("tool_call_id")
                if tid:
                    result_ids.add(tid)
            # Anthropic-style tool results live inside content blocks of role=user
            if msg.get("role") == "user":
                for block in msg.get("content", []) if isinstance(msg.get("content"), list) else []:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        tid = block.get("tool_use_id")
                        if tid:
                            result_ids.add(tid)

        keep: list[dict] = []
        for msg in msgs:
            if msg.get("role") == "assistant":
                tool_calls = msg.get("tool_calls", [])
                if tool_calls:
                    # Drop message if any of its tool calls lack a result
                    missing = [
                        tc["id"] for tc in tool_calls
                        if tc.get("id") and tc["id"] not in result_ids
                    ]
                    if missing:
                        # Issue #94 Gap 1 fix: drop the message if OpenAI-format
                        # tool_calls are orphaned. Also check Anthropic-style
                        # tool_use blocks in content for dual-format messages.
                        content = msg.get("content", [])
                        if isinstance(content, list):
                            use_ids = [
                                b.get("id") for b in content
                                if isinstance(b, dict) and b.get("type") == "tool_use"
                            ]
                            if use_ids:
                                # Dual-format: also check Anthropic-style
                                if any(uid not in result_ids for uid in use_ids if uid):
                                    continue  # drop orphaned message
                            elif any(tc.get("id") not in result_ids
                                     for tc in tool_calls if tc.get("id")):
                                # Pure OpenAI format: missing tool_call results → drop
                                continue
                        else:
                            continue  # drop orphaned message
            keep.append(msg)
        self.messages = keep

        # Pass 3 (Issue #94 Gap 3): remove lone tool result messages whose
        # tool_call_id has no matching tool_call in any assistant message.
        # This can happen when cancel occurs during multi-tool dispatch.
        call_ids: set[str] = set()
        for msg in self.messages:
            if msg.get("role") == "assistant":
                for tc in msg.get("tool_calls", []):
                    cid = tc.get("id")
                    if cid:
                        call_ids.add(cid)
                # Anthropic-style tool_use blocks in content
                content = msg.get("content", [])
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "tool_use":
                            uid = block.get("id")
                            if uid:
                                call_ids.add(uid)

        keep2: list[dict] = []
        for msg in self.messages:
            # OpenAI-style lone tool result
            if msg.get("role") == "tool":
                tid = msg.get("tool_call_id")
                if tid and tid not in call_ids:
                    continue  # drop orphaned tool result
            # Anthropic-style tool_result blocks inside role=user messages
            if msg.get("role") == "user" and isinstance(msg.get("content"), list):
                has_tool_results = False
                filtered_blocks = []
                for block in msg["content"]:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        has_tool_results = True
                        if block.get("tool_use_id") in call_ids:
                            filtered_blocks.append(block)
                        # else: drop orphaned tool_result block
                    else:
                        filtered_blocks.append(block)
                if has_tool_results and not filtered_blocks:
                    continue  # entire message was orphaned tool_results
                if has_tool_results:
                    msg = {**msg, "content": filtered_blocks}
            keep2.append(msg)
        self.messages = keep2

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
            origin=self._current_origin,
        )
        result = await self._pipeline.execute(call, tool_def.executor)

        # Issue #106: link ActionRecord to the envelope (if not already linked
        # by _on_state_change during execution — see #109 early-add logic).
        ctx = call.metadata.get(LIFECYCLE_CTX_KEY)
        if ctx is not None and self._current_envelope is not None:
            if ctx.record not in self._current_envelope.records:
                self._current_envelope.add(ctx.record)

        return result

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

        # Issue #56: Old binary record_outcome() loop removed.
        # Skill confidence is now updated by SkillOutcomeTracker via
        # quality-gradient self-assessment (1-5 score EMA), not per-tool
        # success/failure.  The old loop also never hit because tool_name
        # (e.g. "read_file") rarely matches a SkillGenome name (e.g. "loom-engineer").

        # Track tool usage for SkillOutcomeTracker
        if self._skill_outcome_tracker is not None:
            self._skill_outcome_tracker.record_tool_usage()

        # Counter-factual reflection: fire-and-forget for execution_error failures.
        # Only triggers when a SkillGenome exists for the tool (checked inside reflector).
        if (
            self._reflector is not None
            and not result.success
            and result.failure_type == "execution_error"
        ):
            self._reflector.maybe_reflect(call, result, self.session_id)

    async def _on_lifecycle(self, record: ActionRecord) -> None:
        """
        Persist a completed ActionRecord to the action_records table (Issue #42).

        Called by LifecycleMiddleware after an action reaches a terminal state.
        Stores full state_history as JSON for complete audit trail.
        """
        import json as _json
        try:
            envelope_id = self._current_envelope.id if self._current_envelope else ""
            await self._db.execute(
                """
                INSERT OR REPLACE INTO action_records
                    (id, envelope_id, session_id, turn_index, tool_name, call_id,
                     final_state, intent_summary, scope, duration_ms,
                     state_history, has_rollback, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.id,
                    envelope_id,
                    self.session_id,
                    self._turn_index,
                    record.tool_name,
                    record.call.id if record.call else "",
                    record.final_state,
                    record.intent.intent_summary,
                    record.intent.scope,
                    record.elapsed_ms,
                    _json.dumps(record.history_dicts(), ensure_ascii=False),
                    1 if record.rollback_result is not None else 0,
                    record.created_at.isoformat(),
                ),
            )
            await self._db.commit()
        except Exception:
            pass  # DB write must never crash the pipeline

    async def _on_state_change(
        self, record: ActionRecord, old_state: str, new_state: str
    ) -> None:
        """
        Enqueue an ActionStateChange event for stream_turn() to yield.

        Called by LifecycleMiddleware on each state transition.
        Also enqueues ActionRolledBack when transitioning to 'reverted'.

        Issue #109: early-add the record to the envelope on first state
        change so _build_envelope_view() can see ⏳ awaiting_confirm
        while the confirm widget is blocking.
        """
        # Early-add record to envelope (#109)
        if self._current_envelope is not None:
            if record not in self._current_envelope.records:
                self._current_envelope.add(record)

        call_id = record.call.id if record.call else ""
        self._lifecycle_events.put_nowait(
            ActionStateChange(
                action_id=record.id,
                tool_name=record.tool_name,
                call_id=call_id,
                old_state=old_state,
                new_state=new_state,
                reason=record.state_history[-1].reason if record.state_history else None,
            )
        )
        # Emit a specialized rollback event
        if new_state == "reverted":
            rb_success = record.rollback_result.success if record.rollback_result else False
            rb_msg = ""
            if record.rollback_result:
                rb_msg = record.rollback_result.output or record.rollback_result.error or ""
            self._lifecycle_events.put_nowait(
                ActionRolledBack(
                    action_id=record.id,
                    tool_name=record.tool_name,
                    call_id=call_id,
                    rollback_success=rb_success,
                    message=str(rb_msg)[:200],
                )
            )

    # ------------------------------------------------------------------
    # Issue #106: Envelope projection — build read-only view for UI
    # ------------------------------------------------------------------

    def _build_envelope_view(self, batch_t0: float = 0.0) -> ExecutionEnvelopeView:
        """Build a read-only ``ExecutionEnvelopeView`` from the live envelope.

        This is the projection layer described in doc/43 — it only does
        view shaping, never mutates middleware state.
        """
        env = self._current_envelope
        if env is None:
            return ExecutionEnvelopeView(
                envelope_id=f"e{self._envelope_counter}",
                session_id=self.session_id,
                turn_index=self._turn_index,
                status="running",
                node_count=0,
                parallel_groups=0,
            )

        nodes: list[ExecutionNodeView] = []
        for record in env.records:
            call = record.call
            tdef = None
            if record.tool_name != "(unknown)":
                tdef = self.registry.get(record.tool_name)

            # Build args preview from first string argument
            args_preview = ""
            if call and call.args:
                first_val = next(iter(call.args.values()), "")
                if isinstance(first_val, str):
                    args_preview = first_val.replace("\n", "↵")[:60]

            # Detail fields (Issue #108)
            full_args = dict(call.args) if call and call.args else {}
            state_history = record.history_dicts()
            auth_decision = ""
            auth_expires = 0.0
            auth_selector = ""
            if call:
                auth_decision = call.metadata.get("confirm_decision", "")
                # Extract scope grant info if available
                scope_req = call.metadata.get("scope_request")
                if scope_req is not None:
                    reqs = getattr(scope_req, "requirements", [])
                    if reqs:
                        auth_selector = reqs[0].selector
                # Check for lease TTL from grants
                if auth_decision == "scope":
                    for g in self.perm._effective_grants():
                        if (hasattr(g, "source") and g.source == "lease"
                                and g.valid_until > 0):
                            auth_expires = g.valid_until
                            break

            output_preview = ""
            if record.result:
                raw = record.result.output if record.result.success else (record.result.error or "")
                output_preview = str(raw)[:200]

            nodes.append(ExecutionNodeView(
                node_id=record.id,
                call_id=call.id if call else "",
                action_id=record.id,
                tool_name=record.tool_name,
                level=0,  # all current parallel dispatch = single level
                state=record.state.value,
                trust_level=tdef.trust_level.plain if tdef else "SAFE",
                capabilities=[c.name for c in tdef.capabilities] if tdef else [],
                args_preview=args_preview,
                duration_ms=record.elapsed_ms,
                error_snippet=(
                    (record.result.error or "")[:80]
                    if record.result and not record.result.success
                    else ""
                ),
                full_args=full_args,
                state_history=state_history,
                auth_decision=auth_decision,
                auth_expires=auth_expires,
                auth_selector=auth_selector,
                output_preview=output_preview,
            ))

        # Compute aggregate status
        all_done = env.all_terminal
        has_failure = any(r.is_failure for r in env.records)
        if all_done and has_failure:
            status = "failed"
        elif all_done:
            status = "completed"
        else:
            status = "running"

        elapsed_ms = (time.monotonic() - batch_t0) * 1000 if batch_t0 else 0.0

        return ExecutionEnvelopeView(
            envelope_id=f"e{self._envelope_counter}",
            session_id=self.session_id,
            turn_index=self._turn_index,
            status=status,
            node_count=len(env.records),
            parallel_groups=1,
            elapsed_ms=elapsed_ms,
            levels=[[n.node_id for n in nodes]],
            nodes=nodes,
        )

    def _build_grants_snapshot(self) -> GrantsSnapshot:
        """Build a GrantsSnapshot from current PermissionContext (#108, #112)."""
        import hashlib
        import time as _time
        grants = self.perm._effective_grants()
        now = _time.time()
        active = len(grants)
        # Find nearest expiry
        next_expiry_secs = 0.0
        summaries: list[GrantSummary] = []
        for g in grants:
            # Stable ID from grant fields for UI tracking
            id_src = f"{g.resource}:{g.action}:{g.selector}:{g.granted_at}"
            gid = hashlib.md5(id_src.encode()).hexdigest()[:8]
            # Determine tool_name from metadata if available, else action
            tool_name = g.constraints.get("tool_name", g.action)
            summaries.append(GrantSummary(
                grant_id=gid,
                tool_name=tool_name,
                selector=g.selector,
                source=g.source,
                expires_at=g.valid_until,
            ))
            if g.valid_until > 0:
                remaining = g.valid_until - now
                if remaining > 0:
                    if next_expiry_secs == 0.0 or remaining < next_expiry_secs:
                        next_expiry_secs = remaining
        return GrantsSnapshot(
            active_count=active,
            next_expiry_secs=next_expiry_secs,
            grants=summaries,
        )

    @staticmethod
    def _format_scope_panel(call: ToolCall) -> str:
        """
        Build a Rich-formatted string describing scope verdict + diff.

        Phase C (Issue #45): when scope metadata is available, the confirm
        prompt shows structured information instead of raw args.
        """
        from loom.core.harness.scope import (
            DiffReason, PermissionVerdict, ScopeDiff, ScopeRequest,
        )

        verdict = call.metadata.get("scope_verdict")
        diff: ScopeDiff | None = call.metadata.get("scope_diff")
        scope_req: ScopeRequest | None = call.metadata.get("scope_request")

        if verdict is None or scope_req is None:
            # Legacy path — no scope metadata available.
            # Format args with smart truncation so long paths remain readable.
            arg_lines: list[str] = []
            for k, v in call.args.items():
                if isinstance(v, str):
                    display = v if len(v) <= 120 else v[:40] + "…" + v[-40:]
                    arg_lines.append(f"  [cyan]{k}[/cyan]: {display}")
                else:
                    arg_lines.append(f"  [cyan]{k}[/cyan]: {v!r}")
            args_display = "\n".join(arg_lines) if arg_lines else "  (no args)"
            return (
                f"[bold]{call.tool_name}[/bold]  {call.trust_level.label}\n"
                f"{args_display}"
            )

        # --- Verdict-aware header ---
        _REASON_LABELS: dict[DiffReason, str] = {
            DiffReason.FIRST_TIME: "First time accessing this resource",
            DiffReason.SELECTOR_EXPANSION: "Expanding beyond previously approved scope",
            DiffReason.CONSTRAINT_EXPANSION: "Exceeding previously approved limits",
            DiffReason.RESOURCE_TYPE_NEW: "New resource type not previously authorized",
        }

        _VERDICT_TITLES: dict[PermissionVerdict, tuple[str, str]] = {
            PermissionVerdict.CONFIRM: ("Tool requires confirmation", "yellow"),
            PermissionVerdict.EXPAND_SCOPE: ("Scope expansion required", "red"),
        }
        title_text, border = _VERDICT_TITLES.get(
            verdict, ("Tool requires confirmation", "yellow"),
        )

        lines: list[str] = [
            f"[bold]{call.tool_name}[/bold]  {call.trust_level.label}",
        ]

        # Scope summary: what the tool wants to access
        for req in scope_req.requirements:
            lines.append(
                f"  [cyan]{req.resource}[/cyan]:{req.action} → "
                f"[bold]{req.selector}[/bold]"
            )
            if req.constraints.get("scope_unknown"):
                lines.append("  [yellow]⚠ scope could not be fully resolved[/yellow]")

        # Diff reason
        if diff is not None and not diff.is_fully_covered:
            reason_label = _REASON_LABELS.get(diff.reason, str(diff.reason.value))
            lines.append(f"\n[dim]{reason_label}[/dim]")

            # Show what's already covered vs missing
            if diff.covered:
                covered_selectors = ", ".join(r.selector for r in diff.covered)
                lines.append(f"[green]✓ covered:[/green] {covered_selectors}")
            if diff.missing:
                missing_selectors = ", ".join(r.selector for r in diff.missing)
                lines.append(f"[yellow]● new:[/yellow] {missing_selectors}")

        return "\n".join(lines)

    async def _confirm_tool_cli(self, call: ToolCall) -> "ConfirmDecision":
        """
        CLI-specific confirmation prompt (stdin / Rich panel).

        Phase C (Issue #45): scope metadata → verdict + diff info.
        Phase B (Issue #88): returns ConfirmDecision (y/s/a/N) instead of bool.

        Returns
        -------
        ConfirmDecision
            DENY  — user typed N or Enter (default)
            ONCE  — user typed y (approve and grant this scope for the session)
            SCOPE — user typed s (approve with a 30-min lease, auto-expires)
            AUTO  — user typed a (permanent grant, same scope, no expiry)
        """
        from loom.core.harness.scope import ConfirmDecision, PermissionVerdict

        # Stop any running spinner before printing the confirm panel so the
        # spinner animation doesn't overwrite the prompt input line.
        # Write \r\033[K directly to stdout — Rich Console.print strips \r.
        import sys
        if self._cancel_spinner_fn is not None:
            self._cancel_spinner_fn()
            sys.stdout.write("\r\033[K")
            sys.stdout.flush()
        console.print()

        # Determine panel styling from verdict
        verdict = call.metadata.get("scope_verdict")
        if verdict == PermissionVerdict.EXPAND_SCOPE:
            title = "[red]⚠ Scope expansion required[/red]"
            border_style = "red"
        else:
            title = "[yellow]  Tool requires confirmation[/yellow]"
            border_style = "yellow"

        console.print(
            Panel(
                self._format_scope_panel(call),
                title=title,
                subtitle="[dim]y=approve  s=lease (30m)  a=permanent  N=deny[/dim]",
                border_style=border_style,
            )
        )
        # Use prompt_toolkit so the prompt renders correctly on all terminals.
        from prompt_toolkit import prompt as pt_prompt

        try:
            answer = await asyncio.get_event_loop().run_in_executor(
                None, pt_prompt, "Allow? [y=approve/s=lease/a=permanent/N]: "
            )
        except (EOFError, KeyboardInterrupt):
            answer = ""

        choice = answer.strip().lower()
        if choice in {"y", "yes"}:
            return ConfirmDecision.ONCE
        if choice in {"s", "scope"}:
            return ConfirmDecision.SCOPE
        if choice in {"a", "auto"}:
            return ConfirmDecision.AUTO
        return ConfirmDecision.DENY

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
        """
        from loom.core.tasks.graph import TaskGraph
        from loom.core.tasks.scheduler import TaskScheduler

        graph = TaskGraph()
        node_ids: list[str] = []
        for tu in tool_uses:
            node = graph.add(
                tu.name,
                metadata={"tool_use": tu},
            )
            node_ids.append(node.id)

        async def _run_node(node) -> tuple:
            tu = node.metadata["tool_use"]
            ts = time.monotonic()
            try:
                result = await self._dispatch(tu.name, tu.args, tu.id)
            except Exception as exc:
                result = ToolResult(
                    call_id=tu.id,
                    tool_name=tu.name,
                    success=False,
                    error=f"Internal dispatch error: {exc}",
                    failure_type="execution_error",
                )
            duration_ms = (time.monotonic() - ts) * 1000
            return result, duration_ms

        plan = graph.compile()
        # Issue #94 Gap 2: wrap scheduler to handle unexpected failures.
        # Nodes without results get error ToolResults from the fallback below.
        try:
            await TaskScheduler(executor=_run_node).run(plan)
        except Exception as sched_exc:
            logger.error(
                "_dispatch_parallel: scheduler failed: %s", sched_exc, exc_info=True,
            )

        ordered: list[tuple] = []
        for tu, node_id in zip(tool_uses, node_ids):
            node = graph.get(node_id)
            if node is None or node.result is None:
                result = ToolResult(
                    call_id=tu.id,
                    tool_name=tu.name,
                    success=False,
                    error="Internal dispatch error: parallel scheduler returned no result",
                    failure_type="execution_error",
                )
                ordered.append((tu, result, 0.0))
                continue
            result, duration_ms = node.result
            ordered.append((tu, result, duration_ms))
        return ordered

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

    @property
    def model(self) -> str:
        """Return the currently active model name."""
        return self._model

    def set_model(self, model: str) -> bool:
        """
        Switch to a different LLM model/provider at runtime.

        Looks up the provider for the given model name via the routing table
        and updates the provider's model attribute.  Returns True on success.
        """
        ok = self.router.switch_model(model)
        if ok:
            self._model = model
        return ok


__all__ = [
    "LoomSession",
    "build_router",
    "compress_session",
    "_load_loom_config",
    "_load_env",
    "_parse_skill_frontmatter",
    "COMPRESS_PROMPT",
    "COMPACT_PROMPT",
]
