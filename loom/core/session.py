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
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, UTC
from pathlib import Path
from typing import Any, Awaitable, Callable

from dotenv import dotenv_values
from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule

_OUTCOME_MARKERS: dict[str, str] = {
    "✓": "fulfilled",
    "◐": "partial",
    "⚠": "unfulfilled",
    "↪": "pivoted",
    "🛑": "aborted",
}


def _clean_envelope_metadata_line(text: str, *, max_len: int = 140) -> str:
    line = " ".join(str(text or "").strip().split())
    if len(line) <= max_len:
        return line
    return line[: max_len - 1] + "…"


def _iter_nonempty_lines(text: str):
    for line in str(text or "").splitlines():
        line = line.strip()
        if line:
            yield line


def _extract_envelope_intent_marker(text: str) -> str:
    for line in _iter_nonempty_lines(text):
        if line.startswith("▸"):
            return _clean_envelope_metadata_line(line.removeprefix("▸"))
    return ""


def _extract_envelope_outcome_marker(text: str) -> tuple[str, str]:
    for line in _iter_nonempty_lines(text):
        marker = line[0]
        outcome = _OUTCOME_MARKERS.get(marker)
        if outcome is None:
            continue
        summary = _clean_envelope_metadata_line(line[1:].lstrip("\ufe0f"))
        return outcome, summary
    return "", ""


from loom.core.config import load_loom_config as _load_loom_config  # re-exported for tests/diagnostic/cli
from loom.core.diagnostic import DiagnosticReport, StartupDiagnostic
from loom.core.ledger import (
    CallMeta,
    JudgeVerdictPayload,
    LedgerEmitter,
    LedgerEnvelopeProjector,
    LedgerStore,
    ModelEventPayload,
    ThoughtPayload,
    TurnEndPayload,
    TurnStartPayload,
    new_correlation_id,
    reset_correlation,
    reset_turn_id,
    set_correlation,
    set_turn_id,
)
from loom.core.memory.facade import MemoryFacade
from loom.core.cognition.context import ContextBudget
from loom.core.cognition.prompt_stack import PromptStack
from loom.core.cognition.providers import AnthropicProvider
from loom.core.cognition.counter_factual import CounterFactualReflector
from loom.core.cognition.judge import (
    JudgeVerdict,
    build_trace_digest,
    format_verdict_reminder,
    gate_should_fire,
    is_high_stakes,
    run_judge,
    should_inject_reminder,
)
from loom.core.cognition.reflection import ReflectionAPI
from loom.core.cognition.router import LLMRouter
from loom.core.cognition.task_reflector import TaskDiagnostic, TaskReflector
from loom.core.timezone import user_timestamp  # Issue #124
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
    ReasoningContinuation,
    TextChunk,
    TierChanged,
    TierExpiryHint,
    ThinkCollapsed,
    ToolBegin,
    ToolEnd,
    TurnDone,
    TurnDropped,
    TurnPaused,
)
from loom.core.harness.lifecycle import ActionRecord, LIFECYCLE_CTX_KEY
from loom.core.harness.middleware import (
    BlastRadiusMiddleware,
    JITRetrievalMiddleware,
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
from loom.core.infra.telemetry import AgentTelemetryTracker, DEFAULT_DIMENSIONS
from loom.core.memory.embeddings import build_embedding_provider
from loom.core.memory.episodic import EpisodicEntry, EpisodicMemory
from loom.core.memory.governance import MemoryGovernor
from loom.core.memory.index import MemoryIndex, MemoryIndexer, SkillCatalogEntry
from loom.core.memory.procedural import ProceduralMemory
from loom.core.memory.pulse import (
    PULSE_TYPE_CONTRADICTION,
    PulseRecord,
    fold_contradiction_pulses,
)
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
Extract 0-7 reusable INSIGHTS that would be valuable in future sessions.

Three tiers of session content — only the middle tier belongs in semantic memory:

  1. OPERATIONAL TRACE (do NOT emit): step-by-step records of what happened
     this session — "ran tool X with args Y", "called Z before W", "diary
     write succeeded after list_dir". These are SOPs for *this* run; the
     episodic log already keeps them.

  2. INSIGHT (EMIT as FACT): durable, reusable knowledge that future sessions
     would benefit from — non-obvious user preferences, project facts that
     survive across sessions, lessons learned from failures, conceptual
     understanding. Should still be true and useful next week.

  3. SNAPSHOT (do NOT emit here): in-progress work state, current TODO,
     "next we should do X". That belongs in pursuit trackers, not semantic.

Quality bar: if a fact reads like "always do X before Y in this kind of
operation", that's almost always tier 1 (operational), not tier 2. Insights
are about *what is true*, not *what procedure to follow*.

It is fine to emit 0 facts if the session contains no genuine insights.
Do NOT pad to reach a minimum count.

Format each insight on its own line starting with "FACT: ".
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
    telemetry: "AgentTelemetryTracker | None" = None,
) -> int:
    """Compress unprocessed episodic entries to semantic facts.

    Uses a timestamp in the semantic key so repeated compressions (mid-session
    and on close) never overwrite each other. Processed entries are **soft-
    deleted** via ``mark_compressed`` rather than removed, so the original
    trace remains available for audit and recovery until TTL prune aged
    them out (``MemoryGovernor._prune_episodic_ttl``).

    When a MemoryGovernor is provided, candidate facts are filtered through
    the admission gate before being written to semantic memory (Issue #43).
    """
    entries = await episodic.read_session(session_id, uncompressed_only=True)
    if not entries:
        return 0

    log_text = "\n".join(f"[{e.event_type}] {e.content}" for e in entries[:60])

    response = await router.chat(
        model=model,
        messages=[{"role": "user", "content": COMPRESS_PROMPT.format(log=log_text)}],
        max_tokens=1024,
    )

    raw = response.text or ""
    # Issue #411 review: the old "no FACT: prefix → use raw text as one fact"
    # fallback turned compliant zero-insight responses ("No reusable
    # insights found.") into admissible semantic facts. With COMPRESS_PROMPT
    # now explicitly permitting 0 facts, that fallback is actively harmful —
    # any response without a FACT: prefix is treated as the model declaring
    # zero insights.
    facts = [
        line[len("FACT:") :].strip()
        for line in raw.splitlines()
        if line.strip().startswith("FACT:")
    ]

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

    # Soft-delete: mark every entry we sent to the LLM as compressed,
    # regardless of whether facts survived parsing + admission. Previously
    # this only ran when ``facts`` was non-empty, which meant duplicate-heavy
    # or zero-insight sessions kept re-paying the LLM cost on every trigger
    # (issue #411 review). The episodic rows are soft-deleted, not removed,
    # so audit + recovery still work via TTL-bounded retention.
    await episodic.mark_compressed([e.id for e in entries])

    # Issue #142: record the yield ratio so a silently degrading extractor
    # (facts << entries) surfaces as an anomaly on the next turn boundary.
    # Issue #173: exclude tool_call/tool_result entries from the denominator —
    # they describe operations, not knowledge, so their inclusion produced a
    # permanent false-positive low-yield alert on tool-heavy sessions.
    if telemetry is not None:
        dim = telemetry.get("memory_compression")
        if dim is not None:
            tool_events = sum(
                1 for e in entries
                if e.event_type in ("tool_call", "tool_result")
            )
            dim.record(
                entries=len(entries),
                facts=len(facts),
                tool_events=tool_events,
            )
            telemetry.mark_dirty()

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


# Issue #181: per-model output cap. Different providers have very different
# output ceilings (MiniMax-M2.7 ~64K, Claude Sonnet 4.6 ~64K, smaller local
# models often 4-8K) so a single hardcoded value was either wasteful or
# clipping long tool-loop turns. Resolved once at session start.
_DEFAULT_OUTPUT_MAX_TOKENS = 8192

# Issue #271: max consecutive max_tokens-with-zero-tools recoveries within a
# single stream_turn() before falling through to TurnDropped. Two attempts
# is empirically enough for deep-reasoning prompts (10-question deductive
# quizzes) without risking unbounded loops on pathologically long inputs.
_MAX_REASONING_CONTINUATIONS = 2


def _resolve_output_max_tokens(
    cfg: dict, model: str, router: "LLMRouter | None" = None,
) -> int:
    """Return the output-token cap for *model*.

    Precedence (Issue #272):
      1. ``[cognition.output_max_tokens_overrides][<model>]`` — explicit
         user override, case-sensitive (intentional: user typed it).
      2. ``[cognition].output_max_tokens`` — global user-set cap.
      3. **Provider-declared native limit** — provider class knows what its
         own models support. Lookup is case-insensitive. New models become
         available without loom.toml edits.
      4. ``_DEFAULT_OUTPUT_MAX_TOKENS`` — last-resort fallback.

    The provider-native step (3) is the one that lets users drop the
    overrides table entirely once their models are in the provider's
    ``NATIVE_OUTPUT_LIMITS``.
    """
    cog = cfg.get("cognition", {}) or {}
    overrides = cog.get("output_max_tokens_overrides", {}) or {}
    if model in overrides:
        try:
            return int(overrides[model])
        except (TypeError, ValueError):
            pass
    default = cog.get("output_max_tokens")
    if default is not None:
        try:
            return int(default)
        except (TypeError, ValueError):
            pass
    # Step 3 — ask the provider what its native ceiling is for this model.
    if router is not None:
        try:
            provider = router.get_provider(model)
            native = provider.native_max_tokens(model)
            if native is not None and native > 0:
                return int(native)
        except Exception:
            # Routing failures or missing provider shouldn't break the call;
            # fall through to the constant default.
            pass
    return _DEFAULT_OUTPUT_MAX_TOKENS


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


def _parse_skill_frontmatter(
    raw: str,
) -> tuple[str, str, list[str], list[dict], int | None]:
    """
    Parse YAML frontmatter from a SKILL.md file.

    Returns ``(name, description, tags, precondition_check_refs, model_tier)``.
    On parse failure returns empty strings / lists / ``None``.
    Follows Agent Skills spec lenient validation: best-effort parsing.

    Issue #276: ``model_tier`` is an optional positive int declaring the
    LLM tier this skill expects (1 = daily, 2 = deep reasoning, …). The
    harness escalates the active model when the skill is loaded. ``None``
    means "no opinion".
    """
    if not raw.startswith("---"):
        return "", "", [], [], None

    parts = raw.split("---", 2)
    if len(parts) < 3:
        return "", "", [], [], None

    yaml_text = parts[1].strip()
    try:
        import yaml
        data = yaml.safe_load(yaml_text)
        if not isinstance(data, dict):
            return "", "", [], [], None
    except Exception:
        return "", "", [], [], None

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

    # Issue #276: skill-declared LLM tier requirement
    raw_tier = data.get("model_tier")
    model_tier: int | None
    try:
        model_tier = int(raw_tier) if raw_tier is not None else None
        if model_tier is not None and model_tier < 1:
            model_tier = None  # invalid; treat as unspecified
    except (TypeError, ValueError):
        model_tier = None

    return name, description, tags, valid_refs, model_tier


def _build_jobs_inject_message(jobstore: Any) -> str | None:
    """Issue #154: render a pre-final-response reminder about background jobs.

    Returns None when there is nothing to report — no new completions and no
    still-running jobs. Only items finished since the last call are listed
    under 'Completed since last turn' (JobStore.reap_since_last is idempotent).
    """
    new_finished, running = jobstore.reap_since_last()
    if not new_finished and not running:
        return None

    lines = ["[Jobs update]"]
    if new_finished:
        lines.append("Completed since last turn:")
        for j in new_finished:
            if j.state.value == "done":
                ref = f"scratchpad://{j.result_ref}" if j.result_ref else "(no output)"
                size = f" ({j.result_summary})" if j.result_summary else ""
                lines.append(f"  - {j.id} ({j.fn_name}): done → {ref}{size}")
            elif j.state.value == "failed":
                lines.append(f"  - {j.id} ({j.fn_name}): failed — {j.error}")
            elif j.state.value == "cancelled":
                lines.append(f"  - {j.id} ({j.fn_name}): cancelled — {j.cancel_reason}")
    if running:
        if new_finished:
            lines.append("")
        lines.append("Still running:")
        for j in running:
            elapsed = j.elapsed_seconds
            elapsed_str = f" ({elapsed:.0f}s elapsed)" if elapsed else ""
            lines.append(f"  - {j.id} ({j.fn_name}): {j.state.value}{elapsed_str}")
    return "\n".join(lines)


