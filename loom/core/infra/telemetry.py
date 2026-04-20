"""
AgentTelemetryTracker — self-observable agent behavior (Issue #142).

Mirrors ``MemoryHealthTracker`` (#133): in-memory counters on the hot path,
persistent JSON snapshots in the ``agent_telemetry`` table. The agent reads
its own state through the ``agent_health`` tool; anomalies are pushed into
context only when a dimension reports one, so noise stays proportional to
signal.

v1 dimensions
-------------
- ``tool_call``         — success/failure/latency aggregated per tool
- ``context_layout``    — token share across SOUL / Agent / messages layers,
                          reconstructed from the last real ``input_tokens``
                          (authoritative total) + char-weighted attribution
                          per layer
- ``memory_compression`` — episodic entries compressed vs facts actually
                           extracted; the fact-yield ratio is the proxy for
                           whether ``compress_session`` is losing content
                           to the LLM extractor or the admission gate

Persistence layout
------------------
Single ``agent_telemetry`` table, one row per (dimension, session_id). Each
row stores a JSON payload produced by the dimension's ``snapshot()``. Cross-
session queries use ``json_extract``; the schema stays flexible even as
dimensions grow.
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, UTC
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    import aiosqlite
    from loom.core.cognition.prompt_stack import PromptStack

logger = logging.getLogger(__name__)


# ── Schema ────────────────────────────────────────────────────────────────

TELEMETRY_TABLE_DDL = """\
CREATE TABLE IF NOT EXISTS agent_telemetry (
    dimension   TEXT NOT NULL,
    session_id  TEXT NOT NULL,
    payload     TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (dimension, session_id)
);
"""

TELEMETRY_INDEX_DDL = """\
CREATE INDEX IF NOT EXISTS idx_agent_telemetry_updated
ON agent_telemetry(updated_at DESC);
"""


# ── Base dimension ────────────────────────────────────────────────────────

class DimensionTracker(ABC):
    """A single observability dimension. Subclasses are hot-path safe
    (no I/O) and must implement snapshot/render/anomaly.
    """

    #: Persisted dimension identifier; must be unique.
    name: str = ""

    @abstractmethod
    def snapshot(self) -> dict[str, Any]:
        """Return a JSON-serialisable view of current state."""

    @abstractmethod
    def render_summary(self) -> str:
        """One-line status suitable for a minimal report."""

    @abstractmethod
    def render_detail(self) -> str:
        """Multi-line breakdown for drill-down queries."""

    def has_anomaly(self) -> bool:
        """Return True when the agent should be nudged via system-reminder."""
        return False

    def describe_anomaly(self) -> str | None:
        """Short description of the current anomaly, or None. Consulted only
        when ``has_anomaly()`` is True.
        """
        return None

    def load_from(self, payload: dict[str, Any]) -> None:
        """Restore prior-session state from a persisted snapshot. Default:
        no-op (dimensions that don't need cross-session continuity can skip).
        """
        return None


# ── Dimension: tool_call ──────────────────────────────────────────────────

@dataclass
class ToolCallStats:
    tool_name: str
    success_count: int = 0
    failure_count: int = 0
    total_latency_ms: float = 0.0
    last_failure_msg: str | None = None
    last_failure_at: str | None = None

    @property
    def total(self) -> int:
        return self.success_count + self.failure_count

    @property
    def failure_rate(self) -> float:
        return self.failure_count / self.total if self.total > 0 else 0.0

    @property
    def avg_latency_ms(self) -> float:
        return self.total_latency_ms / self.total if self.total > 0 else 0.0


class ToolCallDimension(DimensionTracker):
    name = "tool_call"

    #: Anomaly threshold — fires when overall failure rate exceeds this,
    #: provided we have enough samples to trust the ratio.
    FAILURE_RATE_THRESHOLD = 0.3
    MIN_SAMPLES_FOR_ANOMALY = 5

    def __init__(self) -> None:
        self._tools: dict[str, ToolCallStats] = {}

    def record(
        self,
        tool_name: str,
        *,
        success: bool,
        duration_ms: float,
        error_msg: str | None = None,
    ) -> None:
        stats = self._tools.setdefault(tool_name, ToolCallStats(tool_name))
        stats.total_latency_ms += duration_ms
        if success:
            stats.success_count += 1
        else:
            stats.failure_count += 1
            stats.last_failure_msg = (error_msg or "")[:300]
            stats.last_failure_at = datetime.now(UTC).isoformat()

    # ── Aggregates ────────────────────────────────────────────────
    def _totals(self) -> tuple[int, int, float]:
        total = sum(s.total for s in self._tools.values())
        fails = sum(s.failure_count for s in self._tools.values())
        avg_lat = (
            sum(s.total_latency_ms for s in self._tools.values()) / total
            if total else 0.0
        )
        return total, fails, avg_lat

    # ── DimensionTracker ──────────────────────────────────────────
    def snapshot(self) -> dict[str, Any]:
        return {
            "tools": {
                name: {
                    "success": s.success_count,
                    "failure": s.failure_count,
                    "avg_latency_ms": round(s.avg_latency_ms, 1),
                    "last_failure_at": s.last_failure_at,
                    "last_failure_msg": s.last_failure_msg,
                }
                for name, s in self._tools.items()
            }
        }

    def render_summary(self) -> str:
        total, fails, avg_lat = self._totals()
        if total == 0:
            return "tool: (no calls yet)"
        success_pct = 100 * (1 - fails / total)
        return f"tool:{success_pct:.0f}% | lat:{avg_lat:.0f}ms | n={total}"

    def render_detail(self) -> str:
        if not self._tools:
            return "tool_call: no activity recorded."
        lines = ["## tool_call"]
        for name, s in sorted(self._tools.items()):
            icon = "OK" if s.failure_count == 0 else (
                "DEGRADED" if s.failure_rate < 0.1 else "FAILING"
            )
            lines.append(
                f"- **{name}** [{icon}] "
                f"{s.success_count}/{s.total} ok, "
                f"{s.avg_latency_ms:.0f}ms avg"
            )
            if s.last_failure_msg:
                lines.append(f"  last err: {s.last_failure_msg[:120]}")
        return "\n".join(lines)

    def has_anomaly(self) -> bool:
        total, fails, _ = self._totals()
        return (
            total >= self.MIN_SAMPLES_FOR_ANOMALY
            and fails / total > self.FAILURE_RATE_THRESHOLD
        )

    def describe_anomaly(self) -> str | None:
        total, fails, _ = self._totals()
        if total < self.MIN_SAMPLES_FOR_ANOMALY:
            return None
        rate = fails / total
        if rate <= self.FAILURE_RATE_THRESHOLD:
            return None
        worst = max(
            (s for s in self._tools.values() if s.failure_count > 0),
            key=lambda s: s.failure_rate,
            default=None,
        )
        worst_str = f" worst: {worst.tool_name} ({worst.failure_count}/{worst.total})" if worst else ""
        return f"tool_call failure rate {rate:.0%} over {total} calls.{worst_str}"


# ── Dimension: memory_compression ─────────────────────────────────────────

class MemoryCompressionDimension(DimensionTracker):
    """Observes compress_session runs: how many entries went in, how many
    facts came out, whether the yield ratio is healthy.

    Backed by the soft-delete landed in #158 — when yield is low, operators
    know the raw episodic trace is still on disk and can be re-mined.

    Tool-event entries (``tool_call`` / ``tool_result``) are excluded from
    the yield denominator (#173): they record operational side-effects, not
    knowledge, so a low facts/entries ratio on a tool-heavy session is
    semantically correct and shouldn't fire an anomaly.
    """

    name = "memory_compression"

    #: Anomaly threshold — fires when rolling yield ratio drops below this.
    LOW_YIELD_THRESHOLD = 0.2
    MIN_RUNS_FOR_ANOMALY = 3

    def __init__(self) -> None:
        self._runs: int = 0
        self._entries_total: int = 0
        self._tool_events_total: int = 0
        self._facts_total: int = 0
        # Window of recent yield ratios for anomaly detection
        self._recent_yields: list[float] = []
        self._last_at: str | None = None
        # Runs that had no knowledge entries after filtering — tracked for
        # visibility but excluded from the anomaly window.
        self._skipped_runs: int = 0

    def record(self, *, entries: int, facts: int, tool_events: int = 0) -> None:
        self._runs += 1
        self._entries_total += entries
        self._tool_events_total += tool_events
        self._facts_total += facts
        knowledge_entries = entries - tool_events
        if knowledge_entries > 0:
            self._recent_yields.append(facts / knowledge_entries)
            # Keep the window bounded — rolling average over last 10 runs
            if len(self._recent_yields) > 10:
                self._recent_yields.pop(0)
        else:
            self._skipped_runs += 1
        self._last_at = datetime.now(UTC).isoformat()

    @property
    def knowledge_entries_total(self) -> int:
        return max(self._entries_total - self._tool_events_total, 0)

    @property
    def overall_yield(self) -> float:
        denom = self.knowledge_entries_total
        return self._facts_total / denom if denom > 0 else 0.0

    @property
    def recent_yield(self) -> float:
        if not self._recent_yields:
            return 0.0
        return sum(self._recent_yields) / len(self._recent_yields)

    # ── DimensionTracker ──────────────────────────────────────────
    def snapshot(self) -> dict[str, Any]:
        return {
            "runs": self._runs,
            "entries_total": self._entries_total,
            "tool_events_total": self._tool_events_total,
            "knowledge_entries_total": self.knowledge_entries_total,
            "facts_total": self._facts_total,
            "overall_yield": round(self.overall_yield, 3),
            "recent_yields": [round(y, 3) for y in self._recent_yields],
            "skipped_runs": self._skipped_runs,
            "last_at": self._last_at,
        }

    def render_summary(self) -> str:
        if self._runs == 0:
            return "compress: (not run)"
        return (
            f"compress:{self.overall_yield:.0%} yield "
            f"({self._facts_total}/{self.knowledge_entries_total}, n={self._runs})"
        )

    def render_detail(self) -> str:
        if self._runs == 0:
            return "memory_compression: no runs this session."
        lines = [
            "## memory_compression",
            f"- runs: {self._runs}",
            f"- entries processed: {self._entries_total}",
            f"- tool events filtered: {self._tool_events_total}",
            f"- knowledge entries: {self.knowledge_entries_total}",
            f"- facts extracted: {self._facts_total}",
            f"- overall yield: {self.overall_yield:.2%}",
            f"- recent yield (last {len(self._recent_yields)} runs): "
            f"{self.recent_yield:.2%}",
        ]
        if self._skipped_runs:
            lines.append(
                f"- runs skipped (tool-only, no knowledge entries): {self._skipped_runs}"
            )
        if self.has_anomaly():
            lines.append(
                "- ⚠ yield is low — LLM extractor may be missing content. "
                "Original entries are soft-deleted (#158) and remain on disk "
                "until TTL prune."
            )
        return "\n".join(lines)

    def has_anomaly(self) -> bool:
        return (
            len(self._recent_yields) >= self.MIN_RUNS_FOR_ANOMALY
            and self.recent_yield < self.LOW_YIELD_THRESHOLD
        )

    def describe_anomaly(self) -> str | None:
        if not self.has_anomaly():
            return None
        return (
            f"compression yield {self.recent_yield:.0%} over last "
            f"{len(self._recent_yields)} runs — facts are being lost in "
            f"extraction."
        )


# ── Dimension: context_layout ─────────────────────────────────────────────

class ContextLayoutDimension(DimensionTracker):
    """Token-share estimate across prompt layers.

    No tokenizer dependency: uses the authoritative ``input_tokens`` from
    the last LLM response and attributes it across layers by character
    count. The attribution is an approximation (±15% typical), but the
    total is real.
    """

    name = "context_layout"

    def __init__(
        self,
        *,
        stack: "PromptStack | None" = None,
        messages_ref: list | None = None,
        max_window: int = 200_000,
    ) -> None:
        self._stack = stack
        self._messages_ref = messages_ref
        self._max_window = max_window
        self._last_total_tokens: int = 0

    def update_total(self, input_tokens: int) -> None:
        """Called after each LLM response — stores the authoritative total."""
        if input_tokens > 0:
            self._last_total_tokens = input_tokens

    def _layer_chars(self) -> dict[str, int]:
        """Return {layer_name: char_count} for each prompt layer + history."""
        out: dict[str, int] = {}
        if self._stack is not None:
            for layer in getattr(self._stack, "_layers", []):
                out[layer.name] = len(layer.content)
        if self._messages_ref is not None:
            history_chars = 0
            for m in self._messages_ref:
                c = m.get("content") if isinstance(m, dict) else None
                if isinstance(c, str):
                    history_chars += len(c)
                elif isinstance(c, list):
                    # Provider-neutral tool-use blocks
                    for block in c:
                        text = block.get("text") if isinstance(block, dict) else None
                        if isinstance(text, str):
                            history_chars += len(text)
            out["messages"] = history_chars
        return out

    def _attribute(self) -> dict[str, int]:
        """Apportion the last-known total_tokens across layers by char share."""
        chars = self._layer_chars()
        total_chars = sum(chars.values())
        if total_chars == 0 or self._last_total_tokens == 0:
            return dict.fromkeys(chars, 0)
        return {
            name: int(self._last_total_tokens * n / total_chars)
            for name, n in chars.items()
        }

    # ── DimensionTracker ──────────────────────────────────────────
    def snapshot(self) -> dict[str, Any]:
        return {
            "total_tokens": self._last_total_tokens,
            "max_window": self._max_window,
            "utilisation": (
                round(self._last_total_tokens / self._max_window, 3)
                if self._max_window else 0.0
            ),
            "layers": self._attribute(),
            "layer_chars": self._layer_chars(),
        }

    def render_summary(self) -> str:
        if self._last_total_tokens == 0:
            return "ctx: (no llm calls yet)"
        used_k = self._last_total_tokens / 1000
        max_k = self._max_window / 1000
        return f"ctx:{used_k:.1f}k/{max_k:.0f}k"

    def render_detail(self) -> str:
        if self._last_total_tokens == 0:
            return "context_layout: no LLM calls observed yet."
        snap = self.snapshot()
        lines = [
            "## context_layout",
            f"- total: {snap['total_tokens']:,} tokens "
            f"({snap['utilisation']:.1%} of {snap['max_window']:,})",
            "- per-layer estimate (char-weighted attribution, ±15%):",
        ]
        for name, toks in snap["layers"].items():
            share = toks / snap["total_tokens"] if snap["total_tokens"] else 0
            lines.append(f"  - {name}: ~{toks:,} tokens ({share:.1%})")
        return "\n".join(lines)


# ── Aggregator ────────────────────────────────────────────────────────────

#: Default v1 dimension set. Overridable via ``loom.toml [telemetry].dimensions``.
DEFAULT_DIMENSIONS = ("tool_call", "context_layout", "memory_compression")


def _build_dimension(name: str, **kwargs: Any) -> DimensionTracker | None:
    """Factory — returns None for unknown names so unknown config keys
    degrade gracefully instead of crashing startup.
    """
    if name == "tool_call":
        return ToolCallDimension()
    if name == "context_layout":
        return ContextLayoutDimension(**kwargs)
    if name == "memory_compression":
        return MemoryCompressionDimension()
    logger.warning("Unknown telemetry dimension: %s", name)
    return None


class AgentTelemetryTracker:
    """Aggregates dimensions and handles DB persistence.

    Lifecycle:
        tracker = AgentTelemetryTracker(db, session_id, dimensions=...)
        await tracker.ensure_table()
        # record on hot path via tracker.get("tool_call").record(...)
        report = tracker.report_minimal()
        await tracker.flush()
    """

    def __init__(
        self,
        db: "aiosqlite.Connection",
        session_id: str,
        *,
        dimensions: tuple[str, ...] = DEFAULT_DIMENSIONS,
        persist_interval: int = 100,
        stack: "PromptStack | None" = None,
        messages_ref: list | None = None,
        max_window: int = 200_000,
    ) -> None:
        self._db = db
        self._session_id = session_id
        self._persist_interval = persist_interval
        self._event_count = 0
        self._dirty = False

        self._dims: dict[str, DimensionTracker] = {}
        for name in dimensions:
            kwargs: dict[str, Any] = {}
            if name == "context_layout":
                kwargs = {
                    "stack": stack,
                    "messages_ref": messages_ref,
                    "max_window": max_window,
                }
            d = _build_dimension(name, **kwargs)
            if d is not None:
                self._dims[name] = d

    # ── Dimension access ─────────────────────────────────────────
    def get(self, name: str) -> DimensionTracker | None:
        return self._dims.get(name)

    def mark_dirty(self) -> None:
        """Record an event that should eventually be flushed. Does not force
        I/O on the hot path — `maybe_flush` decides timing.
        """
        self._dirty = True
        self._event_count += 1

    async def maybe_flush(self) -> None:
        """Flush if we've accumulated enough events since the last persist."""
        if self._dirty and self._event_count >= self._persist_interval:
            await self.flush()

    # ── Persistence ──────────────────────────────────────────────
    async def ensure_table(self) -> None:
        await self._db.executescript(TELEMETRY_TABLE_DDL + TELEMETRY_INDEX_DDL)
        await self._db.commit()

    async def flush(self) -> None:
        if not self._dirty:
            return
        now = datetime.now(UTC).isoformat()
        for name, dim in self._dims.items():
            payload = json.dumps(dim.snapshot(), ensure_ascii=False)
            await self._db.execute(
                """
                INSERT INTO agent_telemetry (dimension, session_id, payload, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(dimension, session_id) DO UPDATE SET
                    payload    = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (name, self._session_id, payload, now),
            )
        await self._db.commit()
        self._dirty = False
        self._event_count = 0

    async def load_prior(self) -> None:
        """Reserved for future cross-session reporting — current dimensions
        are session-scoped, so this is a no-op for now.
        """
        return None

    # ── Reporting ────────────────────────────────────────────────
    def report_minimal(self) -> str:
        """One-line composite across dimensions (for status bar / TUI)."""
        parts = [dim.render_summary() for dim in self._dims.values()]
        return " | ".join(p for p in parts if p)

    def report_detail(self, dimension: str | None = None) -> str:
        """Full or per-dimension report (for `agent_health` tool)."""
        if dimension:
            dim = self._dims.get(dimension)
            if dim is None:
                available = ", ".join(self._dims) or "(none)"
                return f"Unknown dimension '{dimension}'. Available: {available}."
            return dim.render_detail()
        blocks = [dim.render_detail() for dim in self._dims.values()]
        return "\n\n".join(blocks)

    def anomaly_report(self) -> str | None:
        """Compact injectable string when one or more dimensions signal
        trouble. Returns None when everything is nominal — the turn-boundary
        injector uses None to stay silent.
        """
        active = [
            (d.name, d.describe_anomaly())
            for d in self._dims.values()
            if d.has_anomaly()
        ]
        if not active:
            return None
        header = (
            "⚠ AGENT TELEMETRY ALERT — one or more observability dimensions "
            "are outside expected bounds:"
        )
        body = "\n".join(f"  • [{n}] {msg}" for n, msg in active if msg)
        return f"{header}\n{body}"

    @property
    def has_issues(self) -> bool:
        return any(d.has_anomaly() for d in self._dims.values())