def build_router(active_model: str | None = None) -> LLMRouter:
    """
    Build the LLM router with all available providers registered.

    Providers are registered if their credentials / config exist.
    The session's ``model`` attribute controls which provider is active at
    runtime via ``switch_model()``.

    Cloud providers (require API keys in .env):
      MiniMax    — MINIMAX_API_KEY  (uses Anthropic-compatible endpoint, name="minimax")
      Anthropic  — ANTHROPIC_API_KEY
      OpenAI     — OPENAI_API_KEY
      Codex      — Codex OAuth from `codex login` (explicit codex/<model> prefix)
      OpenRouter — OPENROUTER_API_KEY (OpenAI-compatible multi-vendor aggregator)
      DeepSeek   — DEEPSEEK_API_KEY   (official DeepSeek API, OpenAI-compatible)

    Local providers (no key needed; enable in loom.toml or via env):
      Ollama    — [providers.ollama] enabled=true  or  OLLAMA_BASE_URL
      LM Studio — [providers.lmstudio] enabled=true  or  LMSTUDIO_BASE_URL
    """
    from loom.core.cognition.router import get_default_model
    from loom.core.cognition.providers import (
        OllamaProvider,
        LMStudioProvider,
        CodexResponsesProvider,
        OpenAIProvider,
        OpenRouterProvider,
    )

    env = _load_env()
    cfg = _load_loom_config()
    router = LLMRouter()
    default = get_default_model()
    requested_model = active_model or default

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
                # 120s was too aggressive for MiniMax-M2.7 reasoning bursts —
                # httpx read timeout is the max gap between streamed chunks,
                # and heavy thinking turns routinely pause >120s before the
                # first token. 300s tolerates that without making true hangs
                # absurdly long to surface (still ~15min total over 3 retries).
                timeout=300.0,
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

    # OpenAI — official OpenAI API. Supports bare OpenAI model names and the
    # optional "openai/" Loom routing prefix for users who prefer explicitness.
    openai_key = (
        env.get("OPENAI_API_KEY")
        or os.environ.get("OPENAI_API_KEY", "")
    )
    if openai_key:
        openai_cfg = cfg.get("providers", {}).get("openai", {})
        base_url = openai_cfg.get("base_url", "") or OpenAIProvider.DEFAULT_BASE_URL
        raw_model = openai_cfg.get("default_model", OpenAIProvider.DEFAULT_MODEL)
        default_is_openai = default.startswith(("gpt-", "openai/"))
        openai_model = default if default_is_openai else raw_model
        router.register(
            OpenAIProvider(
                base_url=base_url,
                model=openai_model,
                api_key=openai_key,
            ),
            default=default_is_openai,
        )

    # Codex OAuth — explicit opt-in via codex/<model>. This avoids silently
    # burning OPENAI_API_KEY quota when users expect ChatGPT/Codex billing.
    codex_cfg = cfg.get("providers", {}).get("codex", {})
    codex_requested = requested_model.startswith("codex/")
    codex_default = default.startswith("codex/")
    codex_enabled = bool(codex_cfg.get("enabled", False))
    if codex_requested or codex_default or codex_enabled:
        codex_model = codex_cfg.get("default_model", CodexResponsesProvider.DEFAULT_MODEL)
        if requested_model.startswith("codex/"):
            codex_model = requested_model
        elif default.startswith("codex/"):
            codex_model = default
        else:
            codex_model = f"codex/{codex_model}"
        router.register(
            CodexResponsesProvider(
                model=codex_model,
                base_url=codex_cfg.get("base_url", "") or CodexResponsesProvider.DEFAULT_BASE_URL,
            ),
            default=codex_default or codex_requested,
            fallback=codex_default or codex_requested,
        )

    # OpenRouter — OpenAI-compatible aggregator. Single key fronts many vendors.
    openrouter_key = (
        env.get("OPENROUTER_API_KEY")
        or env.get("Openrouter_API_KEY")
        or os.environ.get("OPENROUTER_API_KEY", "")
    )
    if openrouter_key:
        openrouter_cfg = cfg.get("providers", {}).get("openrouter", {})
        base_url = (
            openrouter_cfg.get("base_url", "")
            or OpenRouterProvider.DEFAULT_BASE_URL
        )
        raw_model = openrouter_cfg.get(
            "default_model", OpenRouterProvider.DEFAULT_MODEL
        )
        openrouter_model = (
            default if default.startswith("openrouter/")
            else f"openrouter/{raw_model}"
        )
        router.register(
            OpenRouterProvider(
                base_url=base_url,
                model=openrouter_model,
                api_key=openrouter_key,
            ),
            default=default.startswith("openrouter/"),
        )

    # DeepSeek — official api.deepseek.com via Anthropic-compatible endpoint.
    # Reuses AnthropicProvider so thinking blocks (DeepSeek v4-pro reasoning)
    # are preserved across multi-turn tool use using the same code path that
    # MiniMax uses. Bare model names (e.g. "deepseek-v4-pro") routed by the
    # "deepseek-" prefix in LLMRouter._ROUTING — same pattern as MiniMax-* /
    # claude-*, so no Loom-side prefix stripping is needed.
    deepseek_key = (
        env.get("DEEPSEEK_API_KEY")
        or os.environ.get("DEEPSEEK_API_KEY", "")
    )
    if deepseek_key:
        deepseek_cfg = cfg.get("providers", {}).get("deepseek", {})
        base_url = (
            deepseek_cfg.get("base_url", "")
            or "https://api.deepseek.com/anthropic"
        )
        raw_model = deepseek_cfg.get("default_model", "deepseek-v4-pro")
        deepseek_model = default if default.startswith("deepseek-") else raw_model
        router.register(
            AnthropicProvider(
                api_key=deepseek_key,
                model=deepseek_model,
                base_url=base_url,
                name="deepseek",
                timeout=300.0,
            ),
            default=default.startswith("deepseek-"),
        )

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
            "Add MINIMAX_API_KEY, ANTHROPIC_API_KEY, OPENAI_API_KEY, OPENROUTER_API_KEY, or DEEPSEEK_API_KEY to .env, "
            "or enable a local provider in loom.toml ([providers.ollama] / [providers.lmstudio])."
        )
    return router


# ---------------------------------------------------------------------------
# LoomSession
# ---------------------------------------------------------------------------


class LoomSession:
    """Top-level session orchestrator.

    Section index (grep ``# SECTION N`` to jump):

      SECTION 1  — INIT & PERSONALITY
      SECTION 2  — BOOTSTRAP / start()
      SECTION 3  — LIFECYCLE (pause / resume / cancel / stop)
      SECTION 4  — TURN ORCHESTRATION (stream_turn)
      SECTION 5  — JUDGE / TURN HOOKS
      SECTION 6  — PLUGIN / SKILL LOADING
      SECTION 7  — HISTORY SANITIZATION
      SECTION 8  — TIER / TELEMETRY
      SECTION 9  — DISPATCH / TRACE / EVENTS
      SECTION 10 — HITL / SCOPE / GRANTS
      SECTION 11 — PARALLEL DISPATCH
      SECTION 12 — MEMORY INDEX / COMPACT
      SECTION 13 — ACCESSORS
    """

    # =========================================================================
    # SECTION 1 — INIT & PERSONALITY
    # =========================================================================
    def __init__(
        self,
        model: str,
        db_path: str,
        resume_session_id: str | None = None,
        workspace: Path | None = None,
        provisional_title: str | None = None,
    ) -> None:
        if model is None:
            from loom.core.cognition.router import get_default_model
            model = get_default_model()
        self._model = model
        self.session_id = resume_session_id or str(uuid.uuid4())[:8]
        self._resume = resume_session_id is not None
        self._provisional_title = provisional_title
        # Workspace root — all relative file paths resolve here; defaults to CWD
        self.workspace: Path = (workspace or Path.cwd()).resolve()
        self.router = build_router(self.model)

        # AgentLedger handles — opened in start(), closed in stop().
        # Until start() runs, these are None and every subsystem's optional
        # ledger_emitter wiring is a no-op (existing tests rely on this).
        self._ledger_store: LedgerStore | None = None
        self._ledger_emitter: LedgerEmitter | None = None
        # Step 5 cutover: envelope view projects from ledger instead of
        # ExecutionEnvelope.records. _call_metadata caches per-call args /
        # auth bits not stored in the ledger; _envelope_call_ids preserves
        # dispatch order for the current envelope.
        #
        # Lifecycle: _envelope_call_ids resets at each new envelope (per
        # tool batch). _call_metadata is turn-scoped — entries from
        # earlier batches in the same turn stay reachable for cross-
        # batch correlation. Today's per-turn call counts are small
        # (< 50); if that assumption ever breaks, switch to a per-batch
        # eviction policy.
        self._envelope_projector: LedgerEnvelopeProjector | None = None
        self._call_metadata: dict[str, CallMeta] = {}
        self._envelope_call_ids: list[str] = []
        self._envelope_metadata: dict[str, dict[str, str]] = {}
        # Per-turn ledger aggregators (#334). Reset at every turn_start.
        # _turn_token_usage feeds turn_end.token_usage so the placeholder
        # `{}` from Step 2 finally goes away. _thought_buffer holds the
        # raw reasoning text accumulated during the turn; commit-or-discard
        # at turn_end follows doc/53 §3.3 signal accumulator rules.
        self._turn_token_usage: dict[str, int] = {}
        self._turn_thought_text: str = ""
        self._turn_thought_started: float | None = None
        self._turn_tool_calls_in_turn: int = 0
        self._turn_judge_capture_signal: bool = False
        self._turn_artifact_max_size: int = 0

        # Build prompt stack from loom.toml [identity] config
        config = _load_loom_config()
        self._stack = PromptStack.from_config(config)
        self._episodic_compress_threshold: int = (
            config.get("memory", {}).get("episodic_compress_threshold", 30)
        )
        # Issue #181: cache the whole config so ``output_max_tokens`` resolves
        # cheaply on every LLM call, and responds live to ``set_model``.
        self._loom_config: dict = config
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
        _harness_cfg = config.get("harness", {})
        _strict_sandbox: bool = _harness_cfg.get("strict_sandbox", False)
        self._strict_sandbox = _strict_sandbox
        # Issue #197: JIT spill threshold for tool outputs. Tokens × 4 ≈
        # chars; 2000 tokens ≈ 8000 chars is conservative — most tool
        # outputs stay inline. 0 disables spilling entirely.
        _jit_tokens: int = int(_harness_cfg.get("jit_spill_threshold_tokens", 2000))
        self._jit_threshold_chars = max(0, _jit_tokens) * 4
        # Issue #197 Phase 2: mask tool observations older than N turns.
        # 20 = balance for deep-research tasks (15+ turns of fetch_url
        # collection followed by synthesis). 0 disables masking. JIT
        # ensures the original content is in scratchpad regardless.
        self._mask_age_turns: int = int(_harness_cfg.get("mask_age_turns", 20))

        # Issue #276: LLM tier system. ``[cognition.tiers]`` maps int → model
        # name. Skills declare ``model_tier: N`` in frontmatter; when activated,
        # the harness escalates to that tier (sticky). Agent / user can
        # downgrade explicitly. ``reminder_after_turns`` triggers a soft hint
        # — never auto-reverts.
        _tier_cfg = (config.get("cognition") or {}).get("tiers", {}) or {}
        self._tier_models: dict[int, str] = {}
        for k, v in _tier_cfg.items():
            try:
                tier_int = int(k)
                if isinstance(v, str) and v.strip():
                    self._tier_models[tier_int] = v.strip()
            except (TypeError, ValueError):
                continue
        self._default_tier: int = int(_tier_cfg.get("default_tier", 1) or 1)
        self._tier_reminder_after_turns: int = int(
            _tier_cfg.get("reminder_after_turns", 10) or 10
        )
        # ``None`` = follow ``_default_tier``; integer = sticky override.
        self._sticky_tier: int | None = None
        self._manual_model_override: bool = False
        # Counts consecutive turns at the *active* tier (sticky or default).
        # Reset on tier change; surfaces in TierExpiryHint after threshold.
        self._turns_at_current_tier: int = 0
        # Tracks "did we already emit a reminder this sticky session?" so we
        # don't spam the agent every turn after the threshold is hit.
        self._tier_reminder_emitted: bool = False
        # Issue #276: skill name → declared tier, populated during skill
        # bootstrap. ``_compute_skill_max_tier`` reads this synchronously
        # from inside stream_turn (procedural.get is async).
        self._skill_tier_snapshot: dict[str, int] = {}

        # Issue #271: when stop_reason='max_tokens' fires after 0 tools, inject
        # a system-reminder telling the agent to spill in-flight reasoning to
        # scratchpad and resume, instead of silently truncating. ``"auto"``
        # enables (default), ``"off"`` falls back to the original drop path.
        _continuation_mode = str(
            _harness_cfg.get("reasoning_continuation", "auto")
        ).lower()
        if _continuation_mode not in ("auto", "off"):
            _continuation_mode = "auto"
        self._reasoning_continuation_mode: str = _continuation_mode
        # Per-turn counter, reset at the top of stream_turn(). Capped at
        # ``_MAX_REASONING_CONTINUATIONS`` before falling through to
        # TurnDropped to prevent unbounded loops on pathologically long
        # prompts.
        self._consecutive_max_tokens: int = 0

        # Issue #196 Phase 2: turn-boundary LLM-as-judge. ``off`` disables;
        # ``auto`` runs async by default and auto-upgrades to sync for
        # high-stakes turns (CRITICAL trust / git push / PR ops).
        _verif_cfg = config.get("verification", {})
        self._judge_mode: str = str(_verif_cfg.get("judge_mode", "auto")).lower()
        if self._judge_mode not in ("off", "auto"):
            self._judge_mode = "auto"
        # Verdicts produced async land here; drained as <system-reminder>
        # at the start of the next stream_turn.
        self._pending_verdicts: list[str] = []
        # Issue #281 P3 Hook G/A — MemoryPulse appends PulseRecords here,
        # drained as <system-reminder> alongside verdicts in stream_turn.
        # Records (vs. bare strings) so Issue #377 can persist pulse_type
        # / pulse_source into session_log at drain time.
        self._pending_pulses: list[PulseRecord] = []
        # In-flight judge background tasks — kept so stop() can cancel them
        # cleanly and so a single turn can't fire judge twice.
        self._judge_tasks: set[asyncio.Task] = set()
        # #336 — memory compaction subscriber. The inline trigger from
        # stream_turn is replaced by a background task that subscribes
        # to ledger ``turn_end`` events. _compaction_lock serialises
        # concurrent runs (rare but possible when turn_end events
        # arrive faster than the LLM call inside compress_session can
        # finish). _pending_compactions buffers CompressDone events
        # for the next stream_turn to yield; if the session ends
        # before that yield, the signal is lost — which mirrors the
        # old inline behaviour when the turn that triggered compaction
        # was itself the last one.
        self._compaction_lock: asyncio.Lock = asyncio.Lock()
        self._pending_compactions: list[CompressDone] = []
        self._compaction_task: asyncio.Task | None = None

        self.registry = ToolRegistry()
        # Issue #154: JobStore + Scratchpad must exist before tools that may
        # submit to them (run_bash, fetch_url). Session-scoped, in-memory,
        # cleared on stop(). Final products go through memory system.
        from loom.core.jobs import JobStore, Scratchpad
        self._jobstore = JobStore()
        self._scratchpad = Scratchpad()

        # Issue #29 / Quest A Phase 4: optional OS-level sandbox via srt.
        # Defaults to ``backend = "none"`` so behaviour matches pre-#29.
        from loom.core.security import SandboxSettings
        _sandbox_settings = SandboxSettings.from_config(
            (config.get("security") or {}).get("sandbox")
        )
        self._sandbox_settings = _sandbox_settings

        from loom.platform.cli.tools import make_run_bash_tool, make_filesystem_tools
        _run_bash_tool = make_run_bash_tool(
            self.workspace,
            strict_sandbox=_strict_sandbox,
            jobstore=self._jobstore,
            scratchpad=self._scratchpad,
            sandbox=_sandbox_settings,
        )
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
        # Issue #147 Phase C.2: per-subsystem attributes were removed.
        # All memory access now flows through ``self._memory`` (built in start()).
        self._memory: MemoryFacade | None = None
        self._reflection: ReflectionAPI | None = None
        self._reflector: CounterFactualReflector | None = None
        self._pipeline: MiddlewarePipeline | None = None
        # Issue #213: result of the last StartupDiagnostic.run_all() call.
        # Populated at the end of start(); platforms (CLI/TUI/Discord) and
        # telemetry can consult it for structured boot-time health state.
        self._startup_report: "DiagnosticReport | None" = None
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
        # Issue #120 PR1: structured post-turn diagnostics (replaces maybe_evaluate).
        self._task_reflector: TaskReflector | None = None
        _refl_cfg = config.get("reflection", {})
        self._reflection_enabled: bool = bool(_refl_cfg.get("auto_reflect", True))
        visibility_raw = str(_refl_cfg.get("visibility", "summary")).lower()
        if visibility_raw not in ("off", "summary", "verbose"):
            visibility_raw = "summary"
        self._reflection_visibility: str = visibility_raw

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

        # Serialises stream_turn() so concurrent invocations on the same
        # session can never interleave message-history mutation. Without
        # this, a mid-turn interrupt that fires a new turn before the old
        # one fully unwinds (e.g. the Discord bot's cancel-and-relaunch
        # path) lets two LLM loops mutate self.messages in parallel — the
        # symptom is "the agent does the same thing twice" because turn B
        # observes turn A's partial assistant+tool state plus the new
        # user input.
        self._turn_lock: asyncio.Lock = asyncio.Lock()

        # Issue #42: Action lifecycle tracking. _lifecycle_events feeds
        # ActionStateChange/Rollback events to platform listeners; the
        # per-batch envelope id used by the action_records table and
        # by ExecutionEnvelopeView is the f"e{counter}" string below
        # (#337 dropped the parallel ExecutionEnvelope marker since it
        # only existed to back the now-removed _live_record_for bridge).
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

        # Issue #142: Agent self-observability
        _tele_cfg = config.get("telemetry", {})
        self._telemetry_enabled: bool = _tele_cfg.get("enabled", True)
        self._telemetry_persist_interval: int = int(
            _tele_cfg.get("persist_interval", 100)
        )
        _tele_dims = _tele_cfg.get("dimensions")
        self._telemetry_dimensions: tuple[str, ...] = (
            tuple(_tele_dims) if _tele_dims else DEFAULT_DIMENSIONS
        )
        self._telemetry: "AgentTelemetryTracker | None" = None
        # Dimensions currently in an alerting state. Edge-triggered: an
        # anomaly fires once when it rises into this set, then stays quiet
        # until it recovers (drops out) and crosses the threshold again.
        # Replaces the old per-turn dedup which still re-fired every turn
        # while the rolling window stayed bad (#219).
        self._telemetry_alerting_dims: set[str] = set()

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

    # =========================================================================
    # SECTION 2 — BOOTSTRAP / start()
    # =========================================================================
    async def start(self) -> None:
        await self._store.initialize()
        self._db_ctx = self._store.connect()
        self._db = await self._db_ctx.__aenter__()
        # Issue #147 Phase C.2: subsystems are local to start() — only the
        # facade survives as an attribute on the session.
        episodic = EpisodicMemory(self._db)
        emb_provider = build_embedding_provider(_load_env(), _load_loom_config())
        semantic = SemanticMemory(self._db, embedding_provider=emb_provider)
        procedural = ProceduralMemory(self._db)
        relational = RelationalMemory(self._db)
        # Issue #147 Phase C.1: ReflectionAPI / CounterFactualReflector
        # now take the MemoryFacade. They are constructed below, after
        # ``self._memory`` is built (search "Phase C.1: facade-aware
        # cognition wiring" in this file).
        self._session_log = SessionLog(self._db)

        # Issue #43: Memory Governance — always-on
        _memory_cfg = _load_loom_config().get("memory", {})
        _gov_cfg = dict(_memory_cfg.get("governance", {}))
        # Issue #281 P3: lifecycle throttle is owned by [memory.lifecycle]
        # but also gates session.stop()'s run_decay_cycle path, so plumb
        # the value through governor's config dict.
        _lifecycle_cfg = _memory_cfg.get("lifecycle", {})
        _gov_cfg.setdefault(
            "lifecycle_min_gap_minutes",
            float(_lifecycle_cfg.get("min_gap_minutes", 0.0)),
        )
        self._governor = MemoryGovernor(
            semantic=semantic,
            procedural=procedural,
            relational=relational,
            episodic=episodic,
            db=self._db,
            config=_gov_cfg,
            session_id=self.session_id,
        )
        # Issue #133: initialize health tracking and load prior session issues
        await self._governor.health.ensure_table()
        await self._governor.health.load_prior()
        # Inject health tracker into subsystems that record events
        semantic._health = self._governor.health
        self._session_log._health = self._governor.health

        # ── AgentLedger (Quest B) — open ledger.db before any subsystem
        # is constructed so each one can be wired with the emitter at
        # construction time. doc/53 §11.1 layered emit; §5.1 independent
        # ~/.loom/ledger.db. Opt-out via [ledger].enabled = false.
        _ledger_cfg = _load_loom_config().get("ledger", {})
        if _ledger_cfg.get("enabled", True):
            try:
                self._ledger_store = LedgerStore()
                await self._ledger_store.open()
                self._ledger_emitter = LedgerEmitter(
                    self._ledger_store, session_id=self.session_id
                )
                self._envelope_projector = LedgerEnvelopeProjector(
                    self._ledger_store, registry=self.registry
                )
            except Exception:
                logger.exception(
                    "ledger init failed — proceeding without ledger; "
                    "existing observability paths (envelope / session_log) "
                    "remain unchanged"
                )
                self._ledger_store = None
                self._ledger_emitter = None
                self._envelope_projector = None

        # #336 — start the memory compaction subscriber. Bound to the
        # ledger; if ledger init failed above, no subscriber spawns and
        # compaction never fires (which matches Discord-without-ledger
        # mode's existing behaviour for envelope projection).
        #
        # Spawning here (before facade build below) is safe even though
        # ``_run_compaction_check`` reaches into ``self._memory``: the
        # subscriber blocks on ``turn_end`` events, which only fire
        # from inside ``stream_turn``. ``stream_turn`` is only called
        # after ``start()`` returns, by which point the facade has been
        # built. (#346 review S1.)
        if self._ledger_store is not None:
            self._compaction_task = asyncio.create_task(
                self._compaction_subscriber_loop(),
                name=f"memory_compaction:{self.session_id}",
            )

        # Issue #147 Phase C: build the facade up-front so every later step
        # in start() (auto-import, MemoryIndexer, tool registration, etc.)
        # reads memory exclusively through ``self._memory``.
        search = MemorySearch(semantic, procedural)
        search._health = self._governor.health
        self._memory = MemoryFacade(
            semantic=semantic,
            procedural=procedural,
            relational=relational,
            episodic=episodic,
            search=search,
            session_log=self._session_log,
            governor=self._governor,
            ledger_emitter=self._ledger_emitter,
        )

        # Issue #142: agent self-observability. Noise-proportional-to-signal —
        # counters live on the hot path; DB flush is batched by persist_interval
        # and at stop(). Anomaly summaries inject only when a dimension reports
        # an issue.
        if self._telemetry_enabled:
            self._telemetry = AgentTelemetryTracker(
                self._db,
                self.session_id,
                dimensions=self._telemetry_dimensions,
                persist_interval=self._telemetry_persist_interval,
                stack=self._stack,
                messages_ref=self.messages,
                max_window=self.budget.total_tokens,
                turn_index_fn=lambda: self._turn_index,
            )
            await self._telemetry.ensure_table()

            # Issue #279: wire new dimensions with their data sources
            _ri = self._telemetry.get("runtime_identity")
            if _ri is not None:
                _model = self._active_model()
                _tier = self._active_tier()
                _ri.update(model=_model, tier=_tier,
                           tier_models=self._tier_models)
            _cb = self._telemetry.get("context_budget")
            if _cb is not None:
                _cb.set_budget(self.budget)

        # Build MemoryIndex and inject into system prompt
        # Issue #56: auto-import skills from workspace/skills/ and ~/.loom/skills/
        skill_catalog = await self._auto_import_skills()
        # Issue #279: seed loaded_skills baseline from auto-imported skills
        if self._telemetry is not None:
            _ls = self._telemetry.get("loaded_skills")
            if _ls is not None and skill_catalog:
                for s in skill_catalog:
                    _ls.add(s.name)
        indexer = MemoryIndexer(
            semantic, procedural, episodic, relational,
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
            await self._session_log.create_session(self.session_id, self.model, self._provisional_title)
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
            make_agent_health_tool,
            make_probe_file_tool,
            make_exec_escape_fn,
            make_fetch_url_tool,
            make_openai_image_generation_tool,
            make_memorize_tool,
            make_memory_health_tool,
            make_query_relations_tool,
            make_recall_period_tool,
            make_recall_tool,
            make_relate_tool,
            make_spawn_agent_tool,
            make_web_search_tool,
        )
        from loom.platform.cli.skill_tools import make_load_skill_tool
        # ``self._memory`` was built up-front (right after governor init).
        # See "Issue #147 Phase C: build the facade up-front" above.
        self.registry.register(make_recall_tool(self._memory))
        self.registry.register(make_recall_period_tool(self._memory))
        # PR-C4: when the governor blocks a write, fire the session-level
        # hook so the platform layer can surface a "governor rejected"
        # harness inline. Accept stays silent.
        def _governor_reject_hook(key: str, tier: str, contradictions: int) -> None:
            cb = getattr(self, "_on_governor_reject", None)
            if cb is not None:
                try:
                    cb(key, tier, contradictions)
                except Exception:
                    pass
        self.registry.register(
            make_memorize_tool(self._memory, on_reject=_governor_reject_hook)
        )
        self.registry.register(make_relate_tool(self._memory))
        self.registry.register(make_query_relations_tool(self._memory))
        self.registry.register(make_memory_health_tool(self._governor))

        # Issue #147 Phase C.1: facade-aware cognition wiring.
        # ReflectionAPI / CounterFactualReflector previously took
        # discrete subsystems; they now take the facade so the
        # construction site no longer reaches into ``self._semantic``
        # etc.  TaskReflector follows the same pattern further below.
        self._reflection = ReflectionAPI(self._memory)
        self._reflector = CounterFactualReflector(
            router=self.router,
            model=self.model,
            memory=self._memory,
        )

        # Issue #149: dream_cycle / memory_prune are now first-class memory
        # tools (formerly DreamingPlugin). Wired with dependency injection
        # so the factories stay session-agnostic.
        from loom.core.memory.maintenance import (
            make_dream_cycle_tool,
            make_memory_prune_tool,
        )

        async def _dream_llm_fn(messages: list[dict]) -> str:
            response = await self.router.chat(
                model=self.model, messages=messages, max_tokens=2048,
            )
            return response.text or ""

        self.registry.register(
            make_dream_cycle_tool(
                self._memory.semantic, self._memory.relational, _dream_llm_fn,
                db=self._db,
            )
        )
        self.registry.register(make_memory_prune_tool(self._memory.semantic))

        if self._telemetry is not None:
            self.registry.register(make_agent_health_tool(self._telemetry))

        # Issue #283: probe_file — agent-initiated context establishment
        self.registry.register(make_probe_file_tool())

        # Issue #56: Register load_skill tool with outcome tracker
        from loom.core.memory.skill_outcome import SkillOutcomeTracker
        self._skill_outcome_tracker = SkillOutcomeTracker(
            procedural=self._memory.procedural,
            semantic=self._memory.semantic,
            session_id=self.session_id,
        )


        # Issue #120 PR1: TaskReflector produces structured diagnostics at
        # each TurnDone, replacing the scalar self-assessment path.
        self._task_reflector = TaskReflector(
            router=self.router,
            model=self.model,
            memory=self._memory,
            session_id=self.session_id,
            enabled=self._reflection_enabled,
            visibility=self._reflection_visibility,
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

        # Issue #279: track loaded skills in telemetry
        def _on_skill_loaded(skill_name: str) -> None:
            if self._telemetry is not None:
                _ls = self._telemetry.get("loaded_skills")
                if _ls is not None:
                    _ls.add(skill_name)

        self.registry.register(make_load_skill_tool(
            self._memory.procedural, skills_dirs,
            outcome_tracker=self._skill_outcome_tracker,
            semantic=self._memory.semantic,
            turn_index_fn=lambda: self._turn_index,
            skill_check_manager=self._skill_check_manager,
            relational=self._memory.relational,
            confirm_fn=lambda call: self._confirm_fn(call),
        ))

        # Issue #276: agent-side tier control. Only register when at least
        # one tier is configured — running on a session with no [cognition.tiers]
        # block has no use for this tool and we'd just confuse the agent
        # with options that map to nothing.
        if self._tier_models:
            from loom.platform.cli.tools import (
                make_request_model_tier_tool,
                make_clear_model_tier_tool,
            )
            self.registry.register(make_request_model_tier_tool(self))
            self.registry.register(make_clear_model_tier_tool(self))

        # Issue #64 Phase B: Register unload_skill tool
        from loom.platform.cli.skill_tools import make_unload_skill_tool
        self.registry.register(make_unload_skill_tool(
            self._skill_check_manager,
        ))

        # doc/54 §4.3 / §5 P0-5: Register skill_review tool — agent's
        # conversational entry to per-skill ledger history. Read-only;
        # no-op if the ledger isn't available.
        from loom.platform.cli.skill_tools import make_skill_review_tool
        self.registry.register(make_skill_review_tool(self._ledger_store))

        # Issue #385: ledger_recall — story-form recall over the ledger,
        # joined with session_log so each turn quotes the user's message.
        from loom.platform.cli.tools import make_ledger_recall_tool
        self.registry.register(
            make_ledger_recall_tool(self._ledger_store, self._memory)
        )


        # Register web tools (Phase 5D)
        self.registry.register(make_fetch_url_tool(
            jobstore=self._jobstore,
            scratchpad=self._scratchpad,
        ))
        env = _load_env()
        brave_key = env.get("brave_search_key") or env.get("BRAVE_SEARCH_KEY", "")
        if brave_key:
            self.registry.register(make_web_search_tool(brave_key))
        _image_cfg = (_load_loom_config().get("tools", {}).get("image", {}) or {})
        _image_enabled = bool(_image_cfg.get("enabled", False))
        _image_provider = str(_image_cfg.get("default_provider", "")).strip().lower()
        _openai_image_cfg = (_image_cfg.get("openai", {}) or {})
        _openai_image_enabled = bool(_openai_image_cfg.get("enabled", True))
        if _image_enabled and _image_provider == "openai" and _openai_image_enabled:
            self.registry.register(
                make_openai_image_generation_tool(
                    self.workspace,
                    default_model=str(
                        _openai_image_cfg.get("model", "gpt-image-2")
                    ).strip() or "gpt-image-2",
                    default_auth_mode=str(
                        _openai_image_cfg.get("auth_mode", "auto")
                    ).strip() or "auto",
                )
            )
        elif _image_enabled and _image_provider and _image_provider != "openai":
            logger.warning(
                "[tools.image] provider %r is not supported; skipping image_generate.",
                _image_provider,
            )

        # Register sub-agent tool (Phase 5E)
        self.registry.register(make_spawn_agent_tool(self))

        # Issue #205: Single-tool TaskList (cognitive checklist for the main
        # agent). Replaced the 5-tool surface (#153) with one task_write —
        # see loom/platform/cli/tools.py for the rationale. Cross-session
        # continuity is handled by the memory system, not the TaskList itself;
        # each session starts with a clean list.
        from loom.core.tasks.manager import TaskListManager
        from loom.platform.cli.tools import make_task_write_tool
        self._tasklist_manager = TaskListManager(session_id=self.session_id)
        self.registry.register(
            make_task_write_tool(
                self._tasklist_manager, ledger_emitter=self._ledger_emitter
            )
        )

        # Issue #375: Pursuit tools — long-lived task artifacts as markdown
        # under ~/.loom/pursuits/. Cross-session by design (unlike TaskList).
        # Stateless wrt session so one PursuitStore instance is fine.
        from loom.core.tasks.pursuit import PursuitStore
        from loom.platform.cli.tools import (
            make_pursuit_list_tool,
            make_pursuit_read_tool,
            make_pursuit_write_tool,
        )
        pursuit_store = PursuitStore()
        self.registry.register(make_pursuit_list_tool(pursuit_store))
        self.registry.register(make_pursuit_read_tool(pursuit_store))
        self.registry.register(make_pursuit_write_tool(pursuit_store))

        # Issue #154: async job inspection tools. The JobStore + Scratchpad
        # themselves are created in __init__ so run_bash/fetch_url can close
        # over them; only the inspection tools are registered here.
        from loom.platform.cli.tools import (
            make_jobs_list_tool,
            make_jobs_status_tool,
            make_jobs_await_tool,
            make_jobs_cancel_tool,
            make_scratchpad_read_tool,
        )
        self.registry.register(make_jobs_list_tool(self._jobstore))
        self.registry.register(make_jobs_status_tool(self._jobstore))
        self.registry.register(make_jobs_await_tool(self._jobstore))
        self.registry.register(make_jobs_cancel_tool(self._jobstore))
        self.registry.register(make_scratchpad_read_tool(self._scratchpad))

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

        # Issue #197: JIT must wrap LifecycleMiddleware on the outside so
        # post_validators still see the full output for heuristic checks
        # (run_bash traceback detection, etc.). JIT only mutates what
        # propagates out to the message history.
        self._pipeline = MiddlewarePipeline(
            [
                JITRetrievalMiddleware(
                    scratchpad=self._scratchpad,
                    registry=self.registry,
                    threshold_chars=self._jit_threshold_chars,
                ),
                LifecycleMiddleware(
                    registry=self.registry,
                    on_lifecycle=self._on_lifecycle,
                    on_state_change=self._on_state_change,
                    ledger_emitter=self._ledger_emitter,
                ),
                TraceMiddleware(on_trace=self._on_trace),
                SchemaValidationMiddleware(registry=self.registry),
                self._legitimacy_guard,
                BlastRadiusMiddleware(
                    perm_ctx=self.perm,
                    confirm_fn=self._confirm_tool_cli,
                    exec_escape_fn=_exec_escape_fn,
                    registry=self.registry,
                    ledger_emitter=self._ledger_emitter,
                ),
                LifecycleGateMiddleware(
                    registry=self.registry,
                    skill_check_manager=self._skill_check_manager,
                ),
            ]
        )

        # Issue #213: post-wiring health check. Runs sequentially with
        # only handle inspections / counts (no LLM round-trips), so
        # startup latency stays unchanged. A failing check never aborts
        # startup — operators see structured failures at the top of the
        # session log instead of discovering them on the first turn.
        self._startup_report = await StartupDiagnostic().run_all(self)
        rendered = self._startup_report.render()
        # Console for the operator …
        console.print(rendered)
        # … and structured log for ops / telemetry.
        if self._startup_report.all_passed:
            logger.info("startup_diagnostic: all checks passed")
        else:
            for f in self._startup_report.failures:
                logger.warning(
                    "startup_diagnostic: %s failed — %s (%s)",
                    f.name, f.summary, f.detail,
                )

        # Issue #281 P3 — MemoryPulse: build now (db + memory ready), wire
        # into governor for Hook A, then run Hook G so the brief lands in
        # _pending_pulses before the agent's first turn drains the buffer.
        from loom.core.memory.pulse import MemoryPulse
        self._pulse = MemoryPulse(
            db=self._db,
            semantic=self._memory.semantic,
            session_id=self.session_id,
            session_started_at=datetime.now(UTC),
            pending_buffer=self._pending_pulses,
        )
        self._governor.set_pulse(self._pulse)
        await self._pulse.session_brief()

    # ------------------------------------------------------------------
    # HITL pause / resume / cancel
    # ------------------------------------------------------------------

    # =========================================================================
    # SECTION 3 — LIFECYCLE (pause / resume / cancel / stop)
    # =========================================================================
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
        now_str = user_timestamp()
        self.messages.append({"role": "user", "content": f"[{now_str}]\n{message}"})
        asyncio.ensure_future(self._log_message("user", message, turn_index=self._turn_index))
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
        # Issue #154: cancel any in-flight background jobs with trace.
        # Must run BEFORE clearing self._db so any tool_end emissions still
        # have a live state to write to. Scratchpad is then cleared.
        if hasattr(self, "_jobstore"):
            try:
                await self._jobstore.cancel_all(reason="session_ended")
            except Exception as exc:
                logger.error("JobStore.cancel_all failed: %s", exc, exc_info=True)
        # Issue #196 Phase 2: cancel any in-flight judge tasks so a session
        # shutdown doesn't leave verdict callbacks running against a torn-down
        # router / message list.
        if getattr(self, "_judge_tasks", None):
            for _t in list(self._judge_tasks):
                _t.cancel()
            self._judge_tasks.clear()
        # #336 — cancel the compaction subscriber loop. Same rationale
        # as judge tasks: a torn-down router / governor / facade must
        # not have a background task still trying to call into them.
        if self._compaction_task is not None:
            self._compaction_task.cancel()
            try:
                await self._compaction_task
            except asyncio.CancelledError:
                pass
            except Exception:
                # Subscriber loop has its own outer except Exception,
                # so this branch should never fire. If it does, surface
                # the trace rather than silently swallow. (#346 review N2.)
                logger.exception(
                    "memory compaction subscriber raised during shutdown"
                )
            self._compaction_task = None
        if hasattr(self, "_scratchpad"):
            self._scratchpad.clear()
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
                    self._memory.episodic,
                    self._memory.semantic,
                    self.router,
                    self.model,
                    governor=self._governor,
                    telemetry=self._telemetry,
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
            if self._memory is not None:
                try:
                    from loom.core.cognition.counter_factual import SkillEvolutionHook
                    evolution_hook = SkillEvolutionHook(
                        router=self.router, model=self.model,
                        memory=self._memory,
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

            # Step 6: Flush agent telemetry (Issue #142). Always dirty-flushes
            # so cross-session queries see the final-state snapshot even if no
            # opportunistic flush fired.
            if self._telemetry is not None:
                try:
                    self._telemetry.mark_dirty()
                    await self._telemetry.flush()
                except Exception as exc:
                    logger.debug("Telemetry flush failed: %s", exc)
        finally:
            # Issue #61 Bug 2 + #120 PR1/PR2: wait for pending skill reflection
            # background tasks before closing the DB so their upsert writes can
            # complete.  Covers TaskReflector (task_reflect), its behavioural
            # post-hook (behavioural_triples).
            pending_evals = [
                t for t in asyncio.all_tasks()
                if t.get_name().startswith((
                    "task_reflect:", "behavioural_triples:",
                ))
            ]
            if pending_evals:
                done, still_pending = await asyncio.wait(pending_evals, timeout=5.0)
                if still_pending:
                    logger.warning(
                        "%d skill reflection(s) unfinished on shutdown",
                        len(still_pending),
                    )

            for client in self._mcp_clients:
                try:
                    await client.disconnect()
                except BaseException as exc:
                    # BaseException: catch GeneratorExit + CancelledError too.
                    # MCP stdio_client cleanup may race with event-loop shutdown;
                    # these errors are safe to swallow at this stage.
                    logger.debug("MCP client disconnect error: %s", exc)
            self._mcp_clients = []

            try:
                if db_ctx is not None:
                    await db_ctx.__aexit__(None, None, None)
                elif db is not None:
                    await db.close()
            except Exception as exc:
                logger.debug("DB close error during shutdown: %s", exc)

            # Close the ledger after the main db so any final emits in
            # the shutdown chain (memory compaction, etc.) still land.
            if self._ledger_store is not None:
                try:
                    await self._ledger_store.close()
                except Exception as exc:
                    logger.debug("ledger close error during shutdown: %s", exc)
                self._ledger_store = None
                self._ledger_emitter = None
                self._envelope_projector = None

    # ------------------------------------------------------------------
    # Streaming agent loop
    # ------------------------------------------------------------------

    # =========================================================================
    # SECTION 4 — TURN ORCHESTRATION (stream_turn)
    # =========================================================================
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
        # Serialise turns on this session. If a prior turn is still
        # unwinding (e.g. caller fired ``task.cancel()`` but did not
        # await the task), block here until it has fully exited so we
        # never run two LLM loops on a shared self.messages.
        if self._turn_lock.locked():
            logger.warning(
                "stream_turn: another turn still in flight on this session "
                "— waiting for it to release before starting (origin=%s)",
                origin,
            )
        await self._turn_lock.acquire()
        # ── AgentLedger turn scope (Quest B Phase 2 Step 2 commit 6) ──
        # turn_id is set once per stream_turn invocation and never
        # rotates within the turn (doc/53 §4.4). correlation_id seeds a
        # fresh business-action chain; subagents and inner-loop tools
        # inherit it via contextvar. Tokens are reset in finally.
        _turn_id_v2 = f"turn_{uuid.uuid4().hex[:12]}"
        _turn_corr_id = new_correlation_id("turn")
        _ledger_turn_token: Any = None
        _ledger_corr_token: Any = None
        _turn_start_monotonic = time.monotonic()
        # #334 — per-turn ledger aggregator reset.
        self._turn_token_usage = {}
        self._turn_thought_text = ""
        self._turn_thought_started = _turn_start_monotonic
        self._turn_tool_calls_in_turn = 0
        self._turn_judge_capture_signal = False
        self._turn_artifact_max_size = 0
        # #337 review S2 — _call_metadata is turn-scoped per its own
        # docstring; clear at turn start so long sessions don't leak
        # full_args (which can be sizeable for write_file / shell tool
        # batches).
        self._call_metadata.clear()
        self._envelope_metadata.clear()
        if self._ledger_emitter is not None:
            _ledger_turn_token = set_turn_id(_turn_id_v2)
            _ledger_corr_token = set_correlation(_turn_corr_id)
            try:
                await self._emit_ledger_turn_start(_turn_id_v2)
            except Exception:
                logger.exception(
                    "ledger turn_start emit failed; continuing"
                )
        try:
            self._current_origin = origin

            # #336 — surface CompressDone events buffered by the
            # compaction subscriber from the previous turn. Yielded
            # before any other turn-level events so Discord shows the
            # status message at the natural turn boundary.
            if self._pending_compactions:
                for _ev in self._pending_compactions:
                    yield _ev
                self._pending_compactions.clear()

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
            now_str = user_timestamp()
            annotated = f"[{now_str}]\n{user_input}"
            self.messages.append({"role": "user", "content": annotated})
            asyncio.ensure_future(self._log_message("user", annotated, turn_index=self._turn_index))

            # Issue #196 Phase 2 + #281 P3 — drain async-produced reminders
            # (judge verdicts and MemoryPulse hooks) before the agent's next
            # LLM call. Order is incidental: both render as identical
            # <system-reminder> blocks, indistinguishable to the model.
            #
            # Issue #377: pulses also get a session_log row (role='system')
            # tagged with pulse_type/source so noise-vs-utility analytics
            # can run a straight SELECT instead of guessing from agent
            # behaviour. Verdicts stay log-less for now — separate scope.
            #
            # Persistence is best-effort by design: ``_log_message`` is
            # scheduled fire-and-forget (so a slow DB never stalls the
            # turn) and ``_pending_pulses.clear()`` runs synchronously
            # below. If a write fails after clear(), that one inject is
            # lost from the log — the <system-reminder> still reached
            # the agent. Analytics is observational, not auditable; a
            # dropped row skews counts at the margin but doesn't break
            # any contract. If/when we want at-least-once persistence,
            # await the tasks before clear() — accept the latency cost.
            for body in self._pending_verdicts:
                self.messages.append({
                    "role": "user",
                    "content": f"<system-reminder>\n{body}\n</system-reminder>",
                })

            # Issue #378: a single session-compression batch can fire
            # several Hook A contradictions back-to-back (98% of historical
            # injects, peak 6-in-a-batch). Fold them into one inject so the
            # agent sees one bullet list instead of N stacked reminders.
            # session_log keeps one row per record — folding is agent-side
            # noise reduction only, the analytics granularity is preserved.
            contradictions = [
                r for r in self._pending_pulses
                if r.pulse_type == PULSE_TYPE_CONTRADICTION
            ]
            others = [
                r for r in self._pending_pulses
                if r.pulse_type != PULSE_TYPE_CONTRADICTION
            ]

            def _emit_reminder(body: str) -> None:
                self.messages.append({
                    "role": "user",
                    "content": f"<system-reminder>\n{body}\n</system-reminder>",
                })

            def _log_record(record: PulseRecord) -> None:
                asyncio.ensure_future(self._log_message(
                    "system", record.text,
                    metadata={
                        "pulse_type": record.pulse_type,
                        "pulse_source": record.pulse_source,
                    },
                    turn_index=self._turn_index,
                ))

            for record in others:
                _emit_reminder(record.text)
                _log_record(record)

            if len(contradictions) >= 2:
                _emit_reminder(fold_contradiction_pulses(contradictions))
                for record in contradictions:
                    _log_record(record)
            else:
                for record in contradictions:
                    _emit_reminder(record.text)
                    _log_record(record)
            self._pending_verdicts.clear()
            self._pending_pulses.clear()

            await self._memory.episodic.write(
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
            # Issue #197 Phase 2: fold stale tool observations into scratchpad
            # refs. Runs once per turn; safe because JIT (Phase 1) already
            # guarantees the original content lives in scratchpad before this
            # rewrite happens.
            self._apply_observation_masking()

            # Compress before the first LLM call if already over threshold.
            # (budget.used_tokens reflects the last response's actual token count,
            # so this check is accurate from turn 2 onward.)
            # ``should_compress`` is OR over token + block dimensions —
            # MiniMax's 2013-block wire cap can bite long before the
            # token budget does, especially on parallel-tool sessions.
            self.budget.update_block_count(self.messages)
            if self.budget.should_compress():
                console.print(
                    f"[yellow]  Context at {self.budget.format_pressure()} — "
                    f"compressing…[/yellow]"
                )
                await self._smart_compact()

            tools = self.registry.to_openai_schema()
            tool_count = 0
            input_tokens = 0
            output_tokens = 0
            cache_read_input_tokens = 0
            cache_creation_input_tokens = 0
            t0 = time.monotonic()

            # Issue #271: clear per-turn reasoning-continuation counter. The
            # detector at the bottom of the stop_reason switch increments it
            # whenever max_tokens fires after 0 tools; resetting here ensures
            # state from a prior turn doesn't bleed into this one.
            self._consecutive_max_tokens = 0

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

            # Issue #205: TaskList self-check. On end_turn we inject a reminder
            # at most once per stream_turn if the list still has active nodes,
            # nudging the agent to either continue executing or rewrite the list
            # to mark abandonment.
            _tasklist_selfcheck_done = False
            # Issue #154: Jobs status injection, also at most once per stream_turn.
            # Reports newly-finished and still-running background jobs so the
            # agent can absorb progress without polling.
            _jobs_inject_done = False
            # Issue #196 Phase 2: judge runs at most once per stream_turn —
            # idempotent guard mirrors the pattern above.
            _judge_done = False

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

                # Issue #276: skill-driven tier escalation. If the agent has
                # loaded a skill with model_tier=N higher than the current
                # active tier, escalate sticky before the next LLM call.
                # Only ever moves UP automatically; downgrade requires explicit
                # request_model_tier / /tier from agent or user.
                _skill_tier = self._compute_skill_max_tier()
                if _skill_tier > self._active_tier():
                    _ev = self._set_sticky_tier(
                        _skill_tier,
                        reason=f"skill_max={_skill_tier}",
                        source="skill",
                    )
                    if _ev is not None:
                        yield _ev

                # Resolve the active model for this call from the tier system.
                # When tiers aren't configured, _active_model() falls back to
                # self.model (legacy behavior preserved).
                _active_model = self._active_model()

                async for chunk, final in self.router.stream_chat(
                    model=_active_model,
                    messages=self.messages,
                    tools=tools,
                    max_tokens=_resolve_output_max_tokens(
                        self._loom_config, _active_model, router=self.router,
                    ),
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
                # #334 — emit model_event + accumulate token_usage for turn_end.
                await self._emit_ledger_model_event(_active_model, response)
                # Issue #142: feed the authoritative total into the context_layout
                # dimension so layer attribution reflects real usage, not estimates.
                if self._telemetry is not None:
                    ctx_dim = self._telemetry.get("context_layout")
                    if ctx_dim is not None:
                        ctx_dim.update_total(response.input_tokens)
                        self._telemetry.mark_dirty()
                self.messages.append(response.raw_message)
                input_tokens = response.input_tokens  # report latest actual value
                output_tokens += response.output_tokens
                # Cache tokens follow the same replace semantics as input_tokens:
                # each LLM call's usage already reflects the cumulative cached
                # context for that call. Accumulating with += would double-count
                # across tool-call rounds and skew the displayed hit rate.
                cache_read_input_tokens = getattr(response, "cache_read_input_tokens", 0) or 0
                cache_creation_input_tokens = getattr(response, "cache_creation_input_tokens", 0) or 0

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
                    turn_index=self._turn_index,
                ))

                if response.stop_reason == "end_turn":
                    _outcome, _summary = _extract_envelope_outcome_marker(
                        response.text or ""
                    )
                    if (_outcome or _summary) and self._recent_envelopes:
                        _last_envelope = self._recent_envelopes[-1]
                        if _last_envelope.turn_index == self._turn_index:
                            if _outcome:
                                _last_envelope.outcome = _outcome
                            if _summary:
                                _last_envelope.outcome_summary = _summary

                    # Issue #205: Pre-final-response self-check. If the TaskList
                    # has pending/in-progress nodes and we haven't already nudged
                    # this turn, inject a reminder and loop back into the model.
                    # This catches the autonomy stall mode where an agent creates
                    # a plan, does some prep work, then ends silently without
                    # executing the planned nodes (cf. graph 66859851, 2026-04-17).
                    if (
                        not _tasklist_selfcheck_done
                        and hasattr(self, "_tasklist_manager")
                        and self._tasklist_manager.has_active_nodes()
                    ):
                        reminder = self._tasklist_manager.build_self_check_message()
                        if reminder:
                            self.messages.append({
                                "role": "user",
                                "content": f"<system-reminder>\n{reminder}\n</system-reminder>",
                            })
                            _tasklist_selfcheck_done = True
                            continue

                    # Issue #154: after TaskList self-check, report background
                    # jobs status if there's something new to say. Order matters:
                    # TaskList comes first so the agent sees planning context
                    # before absorbing IO updates.
                    if (
                        not _jobs_inject_done
                        and hasattr(self, "_jobstore")
                    ):
                        jobs_msg = _build_jobs_inject_message(self._jobstore)
                        if jobs_msg:
                            self.messages.append({
                                "role": "user",
                                "content": f"<system-reminder>\n{jobs_msg}\n</system-reminder>",
                            })
                            _jobs_inject_done = True
                            continue

                    # Issue #142 / #219: nudge the agent with a self-
                    # observability alert when a dimension *transitions* into
                    # an anomalous state. Edge-triggered against
                    # `_telemetry_alerting_dims` so repeat anomalies don't
                    # flood the context every turn while the rolling window
                    # stays bad.
                    if self._telemetry is not None:
                        currently = self._telemetry.alerting_dimensions()
                        alert = self._telemetry.anomaly_report(
                            since=self._telemetry_alerting_dims
                        )
                        # Update tracked set regardless: dimensions that have
                        # recovered drop out, so they can re-fire on next entry.
                        self._telemetry_alerting_dims = currently
                        if alert:
                            self.messages.append({
                                "role": "user",
                                "content": f"<system-reminder>\n{alert}\n</system-reminder>",
                            })
                            continue

                    # Issue #196 Phase 2: turn-boundary judge. Predicate is pure
                    # — only the *fire* path consumes the per-stream_turn token.
                    # That matters when an earlier end_turn iteration was a
                    # text-only response (no MUTATES, no claim) followed by a
                    # reminder loop that surfaces real tool work later: we don't
                    # want to burn the judge slot on the empty pre-state.
                    if not _judge_done and self._judge_mode != "off":
                        _final_text = self._extract_final_assistant_text()
                        if gate_should_fire(self._turn_envelopes(), _final_text):
                            _judge_done = True
                            _verdict_reminder = await self._maybe_run_judge(_final_text)
                            if _verdict_reminder:
                                self.messages.append({
                                    "role": "user",
                                    "content": f"<system-reminder>\n{_verdict_reminder}\n</system-reminder>",
                                })
                                continue

                    self._turn_index += 1

                    # Issue #58: trigger skill self-assessment before TurnDone
                    self._trigger_skill_assessment()

                    # #336 — mid-session episodic compaction was previously
                    # an inline ``ep_count >= threshold`` check here. The
                    # trigger is now ``_compaction_subscriber_loop``, fed
                    # by ledger ``turn_end`` events. ``CompressDone`` lands
                    # in ``self._pending_compactions`` and surfaces at the
                    # start of the next stream_turn; this turn's TurnDone
                    # no longer waits on the LLM compaction call.

                    self._last_think = "".join(_think_parts).strip()
                    _tier_hint = self._tick_tier_counter()
                    if _tier_hint is not None:
                        yield _tier_hint
                    yield TurnDone(
                        tool_count=tool_count,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        elapsed_ms=(time.monotonic() - t0) * 1000,
                        cache_read_input_tokens=cache_read_input_tokens,
                        cache_creation_input_tokens=cache_creation_input_tokens,
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
                    _envelope_id = f"e{self._envelope_counter}"
                    _intent = ""
                    if len(response.tool_uses) > 1:
                        _intent = _extract_envelope_intent_marker(response.text or "")
                    if _intent:
                        self._envelope_metadata.setdefault(_envelope_id, {})[
                            "intent"
                        ] = _intent
                    # Step 5: reset per-batch call_id ordering (call_metadata
                    # itself is turn-scoped and stays). Pre-populate with
                    # the tool_uses about to dispatch so EnvelopeStarted
                    # has the same node_count as the legacy path.
                    self._envelope_call_ids = []
                    for _tu in response.tool_uses:
                        self._envelope_call_ids.append(_tu.id)
                        # Best-effort args_preview from first string arg —
                        # mirrors the legacy view's args extraction.
                        _first = next(iter(_tu.args.values()), "") if _tu.args else ""
                        _preview = (
                            _first.replace("\n", "↵")[:60]
                            if isinstance(_first, str)
                            else ""
                        )
                        self._call_metadata[_tu.id] = CallMeta(
                            call_id=_tu.id,
                            tool_name=_tu.name,
                            full_args=dict(_tu.args) if _tu.args else {},
                            args_preview=_preview,
                        )
                    _batch_t0 = time.monotonic()

                    # Yield EnvelopeStarted *before* ToolBegin events
                    yield EnvelopeStarted(envelope=await self._build_envelope_view(_batch_t0))

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
                            self._telemetry_record_tool(
                                tu.name, result=result, duration_ms=duration_ms,
                            )
                            # Drain lifecycle events queued during dispatch
                            while not self._lifecycle_events.empty():
                                yield self._lifecycle_events.get_nowait()
                            # Issue #106: Yield envelope update after each tool completes
                            yield EnvelopeUpdated(envelope=await self._build_envelope_view(_batch_t0))
                            # Issue #197 Phase 2: tag for observation masking.
                            # Underscore-prefixed keys are ignored by provider
                            # conversion (verified for OpenAI native + Anthropic
                            # in providers.py — they explicitly read only role,
                            # tool_call_id, content). If a future provider
                            # changes that, _emit_turn / _tool_name MUST be
                            # preserved — they're the data basis of masking.
                            _tool_msg = self.router.format_tool_result(
                                self.model, tu.id, tool_output, result.success,
                            )
                            _tool_msg["_emit_turn"] = self._turn_index
                            _tool_msg["_tool_name"] = tu.name
                            self.messages.append(_tool_msg)
                            asyncio.ensure_future(self._log_message(
                                "tool", tool_output[:500],
                                {
                                    "tool_call_id": tu.id,
                                    "tool_name": tu.name,
                                    "emit_turn": self._turn_index,
                                },
                                turn_index=self._turn_index,
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
                                        envelope=await self._build_envelope_view(_batch_t0)
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
                            self._telemetry_record_tool(
                                tu.name, result=result, duration_ms=duration_ms,
                            )
                            # Final drain of lifecycle events after dispatch
                            while not self._lifecycle_events.empty():
                                yield self._lifecycle_events.get_nowait()
                            # Issue #106: Yield envelope update after each tool completes
                            yield EnvelopeUpdated(envelope=await self._build_envelope_view(_batch_t0))
                            # Issue #197 Phase 2: tag for observation masking.
                            _tool_msg = self.router.format_tool_result(
                                self.model, tu.id, tool_output, result.success,
                            )
                            _tool_msg["_emit_turn"] = self._turn_index
                            _tool_msg["_tool_name"] = tu.name
                            self.messages.append(_tool_msg)
                            asyncio.ensure_future(self._log_message(
                                "tool", tool_output[:500],
                                {
                                    "tool_call_id": tu.id,
                                    "tool_name": tu.name,
                                    "emit_turn": self._turn_index,
                                },
                                turn_index=self._turn_index,
                            ))

                    # ── Issue #106: Envelope completed ─────────────────────────
                    _completed_view = await self._build_envelope_view(_batch_t0)
                    _completed_view = await self._author_envelope_outcome_summary(
                        _completed_view,
                        model=_active_model,
                    )
                    yield EnvelopeCompleted(envelope=_completed_view)
                    # Keep last 50 envelopes — covers TUI history display *and*
                    # the Issue #196 turn-boundary judge digest. 10 was enough for
                    # the former; tool-heavy turns (Suno session, deep refactor)
                    # routinely exceed it within a single turn and would otherwise
                    # have older nodes evicted before the judge sees them.
                    self._recent_envelopes.append(_completed_view)
                    if len(self._recent_envelopes) > 50:
                        self._recent_envelopes = self._recent_envelopes[-50:]

                    # ── Issue #108: Grants snapshot after each batch ──────────
                    yield self._build_grants_snapshot()

                    # Issue #142: opportunistic flush — keeps DB state fresh
                    # for any out-of-band `agent_health` query without adding
                    # per-tool I/O. No-op when below persist_interval.
                    if self._telemetry is not None:
                        try:
                            await self._telemetry.maybe_flush()
                        except Exception as exc:
                            logger.debug("Telemetry flush deferred: %s", exc)

                    # Check budget after tool results are appended — the next LLM
                    # call in this loop will include them and may push over the limit.
                    # Refresh block_count first: a parallel-tool batch just
                    # appended N tool_result rows, but ``record_response``
                    # only updates token totals, so without this resync
                    # ``should_compress`` would still see the previous turn's
                    # block reading and miss a wire-cap overrun until the
                    # next turn boundary.
                    self.budget.update_block_count(self.messages)
                    if self.budget.should_compress():
                        console.print(
                            f"[yellow]  Context at {self.budget.format_pressure()}"
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
                            _tier_hint = self._tick_tier_counter()
                            if _tier_hint is not None:
                                yield _tier_hint
                            yield TurnDone(
                                tool_count=tool_count,
                                input_tokens=input_tokens,
                                output_tokens=output_tokens,
                                elapsed_ms=(time.monotonic() - t0) * 1000,
                                cache_read_input_tokens=cache_read_input_tokens,
                                cache_creation_input_tokens=cache_creation_input_tokens,
                            )
                            return
                else:
                    # Unexpected stop_reason (e.g. 'max_tokens', unknown provider value).
                    _raw_stop = getattr(response, "stop_reason", "unknown")

                    # Issue #271: max_tokens with 0 tools is the high-reasoning
                    # truncation case. The model exhausted its output budget
                    # purely on reasoning — no tool calls, no recovery hook.
                    # Inject a system-reminder telling the agent to spill
                    # in-flight thinking to scratchpad and resume next round,
                    # then re-enter the while loop so the LLM gets another
                    # response window. Capped at _MAX_REASONING_CONTINUATIONS
                    # to prevent unbounded loops; falls through to TurnDropped
                    # afterwards. The truncated assistant text was already
                    # appended to ``self.messages`` in the streaming branch
                    # above, so the agent has its own prior reasoning visible
                    # when it resumes.
                    if self._should_continue_reasoning(_raw_stop, tool_count):
                        self._consecutive_max_tokens += 1
                        _ref = (
                            f"auto_reasoning_t{self._turn_index}"
                            f"_{self._consecutive_max_tokens}"
                        )
                        logger.info(
                            "reasoning_continuation: max_tokens after 0 tools, "
                            "attempt %d/%d — injecting reminder (ref=%s)",
                            self._consecutive_max_tokens,
                            _MAX_REASONING_CONTINUATIONS,
                            _ref,
                        )
                        yield ReasoningContinuation(
                            attempt=self._consecutive_max_tokens,
                            max_attempts=_MAX_REASONING_CONTINUATIONS,
                        )
                        self.messages.append({
                            "role": "user",
                            "content": (
                                "<system-reminder>\n"
                                f"你的回答超出單輪輸出上限"
                                f"（延伸 {self._consecutive_max_tokens}/"
                                f"{_MAX_REASONING_CONTINUATIONS} 次）。"
                                "為了不打斷推理：\n"
                                f"1. 把目前進行中的關鍵推理摘要寫到 scratchpad，"
                                f"建議 ref `{_ref}`\n"
                                "2. 在這一輪精簡產出 —— 引用該 ref 接續，"
                                "避免重複展開所有推理\n"
                                "3. 如果題目本質需要長篇連續回應，"
                                "先輸出最關鍵的結論部分，剩餘細節放 scratchpad\n"
                                "</system-reminder>"
                            ),
                        })
                        # Re-enter the while loop for another LLM call. The
                        # appended reminder + truncated prior assistant text
                        # both feed into the next sanitize+stream pass.
                        continue

                    # Either disabled, not max_tokens, or budget exhausted →
                    # original drop path (logger warning + TurnDropped event).
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
            _tier_hint = self._tick_tier_counter()
            if _tier_hint is not None:
                yield _tier_hint
            yield TurnDone(
                tool_count=tool_count,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                elapsed_ms=(time.monotonic() - t0) * 1000,
                cache_read_input_tokens=cache_read_input_tokens,
                cache_creation_input_tokens=cache_creation_input_tokens,
                stop_reason=_stop_reason,
            )
        finally:
            try:
                # ── AgentLedger turn_end + token reset ────────────────
                if self._ledger_emitter is not None and _ledger_turn_token is not None:
                    import sys as _sys
                    _exc_type = _sys.exc_info()[0]
                    if _exc_type is None:
                        _outcome = "clean"
                    elif isinstance(_exc_type, type) and issubclass(
                        _exc_type, asyncio.CancelledError
                    ):
                        _outcome = "abandoned"
                    else:
                        _outcome = "error"
                    try:
                        _duration_ms = int(
                            (time.monotonic() - _turn_start_monotonic) * 1000
                        )
                        # #334 — mirror local accumulators into session state so
                        # commit-or-discard can run after the try/except scope.
                        try:
                            self._turn_thought_text = "".join(_think_parts)
                        except NameError:
                            # Exception before _think_parts was declared (rare).
                            self._turn_thought_text = ""
                        try:
                            self._turn_tool_calls_in_turn = tool_count
                        except NameError:
                            self._turn_tool_calls_in_turn = 0
                        await self._commit_or_discard_thought(_turn_id_v2, _outcome)
                        await self._emit_ledger_turn_end(
                            _turn_id_v2, _outcome, _duration_ms
                        )
                    except Exception:
                        logger.exception(
                            "ledger turn_end emit failed; continuing"
                        )
                    # ContextVar.reset() raises ValueError when the token was
                    # created in a different Context — happens when an async
                    # generator is closed via aclose() from a cancelling task
                    # that lives in a different Context than the originating
                    # iteration. The token will be garbage-collected with the
                    # frame anyway, so swallowing this is safe; what is NOT
                    # safe is letting the ValueError escape and shadow the
                    # turn_lock.release() below — which leaks the lock and
                    # wedges every subsequent stream_turn on this session.
                    try:
                        reset_turn_id(_ledger_turn_token)
                    except ValueError:
                        logger.debug(
                            "turn_id token reset skipped (different Context)"
                        )
                    try:
                        reset_correlation(_ledger_corr_token)
                    except ValueError:
                        logger.debug(
                            "correlation token reset skipped (different Context)"
                        )
            finally:
                # Lock release is the load-bearing invariant of this finally
                # block. Keep it in its own try/finally so no failure above
                # (ledger emit, ContextVar reset, etc.) can ever leak the
                # turn lock and brick the session.
                self._turn_lock.release()

    # ------------------------------------------------------------------
    # Issue #58: Skill self-assessment trigger
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Issue #120 PR1: Diagnostic subscriber plumbing
    # ------------------------------------------------------------------

    # =========================================================================
    # SECTION 5 — JUDGE / TURN HOOKS
    # =========================================================================
    def subscribe_diagnostic(
        self, callback: "Callable[[TaskDiagnostic], Awaitable[None]]",
    ) -> None:
        """Register a platform callback for each completed ``TaskDiagnostic``.

        CLI, TUI, and Discord each call this once after ``start()`` to
        render their own view of the diagnostic (status bar, side panel,
        Discord summary message).  Subscribers run after the diagnostic
        is already persisted — they are purely for display.
        """
        if self._task_reflector is not None:
            self._task_reflector.subscribe(callback)

    def _trigger_skill_assessment(self) -> None:
        """
        Fire-and-forget: schedule structured skill diagnostic if a skill was
        activated during this session (Issue #120 PR1).

        Called at each TurnDone point in stream_turn(). The LLM diagnostic
        runs as a background task and never blocks the conversation.
        """
        if (
            self._skill_outcome_tracker is None
            or self._task_reflector is None
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
            # Still drain the tracker so stale activations don't leak
            # across turns even when there was nothing to reflect on.
            self._skill_outcome_tracker.drain_for_reflection(self._turn_index)
            self._skill_outcome_tracker.pop_turn_tool_count()
            return

        self._task_reflector.maybe_reflect(
            tracker=self._skill_outcome_tracker,
            turn_index=self._turn_index,
            turn_summary=turn_summary,
            envelopes=list(self._recent_envelopes),
        )

    # ------------------------------------------------------------------
    # Issue #196 Phase 2: turn-boundary judge
    # ------------------------------------------------------------------

    def _extract_final_assistant_text(self) -> str:
        """Last assistant message's text content — what the agent claimed."""
        for msg in reversed(self.messages):
            if msg.get("role") == "assistant":
                content = msg.get("content", "")
                if isinstance(content, str):
                    return content
                # Anthropic-style content blocks: concatenate text blocks.
                if isinstance(content, list):
                    return "".join(
                        b.get("text", "") for b in content
                        if isinstance(b, dict) and b.get("type") == "text"
                    )
                return ""
        return ""

    def _turn_envelopes(self) -> list:
        """Envelopes completed during the current turn (turn_index)."""
        return [
            env for env in self._recent_envelopes
            if env.turn_index == self._turn_index
        ]

    async def _maybe_run_judge(self, final_text: str) -> str | None:
        """Dispatch for the turn-boundary judge — assumes gate already passed.

        Returns a reminder body for the SYNC path when the verdict is
        fail/uncertain (caller injects + continues). Returns None for the
        async path (verdict will land via ``_pending_verdicts`` next turn)
        or sync ``pass`` verdicts (no noise).
        """
        envelopes = self._turn_envelopes()
        digest = build_trace_digest(envelopes, final_text)

        if is_high_stakes(envelopes):
            verdict, response = await run_judge(self.router, self.model, digest)
            self._record_verdict_telemetry(verdict, sync=True)
            if response is not None:
                await self._emit_ledger_model_event(self.model, response)
            await self._emit_ledger_judge_verdict(verdict, "turn_final_text")
            if should_inject_reminder(verdict):
                return format_verdict_reminder(verdict)
            return None

        # Async path — fire-and-forget, verdict lands in next turn.
        task = asyncio.create_task(
            self._run_judge_async(digest),
            name=f"judge_t{self._turn_index}",
        )
        self._judge_tasks.add(task)
        task.add_done_callback(self._judge_tasks.discard)
        return None

    async def _run_judge_async(self, digest: str) -> None:
        verdict, response = await run_judge(self.router, self.model, digest)
        self._record_verdict_telemetry(verdict, sync=False)
        if response is not None:
            await self._emit_ledger_model_event(self.model, response)
        await self._emit_ledger_judge_verdict(verdict, "turn_final_text")
        if should_inject_reminder(verdict):
            self._pending_verdicts.append(format_verdict_reminder(verdict))

    def _record_verdict_telemetry(self, verdict: "JudgeVerdict", *, sync: bool) -> None:
        """Best-effort log; never raises. Pass verdicts only land here, not
        in the agent's context — keeps the noise floor low."""
        logger.info(
            "judge.verdict turn=%d sync=%s verdict=%s confidence=%.2f reason=%r error=%r",
            self._turn_index, sync, verdict.verdict,
            float(getattr(verdict, "confidence", 0.0) or 0.0),
            verdict.reason[:160], verdict.error or "",
        )

    # ------------------------------------------------------------------
    # Issue #56: Auto-import skills from SKILL.md files
    # ------------------------------------------------------------------

    # =========================================================================
    # SECTION 6 — PLUGIN / SKILL LOADING
    # =========================================================================
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
                name, description, tags, pc_refs, model_tier = (
                    _parse_skill_frontmatter(raw)
                )
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

                existing = await self._memory.procedural.get(name)
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
                        model_tier=model_tier,
                    )
                    await self._memory.procedural.upsert(genome)
                    logger.debug("Auto-imported skill '%s' from %s", name, skill_md)

                # Issue #276: cache name → tier in a sync-readable snapshot
                # so ``_compute_skill_max_tier`` can resolve at LLM-call time
                # without an awaitable lookup.
                effective_tier = (
                    model_tier
                    if model_tier is not None
                    else (existing.model_tier if existing else None)
                )
                if effective_tier is not None:
                    self._skill_tier_snapshot[name] = effective_tier

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
            if self._memory is not None:
                entry = await self._memory.relational.get(rel_key, "approved")
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
                if self._memory is not None:
                    from loom.core.memory.relational import RelationalEntry
                    await self._memory.relational.upsert(RelationalEntry(
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

    # =========================================================================
    # SECTION 7 — HISTORY SANITIZATION
    # =========================================================================
    def _apply_observation_masking(self) -> None:
        """Fold stale tool observations into scratchpad references (Issue #197 Phase 2).

        For each tool result message older than ``_mask_age_turns`` AND
        superseded by a more recent call of the same tool, this method:

        1. Writes the original content to scratchpad under a stable ref
        2. Replaces the inline content with a placeholder pointing at the
           ref, telling the agent it can read scratchpad if the data is
           still relevant

        Skipped:
          - Already-JIT-spilled entries (already minimal — re-folding adds
            no signal but costs a scratchpad ref)
          - The most recent call of any given tool (the agent likely needs
            this for continuity)
          - Already-masked entries (idempotent)
          - Untagged entries (e.g. from before this feature shipped, or
            when the session was loaded from disk without the metadata)

        JIT (Phase 1) is the data-persistence guarantee: large outputs are
        already in scratchpad. Masking is a token-budget optimization on
        top — it never causes data loss, only changes inline visibility.
        Scratchpad write failures gracefully degrade (keep inline).

        **Scratchpad ref prefix conventions** (canonical list lives in
        ``loom/platform/cli/tools.py:_categorize_scratchpad_refs``):

        - ``auto_<tool>_<id>``          → JIT spill (Phase 1)
        - ``masked_<tool>_<id>``        → this method (Phase 2)
        - ``subagent_failure:<id>``     → sub-agent failure trace (#192)

        Both the ``auto_`` and ``masked_`` prefixes are tool-output caches
        an agent can read back via ``scratchpad_read``. The
        ``scratchpad_read`` tool's listing groups refs by these prefixes so
        the agent can scan its own folded state.
        """
        if self._mask_age_turns <= 0:
            return

        # First pass: index the most-recent message slot for each tool name.
        latest_idx_per_tool: dict[str, int] = {}
        for i, msg in enumerate(self.messages):
            if msg.get("role") == "tool":
                tname = msg.get("_tool_name")
                if tname:
                    latest_idx_per_tool[tname] = i

        # Second pass: fold eligible older entries.
        for i, msg in enumerate(self.messages):
            if msg.get("role") != "tool":
                continue
            if msg.get("_masked"):
                continue
            emit_turn = msg.get("_emit_turn")
            if emit_turn is None:
                continue
            age = self._turn_index - emit_turn
            if age < self._mask_age_turns:
                continue

            tname = msg.get("_tool_name")
            if tname and latest_idx_per_tool.get(tname) == i:
                continue  # Most recent call — agent likely still using it.

            content = msg.get("content", "") or ""
            # JIT placeholder is already minimal — folding it again wastes
            # a scratchpad ref without meaningful token savings.
            if content.startswith("[tool output spilled to scratchpad"):
                continue

            ref = f"masked_{tname or 'tool'}_{uuid.uuid4().hex[:6]}"
            try:
                self._scratchpad.write(ref, content)
            except Exception as exc:
                logger.warning(
                    "Observation masking failed for %r turn %d: %s — keeping inline.",
                    tname, emit_turn, exc,
                )
                continue

            msg["content"] = (
                f"[observation folded — {tname or 'tool'} from {age} turns ago, "
                f"superseded by a more recent call]\n"
                f"  ref: scratchpad://{ref}\n"
                f"  Read with scratchpad_read(ref='{ref}') if you still need "
                f"the full output."
            )
            msg["_masked"] = True

        # Tool-result bodies shrank en masse; recount once at the end so
        # the footer % reflects the freed wire surface before the next
        # provider response lands. Block count is the dimension that
        # actually changed (text shrank but slot count is unchanged) —
        # token estimate also moves, so do a full record_messages.
        self.budget.record_messages(self.messages)

    def _sanitize_history(self) -> None:
        """Remove incomplete tool_use sequences and fix malformed tool args.

        Two passes:
        1. Fix any tool_calls whose ``arguments`` field is not valid JSON
           (can happen when MiniMax truncates a streaming response mid-JSON).
           Re-serialize from an empty dict so the API accepts the message.
        2. Trim any assistant message whose tool_calls are not all followed by
           matching tool result messages (orphaned tool_calls → 2013 error).

        PR-C4: when a repair actually happens, populate
        ``self._last_sanitize_repaired`` so the platform layer can surface
        a one-line "⚙ harness › sanitize: …" message. ``None`` means
        "ran but found nothing to fix" — silent.
        """
        msgs = self.messages
        _args_fixed = 0
        _msgs_before = len(msgs)

        # Pass 1: repair invalid arguments JSON in-place
        for msg in msgs:
            if msg.get("role") != "assistant":
                continue
            for tc in msg.get("tool_calls", []):
                fn = tc.get("function", {})
                raw_args = fn.get("arguments", "{}")
                if not isinstance(raw_args, str):
                    fn["arguments"] = json.dumps(raw_args, ensure_ascii=False)
                    _args_fixed += 1
                    continue
                try:
                    json.loads(raw_args)
                except (json.JSONDecodeError, ValueError):
                    fn["arguments"] = "{}"
                    _args_fixed += 1

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

        # Pass 4 (Issue #218): enforce tool_use ↔ tool_result adjacency.
        # Anthropic API requires every assistant tool_use to be immediately
        # followed by a user message containing the matching tool_result.
        # Out-of-order pairs (both exist but separated by other messages) trigger
        # 2013 even though Pass 2/3 are happy. Producer: a long-running tool
        # subprocess kept running past a user interrupt / abort and its late
        # result was appended after subsequent turns had already advanced.
        # Repair strategy: index every tool result by tool_call_id, then re-emit
        # canonical messages with each assistant's results pulled adjacent.
        result_by_id: dict[str, dict] = {}
        for msg in self.messages:
            if msg.get("role") == "tool":
                tid = msg.get("tool_call_id")
                if tid:
                    result_by_id[tid] = msg

        emitted_result_ids: set[str] = set()
        keep3: list[dict] = []
        for msg in self.messages:
            role = msg.get("role")
            if role == "tool":
                if msg.get("tool_call_id") in emitted_result_ids:
                    continue  # already emitted adjacent to its assistant
                # Result reached here without being emitted means its assistant
                # has no tool_call referencing it — Pass 3 should have caught
                # this, drop defensively.
                continue
            if role == "assistant" and msg.get("tool_calls"):
                keep3.append(msg)
                for tc in msg["tool_calls"]:
                    tid = tc.get("id")
                    if tid and tid in result_by_id and tid not in emitted_result_ids:
                        keep3.append(result_by_id[tid])
                        emitted_result_ids.add(tid)
                continue
            keep3.append(msg)
        self.messages = keep3

        # PR-C4: surface a one-line harness note when sanitize actually
        # repaired anything. Two signals:
        #   _args_fixed  — Pass 1 truncated/non-string arg blobs rebuilt
        #   _msgs_dropped — Pass 2/3/4 dropped orphaned tool_use or
        #                   tool_result messages
        # When nothing changed, stays silent (the design point: only
        # speak when something actually moved).
        _msgs_dropped = max(0, _msgs_before - len(self.messages))
        if _args_fixed or _msgs_dropped:
            cb = getattr(self, "_on_sanitize_repaired", None)
            if cb is not None:
                try:
                    cb(_args_fixed, _msgs_dropped)
                except Exception:
                    pass
            # Resync the budget — messages were rewritten, so the
            # block_count from the last API call no longer matches the
            # current history. Without this the footer % can stay
            # stale-high after a sanitize-only turn.
            self.budget.record_messages(self.messages)

    # =========================================================================
    # SECTION 8 — TIER / TELEMETRY
    # =========================================================================
    def _telemetry_record_tool(
        self,
        tool_name: str,
        *,
        result: "ToolResult",
        duration_ms: float,
    ) -> None:
        """Hot-path tool_call recorder. Kept as a small helper so the parallel
        and sequential dispatch loops both go through the same code path.
        """
        if self._telemetry is None:
            return
        dim = self._telemetry.get("tool_call")
        if dim is None:
            return
        dim.record(
            tool_name,
            success=result.success,
            duration_ms=duration_ms,
            error_msg=result.error if not result.success else None,
        )
        self._telemetry.mark_dirty()

    def _active_tier(self) -> int:
        """Effective tier this turn — sticky override or default."""
        return self._sticky_tier if self._sticky_tier is not None else self._default_tier

    def _active_model(self) -> str:
        """Resolve the model name for the active tier.

        A manual ``/model`` selection wins until the user or agent explicitly
        changes tier again. Otherwise, falls back to ``self._model`` when the
        tier system isn't configured for this tier.
        """
        if getattr(self, "_manual_model_override", False):
            return self._model
        tier = self._active_tier()
        return self._tier_models.get(tier, self._model)

    def _set_sticky_tier(
        self, new_tier: int | None, *, reason: str, source: str,
    ) -> "TierChanged | None":
        """Update sticky tier + reset counters. Returns a TierChanged event
        when the active tier actually moved, else None.

        ``source`` is one of ``"skill"`` / ``"agent"`` / ``"user"`` / ``"clear"``
        and lands in the event for telemetry / graphify analysis.
        """
        old_tier = self._active_tier()
        # Normalize: setting sticky to default_tier is equivalent to clearing,
        # so the "no override" state remains canonical.
        if new_tier == self._default_tier:
            new_tier = None
        if new_tier == self._sticky_tier:
            return None  # no-op
        self._manual_model_override = False
        self._sticky_tier = new_tier
        self._turns_at_current_tier = 0
        self._tier_reminder_emitted = False
        active = self._active_tier()
        if active == old_tier:
            return None
        # Issue #279: update runtime_identity on tier switch.
        # Use getattr — _set_sticky_tier is unit-tested with a SimpleNamespace
        # self that does not always carry a _telemetry attribute.
        _telemetry = getattr(self, "_telemetry", None)
        if _telemetry is not None:
            _ri = _telemetry.get("runtime_identity")
            if _ri is not None:
                _ri.update(model=self._tier_models.get(active, self._model),
                           tier=active)
        return TierChanged(
            from_tier=old_tier,
            to_tier=active,
            from_model=self._tier_models.get(old_tier, self._model),
            to_model=self._tier_models.get(active, self._model),
            source=source,
            reason=reason,
        )

    def _tick_tier_counter(self) -> "TierExpiryHint | None":
        """Increment the per-turn counter for the active tier and return a
        ``TierExpiryHint`` event the first time the threshold is crossed
        within a sticky session.

        Called once per ``stream_turn`` completion (immediately before
        ``TurnDone``). Skips emission when:
          - We're on the default tier (no sticky to expire)
          - Threshold isn't configured (``<= 0``)
          - The hint has already been emitted this sticky session
        """
        self._turns_at_current_tier += 1
        if self._sticky_tier is None:
            return None
        if self._tier_reminder_after_turns <= 0:
            return None
        if self._tier_reminder_emitted:
            return None
        if self._turns_at_current_tier < self._tier_reminder_after_turns:
            return None
        self._tier_reminder_emitted = True
        return TierExpiryHint(
            tier=self._sticky_tier,
            model=self._active_model(),
            turns_used=self._turns_at_current_tier,
            threshold=self._tier_reminder_after_turns,
        )

    def _compute_skill_max_tier(self) -> int:
        """Take max ``model_tier`` across currently-activated skills.

        Skills with no declared tier contribute 0, never escalating.
        """
        tracker = getattr(self, "_skill_outcome_tracker", None)
        if tracker is None:
            return 0
        try:
            names = tracker.activated_skills
        except Exception:
            return 0
        if not names:
            return 0
        max_tier = 0
        for name in names:
            try:
                # Cached lookup; ProceduralMemory.get is async, so we use the
                # in-memory snapshot if available. The snapshot is populated
                # at session start by _refresh_memory_index.
                snapshot = getattr(self, "_skill_tier_snapshot", {})
                tier = snapshot.get(name, 0)
            except Exception:
                tier = 0
            if tier > max_tier:
                max_tier = tier
        return max_tier

    def _should_continue_reasoning(self, stop_reason: str, tool_count: int) -> bool:
        """Decide whether to inject a continuation reminder for #271.

        Returns True iff all of:
          - reasoning_continuation mode is not "off"
          - stop_reason is "max_tokens" (the high-reasoning truncation case)
          - 0 tools fired this round (recovery via system-reminder; if tools
            ran the path is more ambiguous and likely user-input territory)
          - retry budget not yet exhausted

        Extracted as a pure predicate so the truth table is unit-testable
        without spinning up a full stream_turn pipeline.
        """
        return (
            self._reasoning_continuation_mode != "off"
            and stop_reason == "max_tokens"
            and tool_count == 0
            and self._consecutive_max_tokens < _MAX_REASONING_CONTINUATIONS
        )

    # =========================================================================
    # SECTION 9 — DISPATCH / TRACE / EVENTS
    # =========================================================================
    async def _log_message(
        self, role: str, content: str, metadata: dict | None = None,
        raw_json: str | None = None, turn_index: int | None = None,
    ) -> None:
        """Fire-and-forget session_log write. Exceptions are swallowed inside log_message.

        Issue #218: ``turn_index`` is captured eagerly at the call site and
        passed through, NOT read lazily from ``self._turn_index`` here.
        Callers schedule this via ``asyncio.ensure_future`` so the body runs
        an unknown number of event-loop ticks later — by then stream_turn
        may have advanced ``_turn_index``, which would persist this row with
        the wrong turn and reorder it on reload (root cause of #218 wire
        2013 errors). When ``turn_index`` is omitted we fall back to the
        live value for backwards compatibility.
        """
        if self._session_log is None:
            return
        ti = turn_index if turn_index is not None else self._turn_index
        await self._session_log.log_message(
            self.session_id, ti, role, content, metadata or {},
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

        # Step 5 cutover: capture per-call metadata that the ledger does
        # not store (full args / auth_decision / auth_selector). The
        # projector joins these with ledger tool_lifecycle events to
        # build ExecutionEnvelopeView. (#337) The previous
        # ``ExecutionEnvelope.records`` append on this path is gone —
        # mid-flight state coarsens to "executing" via projector
        # derivation, no records list needed.
        ctx = call.metadata.get(LIFECYCLE_CTX_KEY)
        if ctx is not None:
            self._register_call_meta(ctx.record)

        return result

    # ------------------------------------------------------------------
    # AgentLedger turn-boundary emit helpers (Quest B Step 2 commit 6)
    # ------------------------------------------------------------------

    def _prompt_stack_snapshot(self) -> tuple[str, dict]:
        """Return (sha256-prefixed hash of composed prompt, components dict).

        v1 components carry persona name + tool-catalog size; doc/53 §3.4
        also lists ``memory_layers`` and ``context_token_count`` but Loom
        does not yet track those — leaving them out is preferable to
        emitting placeholder values that would mislead future readers.
        """
        import hashlib

        try:
            composed = self._stack.composed_prompt or ""
        except Exception:
            composed = ""
        digest = "sha256:" + hashlib.sha256(composed.encode("utf-8")).hexdigest()
        try:
            persona = self._stack.current_personality
        except Exception:
            persona = None
        try:
            tool_count = len(self.registry.list())
        except Exception:
            tool_count = 0
        return digest, {
            "persona": persona,
            "tool_catalog_size": tool_count,
        }

    async def _emit_ledger_turn_start(self, turn_id: str) -> None:
        if self._ledger_emitter is None:
            return
        digest, components = self._prompt_stack_snapshot()
        await self._ledger_emitter.emit_turn_start(
            turn_id=turn_id,
            payload=TurnStartPayload(
                prompt_stack_hash=digest,
                prompt_stack_components=components,
            ),
            event_id=f"evt_turn_start_{turn_id[5:]}",
        )

    async def _emit_ledger_turn_end(
        self, turn_id: str, outcome: str, duration_ms: int
    ) -> None:
        if self._ledger_emitter is None:
            return
        await self._ledger_emitter.emit_turn_end(
            turn_id=turn_id,
            payload=TurnEndPayload(
                outcome=outcome,
                duration_ms=duration_ms,
                token_usage=dict(self._turn_token_usage),
            ),
            event_id=f"evt_turn_end_{turn_id[5:]}",
            parent_event_id=f"evt_turn_start_{turn_id[5:]}",
        )

    async def _emit_ledger_model_event(
        self, model: str, response: Any
    ) -> None:
        """Emit ``model_event`` for one router call and aggregate the
        token usage into ``self._turn_token_usage`` so ``turn_end``
        carries the per-turn rollup (doc/53 §3.1 / §11.2.1)."""
        if self._ledger_emitter is None:
            return
        usage = {
            "input_tokens": int(getattr(response, "input_tokens", 0) or 0),
            "output_tokens": int(getattr(response, "output_tokens", 0) or 0),
        }
        cache_read = int(getattr(response, "cache_read_input_tokens", 0) or 0)
        cache_creation = int(
            getattr(response, "cache_creation_input_tokens", 0) or 0
        )
        if cache_read:
            usage["cache_read_input_tokens"] = cache_read
        if cache_creation:
            usage["cache_creation_input_tokens"] = cache_creation
        for k, v in usage.items():
            self._turn_token_usage[k] = self._turn_token_usage.get(k, 0) + v
        try:
            await self._ledger_emitter.emit_model_event(
                payload=ModelEventPayload(
                    model=model,
                    tier=self._active_tier(),
                    token_usage=usage,
                ),
            )
        except Exception:
            logger.exception("ledger model_event emit failed; continuing")

    async def _emit_ledger_judge_verdict(
        self, verdict: "JudgeVerdict", judged_subject: str
    ) -> None:
        """Emit ``judge_verdict`` (doc/53 §3.1) after ``run_judge`` returns.
        Verdict-driven thought capture: FAIL/UNCERTAIN flips the per-turn
        signal so the reasoning trail gets persisted at turn_end."""
        if verdict.verdict in ("fail", "uncertain"):
            self._turn_judge_capture_signal = True
        if self._ledger_emitter is None:
            return
        # Map judge verdict → schema literal. Judge produces lowercase
        # pass/fail/uncertain; ledger uses uppercase plus an explicit
        # ERROR for transport failures.
        if verdict.error:
            mapped = "ERROR"
        else:
            mapped = {
                "pass": "PASS",
                "fail": "FAIL",
                "uncertain": "CONCERN",
            }.get(verdict.verdict, "CONCERN")
        try:
            await self._ledger_emitter.emit_judge_verdict(
                payload=JudgeVerdictPayload(
                    verdict=mapped,
                    confidence=float(getattr(verdict, "confidence", 0.0) or 0.0),
                    reason=(verdict.reason or "")[:500],
                    judged_subject=judged_subject,
                ),
            )
        except Exception:
            logger.exception("ledger judge_verdict emit failed; continuing")

    async def _commit_or_discard_thought(
        self, turn_id: str, outcome: str
    ) -> None:
        """doc/53 §3.3 — commit accumulated reasoning text iff the turn
        produced a capture signal (judge fail/uncertain, abandoned/error
        outcome, or a >10KB artifact). Drop otherwise to keep the ledger
        slim. Inline up to 50KB; spill to blob storage above that."""
        if self._ledger_emitter is None or not self._turn_thought_text:
            return
        signal = (
            self._turn_judge_capture_signal
            or outcome in ("abandoned", "error")
            or self._turn_artifact_max_size > 10_000
        )
        if not signal:
            return
        store = self._ledger_store
        if store is None:
            return
        eid = f"evt_thought_{turn_id[5:]}"
        duration_ms = (
            int((time.monotonic() - self._turn_thought_started) * 1000)
            if self._turn_thought_started is not None
            else 0
        )
        try:
            payload = await store.build_thought_payload(
                self._turn_thought_text,
                turn_id=turn_id,
                event_id=eid,
                duration_ms=duration_ms,
                produced_tool_calls=self._turn_tool_calls_in_turn,
            )
            await self._ledger_emitter.emit_thought(
                turn_id=turn_id, payload=payload, event_id=eid,
            )
        except Exception:
            logger.exception("ledger thought emit failed; continuing")

    # ------------------------------------------------------------------
    # #336 — memory compaction subscriber (doc/53 §6.4 push pattern)
    # ------------------------------------------------------------------

    async def _compact_memory(self, *, force: bool = False) -> None:
        """Run one episodic-to-semantic compaction pass.

        The caller owns ``_compaction_lock``. ``force=True`` skips the
        configured threshold gate but still avoids a no-op LLM call when
        there are no uncompressed episodic rows.
        """
        ep_count = await self._memory.episodic.count_session(
            self.session_id, uncompressed_only=True,
        )
        if ep_count <= 0:
            return
        if not force and ep_count < self._episodic_compress_threshold:
            return
        fact_count = await compress_session(
            self.session_id,
            self._memory.episodic, self._memory.semantic,
            self.router, self.model,
            governor=self._governor,
            telemetry=self._telemetry,
        )
        if fact_count:
            self._pending_compactions.append(
                CompressDone(fact_count=fact_count)
            )
        # Rebuild MemoryIndex so long-running sessions (Discord)
        # see updated fact/anti-pattern counts without restarting.
        await self._refresh_memory_index()

    async def _run_compaction_check(self) -> None:
        """One pass of the threshold-driven compaction logic.

        Extracted from the prior inline trigger in ``stream_turn``.
        Behaviour is unchanged: count uncompressed episodic rows, run
        ``compress_session`` if past threshold, refresh the memory
        index. The CompressDone signal is appended to
        ``_pending_compactions`` so the next ``stream_turn`` yields
        it for Discord/CLI consumers; if no next turn fires, the
        signal is lost — same as before #336 for late-turn triggers.

        Race contract: a new turn writing fresh episodic entries
        during this call is safe — ``compress_session`` reads a
        snapshot up front, marks only that snapshot at the end.
        Concurrent compaction runs are blocked by ``_compaction_lock``.
        """
        try:
            async with self._compaction_lock:
                await self._compact_memory(force=False)
        except Exception:
            logger.exception(
                "memory compaction subscriber: compress_session failed; "
                "continuing"
            )

    async def force_compact(self) -> None:
        """Force one compaction pass, bypassing the episodic count threshold.

        Used by long-running platform frontends as a safety net for idle
        sessions. The implementation intentionally shares the same lock,
        compression path, pending ``CompressDone`` buffer, and memory-index
        refresh as the turn-driven subscriber.
        """
        try:
            async with self._compaction_lock:
                await self._compact_memory(force=True)
        except Exception:
            logger.exception(
                "memory force_compact: compress_session failed; continuing"
            )

    async def _compaction_subscriber_loop(self) -> None:
        """Background task: subscribe to ``turn_end`` events for this
        session and trigger compaction. Runs for the lifetime of the
        session; cancelled by ``stop()`` alongside other background
        tasks.

        Subscribing rather than polling means compaction is decoupled
        from stream_turn's hot path: the turn returns immediately
        after ``turn_end`` is emitted, and the LLM compaction call
        happens off the user-visible timeline.
        """
        if self._ledger_store is None:
            return
        try:
            async with self._ledger_store.subscribe(
                event_types=["turn_end"],
                session_id=self.session_id,
            ) as subscriber:
                async for _event in subscriber:
                    # Serialise concurrent runs. If a turn_end fires
                    # while the previous compaction is still mid-LLM,
                    # the second attempt waits rather than running in
                    # parallel and double-writing semantic facts.
                    await self._run_compaction_check()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "memory compaction subscriber loop crashed; will not restart"
            )

    async def _on_trace(self, call: ToolCall, result: ToolResult) -> None:
        # #334 / doc/53 §3.3 — feed the per-turn artifact size accumulator
        # so the "artifact > 10 KB" thought capture signal actually fires.
        # The middleware-side ``extract_artifact_info`` is the single source
        # of truth for which tools produce artifacts; we reuse it here.
        from loom.core.harness.middleware import extract_artifact_info
        info = extract_artifact_info(call, result)
        if info is not None:
            size = int(info.get("size_bytes", 0) or 0)
            if size > self._turn_artifact_max_size:
                self._turn_artifact_max_size = size

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
        await self._memory.episodic.write(
            EpisodicEntry(
                session_id=self.session_id,
                event_type="tool_result",
                content=summary,
                metadata=meta,
            )
        )

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
            # #337 — single canonical envelope id; matches what the
            # projector emits so the action_records FK lines up with
            # the ExecutionEnvelopeView envelope_id surfaced to UI.
            envelope_id = (
                f"e{self._envelope_counter}" if self._envelope_counter else ""
            )
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

        #337 — register the call's metadata on first state change so the
        ledger projector can render args / auth fields even before the
        call ends. Mid-flight state itself is intentionally coarsened to
        ``executing`` (sub-states like ``awaiting_confirm`` no longer
        surface in EnvelopeView; they still drive the inline confirm
        widget via the ActionStateChange stream below).
        """
        self._register_call_meta(record)

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

    def _register_call_meta(self, record) -> None:
        """Capture per-call args / auth bits for the envelope projector.

        Called from ``_on_state_change`` (early-add path #109) and the
        post-pipeline link site so the projector sees consistent
        metadata regardless of which hook fires first. Also appends
        ``call.id`` to ``_envelope_call_ids`` if not already present —
        this is how the projector knows which calls belong to the
        current batch (replaces the pre-Step-5
        ``ExecutionEnvelope.records[]`` source of truth).
        """
        call = record.call
        if call is None:
            return
        if call.id not in self._envelope_call_ids:
            self._envelope_call_ids.append(call.id)

        # Args preview — first string argument truncated to 60 chars,
        # newlines collapsed for the UI.
        args_preview = ""
        if call.args:
            first_val = next(iter(call.args.values()), "")
            if isinstance(first_val, str):
                args_preview = first_val.replace("\n", "↵")[:60]

        # auth_decision / auth_selector come from BlastRadiusMiddleware
        # writes to call.metadata.
        auth_decision = call.metadata.get("confirm_decision", "")
        auth_selector = ""
        scope_req = call.metadata.get("scope_request")
        if scope_req is not None:
            reqs = getattr(scope_req, "requirements", [])
            if reqs:
                auth_selector = reqs[0].selector

        self._call_metadata[call.id] = CallMeta(
            call_id=call.id,
            tool_name=record.tool_name,
            full_args=dict(call.args) if call.args else {},
            args_preview=args_preview,
            auth_decision=auth_decision,
            auth_selector=auth_selector,
        )

    def _auth_expires_for(self, call_id: str) -> float:
        """Live lookup of scope-grant expiry for a tool call.

        Reads ``self.perm._effective_grants()`` at view-build time —
        this is session-level state, not per-call ledger data.
        """
        meta = self._call_metadata.get(call_id)
        if meta is None or meta.auth_decision != "scope":
            return 0.0
        for g in self.perm._effective_grants():
            if hasattr(g, "source") and g.source == "lease" and g.valid_until > 0:
                return g.valid_until
        return 0.0

    async def _build_envelope_view(
        self, batch_t0: float = 0.0
    ) -> ExecutionEnvelopeView:
        """Build a read-only ``ExecutionEnvelopeView`` from the ledger.

        #337 — pure projector path. When no ledger is wired (tests,
        ``[ledger].enabled=false``), returns an empty stub so callers
        still get a valid object; mid-batch state granularity is the
        only feature lost in that mode.

        This is the projection layer described in doc/43 — it only
        does view shaping, never mutates middleware state.
        """
        if self._envelope_projector is not None:
            from loom.core.ledger import current_turn_id

            turn_id = current_turn_id() or "system"
            envelope_id = f"e{self._envelope_counter}"
            metadata = self._envelope_metadata.get(envelope_id, {})
            return await self._envelope_projector.build_view(
                envelope_id=envelope_id,
                session_id=self.session_id,
                turn_id=turn_id,
                turn_index=self._turn_index,
                call_ids=list(self._envelope_call_ids),
                call_meta=self._call_metadata,
                auth_expires_lookup=self._auth_expires_for,
                batch_t0=batch_t0,
                intent=metadata.get("intent", ""),
                outcome_summary=metadata.get("outcome_summary", ""),
                parallel_reason=metadata.get("parallel_reason", ""),
            )

        # No ledger wired → return an empty stub. The shape stays the
        # same so EnvelopeStarted / EnvelopeCompleted consumers don't
        # need a separate code path. ``outcome=""`` is explicit (and
        # not just relying on the dataclass default) so the contract
        # is visible at the call site: empty means unknown, never
        # fulfilled — UI surfaces infer UNFULFILLED from
        # ``status=="failed"`` when no producer wrote anything (#421).
        return ExecutionEnvelopeView(
            envelope_id=f"e{self._envelope_counter}",
            session_id=self.session_id,
            turn_index=self._turn_index,
            status="running",
            node_count=0,
            parallel_groups=0,
            intent=self._envelope_metadata.get(
                f"e{self._envelope_counter}", {}
            ).get("intent", ""),
            outcome="",
        )

    async def _author_envelope_outcome_summary(
        self,
        view: ExecutionEnvelopeView,
        *,
        model: str,
    ) -> ExecutionEnvelopeView:
        """Fill one display-line outcome summary for non-fulfilled multi-node batches."""
        if view.node_count <= 1:
            return view
        if view.outcome_summary:
            return view
        if view.outcome in ("", "fulfilled", "aborted"):
            return view

        node_lines = []
        for node in view.nodes:
            detail = node.error_snippet or node.output_preview
            node_lines.append(
                {
                    "tool": node.tool_name,
                    "state": node.state,
                    "detail": detail[:120] if detail else "",
                }
            )

        messages = [
            {
                "role": "system",
                "content": (
                    "Write one Traditional Chinese UI outcome line for a tool "
                    "batch. Be factual, no markdown, no bullet, under 40 Chinese "
                    "characters. Summarize what did not finish or what needs "
                    "follow-up."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "intent": view.intent,
                        "outcome": view.outcome,
                        "nodes": node_lines,
                    },
                    ensure_ascii=False,
                ),
            },
        ]
        try:
            response = await self.router.chat(
                model=model,
                messages=messages,
                tools=None,
                max_tokens=80,
            )
        except Exception as exc:
            logger.debug("Envelope outcome summary skipped: %s", exc)
            return view

        summary = _clean_envelope_metadata_line(response.text or "", max_len=120)
        if summary:
            view.outcome_summary = summary
            self._envelope_metadata.setdefault(view.envelope_id, {})[
                "outcome_summary"
            ] = summary
        return view

    # =========================================================================
    # SECTION 10 — HITL / SCOPE / GRANTS
    # =========================================================================
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
                f"[#c8a464]{call.tool_name}[/#c8a464]  {call.trust_level.label}\n"
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
            f"[#c8a464]{call.tool_name}[/#c8a464]  {call.trust_level.label}",
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

    @staticmethod
    def _format_scope_summary_plain(call: ToolCall) -> str:
        """Plain-text scope summary for the LoomApp confirm widget.

        Mirrors :meth:`_format_scope_panel` but strips Rich markup so it
        renders cleanly inside a prompt_toolkit FormattedTextControl,
        which doesn't speak Rich's ``[bold]`` syntax.
        """
        from loom.core.harness.scope import (
            DiffReason, ScopeDiff, ScopeRequest,
        )

        scope_req: ScopeRequest | None = call.metadata.get("scope_request")
        diff: ScopeDiff | None = call.metadata.get("scope_diff")

        if scope_req is None:
            arg_lines: list[str] = []
            for k, v in call.args.items():
                if isinstance(v, str):
                    display = v if len(v) <= 120 else v[:40] + "…" + v[-40:]
                    arg_lines.append(f"  {k}: {display}")
                else:
                    arg_lines.append(f"  {k}: {v!r}")
            return "\n".join(arg_lines) if arg_lines else "(no args)"

        _REASON_LABELS: dict[DiffReason, str] = {
            DiffReason.FIRST_TIME: "First time accessing this resource",
            DiffReason.SELECTOR_EXPANSION: "Expanding beyond previously approved scope",
            DiffReason.CONSTRAINT_EXPANSION: "Exceeding previously approved limits",
            DiffReason.RESOURCE_TYPE_NEW: "New resource type not previously authorized",
        }

        lines: list[str] = []
        for req in scope_req.requirements:
            line = f"  {req.resource}:{req.action} → {req.selector}"
            if req.constraints.get("scope_unknown"):
                line += "  ⚠ scope could not be fully resolved"
            lines.append(line)
        if diff is not None and not diff.is_fully_covered:
            reason_label = _REASON_LABELS.get(diff.reason, str(diff.reason.value))
            lines.append(reason_label)
            if diff.covered:
                lines.append(
                    "  ✓ covered: " + ", ".join(r.selector for r in diff.covered)
                )
            if diff.missing:
                lines.append(
                    "  ● new: " + ", ".join(r.selector for r in diff.missing)
                )
        return "\n".join(lines)

    async def _confirm_tool_cli(self, call: ToolCall) -> "ConfirmDecision":
        """
        CLI-specific confirmation prompt — arrow-key inline widget.

        Phase C (Issue #45): scope metadata → verdict + diff info.
        Phase B (Issue #88): returns ConfirmDecision (y/s/a/N) instead of bool.
        PR-A3 (#236): replaces single-key stdin prompt with arrow-key
        :func:`select_prompt`. Single-letter shortcuts (y/s/a/N) preserved
        for muscle memory.

        Returns
        -------
        ConfirmDecision
            DENY  — Esc, Ctrl+C, or "N" shortcut
            ONCE  — "y" shortcut (approve this call only)
            SCOPE — "s" shortcut (30-min session lease)
            AUTO  — "a" shortcut (permanent grant for this scope)
        """
        from loom.core.harness.scope import ConfirmDecision, PermissionVerdict
        from loom.platform.cli.ui import SelectOption, select_prompt

        # Stop any running spinner so it doesn't overwrite the widget area.
        import sys
        if self._cancel_spinner_fn is not None:
            self._cancel_spinner_fn()
            sys.stdout.write("\r\033[K")
            sys.stdout.flush()

        verdict = call.metadata.get("scope_verdict")

        widget_title = (
            f"Loom 想執行 [{call.tool_name}]  ·  信任度 {call.trust_level.plain}"
        )
        if verdict == PermissionVerdict.EXPAND_SCOPE:
            widget_title = "⚠ " + widget_title

        # PR-D1: when running under the persistent LoomApp, route through
        # its mode-flag widget so confirm renders inside the layout (not
        # in scrollback) and disappears completely after decision —
        # satisfies the "用過即焚" requirement #236 added. Scope summary
        # is folded into the widget body so we don't print a separate
        # Rich Panel that would leak into scrollback.
        loom_app = getattr(self, "_loom_app", None)
        if loom_app is not None:
            return await loom_app.request_confirm(
                title=widget_title,
                body=self._format_scope_summary_plain(call),
                options=[
                    ("允許這次",                        ConfirmDecision.ONCE,   "y"),
                    ("允許並記住 30 分鐘 (lease)",      ConfirmDecision.SCOPE,  "s"),
                    ("允許並永久授權此 scope",          ConfirmDecision.AUTO,   "a"),
                    ("拒絕",                            ConfirmDecision.DENY,   "n"),
                ],
                default_index=3,  # cursor on DENY for safety
                cancel_value=ConfirmDecision.DENY,
            )

        # Fallback for tests / scripts without a running LoomApp:
        # render the legacy Rich Panel (which leaves scrollback residue,
        # acceptable in non-interactive contexts) and use the standalone
        # select_prompt widget.
        console.print()
        if verdict == PermissionVerdict.EXPAND_SCOPE:
            title = "[loom.error]⚠ Scope expansion required[/loom.error]"
            border_style = "loom.error"
        else:
            title = "[loom.warning]  Tool requires confirmation[/loom.warning]"
            border_style = "loom.warning"
        console.print(
            Panel(
                self._format_scope_panel(call),
                title=title,
                border_style=border_style,
            )
        )
        options = [
            SelectOption(label="允許這次",                   value=ConfirmDecision.ONCE,  shortcut="y"),
            SelectOption(label="允許並記住 30 分鐘 (lease)", value=ConfirmDecision.SCOPE, shortcut="s"),
            SelectOption(label="允許並永久授權此 scope",     value=ConfirmDecision.AUTO,  shortcut="a"),
            SelectOption(label="拒絕",                       value=ConfirmDecision.DENY,  shortcut="n"),
        ]

        async def _run():
            return await select_prompt(
                title=widget_title,
                options=options,
                default_index=3,
                cancel_value=ConfirmDecision.DENY,
            )

        runner = getattr(self, "_run_interactive", None)
        if runner is not None:
            return await runner(_run)
        return await _run()

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

    # =========================================================================
    # SECTION 11 — PARALLEL DISPATCH
    # =========================================================================
    async def _dispatch_parallel(self, tool_uses: list) -> list[tuple]:
        """
        Dispatch multiple independent tool calls concurrently via asyncio.gather.

        Results are returned in the original tool_uses order. Any exception
        raised by a single tool is converted into an error ToolResult rather
        than surfaced to the caller — one tool failing must not cancel the
        rest of the batch.
        """
        async def _run(tu) -> tuple:
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
            return tu, result, duration_ms

        try:
            return list(await asyncio.gather(*[_run(tu) for tu in tool_uses]))
        except Exception as exc:
            # asyncio.gather itself should not raise here (each _run catches its
            # own exceptions); this is a belt-and-braces fallback mirroring the
            # prior TaskScheduler wrapper (Issue #94 Gap 2).
            logger.error(
                "_dispatch_parallel: unexpected gather failure: %s", exc, exc_info=True,
            )
            return [
                (
                    tu,
                    ToolResult(
                        call_id=tu.id,
                        tool_name=tu.name,
                        success=False,
                        error=f"Internal dispatch error: {exc}",
                        failure_type="execution_error",
                    ),
                    0.0,
                )
                for tu in tool_uses
            ]

    # =========================================================================
    # SECTION 12 — MEMORY INDEX / COMPACT
    # =========================================================================
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
        if self._memory is None:
            return
        try:
            indexer = MemoryIndexer(
                self._memory.semantic, self._memory.procedural,
                self._memory.episodic, self._memory.relational,
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
            # System prompt size changed — keep budget aligned so the
            # footer % doesn't drift away from reality until the next
            # provider response arrives.
            self.budget.record_messages(self.messages)
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
    # =========================================================================
    # SECTION 13 — ACCESSORS
    # =========================================================================
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
        if not ok and model.startswith("codex/"):
            self.router = build_router(active_model=model)
            ok = self.router.switch_model(model)
        if ok:
            self._model = model
            self._manual_model_override = True
        return ok


__all__ = [
    "LoomSession",
    "build_router",
    "compress_session",
    "_load_loom_config",
    "_load_env",
    "_parse_skill_frontmatter",
    "_resolve_output_max_tokens",
    "COMPRESS_PROMPT",
    "COMPACT_PROMPT",
]
