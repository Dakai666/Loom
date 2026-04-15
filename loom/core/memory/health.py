"""
MemoryHealthTracker — self-observable memory subsystem health.

Issue #133: Replaces silent error swallowing with a persistent,
queryable health record that the agent itself can inspect.

Design
------
- In-memory counters accumulate during a session (zero overhead on hot path)
- ``flush()`` persists to ``memory_health`` table at session end
- ``load_prior()`` reads last-session state at startup so the agent can
  see problems that occurred in a previous session
- ``report()`` produces a structured summary for agent self-diagnosis

The tracker is attached to ``MemoryGovernor`` (always-on) and exposed
to the agent via the ``memory_health`` tool.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, UTC
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    import aiosqlite

logger = logging.getLogger(__name__)

# Operations tracked by the health system
OPERATIONS = (
    "embedding_write",
    "embedding_search",
    "session_compress",
    "session_log_write",
    "decay_cycle",
    "skill_evolution",
    "governed_upsert",
)

# ── Schema (added via runtime migration in store.py) ─────────────────────
HEALTH_TABLE_DDL = """\
CREATE TABLE IF NOT EXISTS memory_health (
    operation      TEXT NOT NULL,
    session_id     TEXT NOT NULL,
    success_count  INTEGER NOT NULL DEFAULT 0,
    failure_count  INTEGER NOT NULL DEFAULT 0,
    last_failure_at   TEXT,
    last_failure_msg  TEXT,
    updated_at     TEXT NOT NULL,
    PRIMARY KEY (operation, session_id)
);
"""

HEALTH_INDEX_DDL = """\
CREATE INDEX IF NOT EXISTS idx_memory_health_updated
ON memory_health(updated_at DESC);
"""


# ── Data types ────────────────────────────────────────────────────────────

@dataclass
class OperationHealth:
    """Health counters for a single operation type."""
    operation: str
    success_count: int = 0
    failure_count: int = 0
    last_failure_at: str | None = None
    last_failure_msg: str | None = None

    @property
    def total(self) -> int:
        return self.success_count + self.failure_count

    @property
    def failure_rate(self) -> float:
        return self.failure_count / self.total if self.total > 0 else 0.0

    @property
    def is_healthy(self) -> bool:
        return self.failure_count == 0

    @property
    def status_icon(self) -> str:
        if self.failure_count == 0:
            return "OK"
        if self.failure_rate < 0.1:
            return "DEGRADED"
        return "FAILING"


@dataclass
class HealthReport:
    """Aggregate health across all tracked operations."""
    session_id: str
    operations: dict[str, OperationHealth] = field(default_factory=dict)
    prior_session_issues: list[OperationHealth] = field(default_factory=list)

    @property
    def has_issues(self) -> bool:
        return any(not op.is_healthy for op in self.operations.values())

    @property
    def has_prior_issues(self) -> bool:
        return len(self.prior_session_issues) > 0

    def render_summary(self) -> str:
        """Human/agent-readable health summary."""
        lines: list[str] = []

        # Current session
        current_issues = [
            op for op in self.operations.values() if not op.is_healthy
        ]
        if current_issues:
            lines.append("## Current Session Memory Health Issues")
            for op in current_issues:
                lines.append(
                    f"- **{op.operation}**: {op.status_icon} "
                    f"({op.failure_count}/{op.total} failed)"
                )
                if op.last_failure_msg:
                    lines.append(f"  Last error: {op.last_failure_msg[:200]}")
        else:
            lines.append("## Memory Health: All systems nominal this session.")

        # Prior session issues
        if self.prior_session_issues:
            lines.append("")
            lines.append("## Prior Session Issues (unresolved)")
            for op in self.prior_session_issues:
                lines.append(
                    f"- **{op.operation}**: {op.failure_count} failure(s) "
                    f"at {op.last_failure_at or 'unknown time'}"
                )
                if op.last_failure_msg:
                    lines.append(f"  Last error: {op.last_failure_msg[:200]}")

        return "\n".join(lines)

    def render_agent_context(self) -> str | None:
        """Compact note for injection into agent system context.

        Returns None when everything is healthy — no noise.
        """
        issues: list[str] = []

        for op in self.prior_session_issues:
            issues.append(
                f"[PRIOR] {op.operation}: {op.failure_count} failure(s) — "
                f"{op.last_failure_msg or 'unknown error'}"
            )

        current_issues = [
            op for op in self.operations.values() if not op.is_healthy
        ]
        for op in current_issues:
            issues.append(
                f"[NOW] {op.operation}: {op.failure_count}/{op.total} failed — "
                f"{op.last_failure_msg or 'unknown error'}"
            )

        if not issues:
            return None

        header = (
            "⚠ MEMORY HEALTH ALERT — The following memory subsystems have "
            "recorded failures. You may want to investigate or inform the user."
        )
        return header + "\n" + "\n".join(f"  • {i}" for i in issues)


# ── Tracker ───────────────────────────────────────────────────────────────

class MemoryHealthTracker:
    """In-memory accumulator with DB persistence for memory health events.

    Usage:
        tracker = MemoryHealthTracker(db, session_id)
        await tracker.ensure_table()
        await tracker.load_prior()        # reads last-session issues

        tracker.record_success("embedding_write")
        tracker.record_failure("embedding_write", "API timeout")

        report = tracker.report()          # structured report
        await tracker.flush()              # persist to DB
    """

    def __init__(self, db: "aiosqlite.Connection", session_id: str) -> None:
        self._db = db
        self._session_id = session_id
        self._counters: dict[str, OperationHealth] = {}
        self._prior_issues: list[OperationHealth] = []
        self._dirty = False

    def _get_or_create(self, operation: str) -> OperationHealth:
        if operation not in self._counters:
            self._counters[operation] = OperationHealth(operation=operation)
        return self._counters[operation]

    # ── Recording (hot path — no I/O) ─────────────────────────────────

    def record_success(self, operation: str) -> None:
        """Record a successful memory operation."""
        self._get_or_create(operation).success_count += 1
        self._dirty = True

    def record_failure(self, operation: str, error_msg: str) -> None:
        """Record a failed memory operation."""
        op = self._get_or_create(operation)
        op.failure_count += 1
        op.last_failure_at = datetime.now(UTC).isoformat()
        op.last_failure_msg = str(error_msg)[:500]
        self._dirty = True

    # ── Persistence ───────────────────────────────────────────────────

    async def ensure_table(self) -> None:
        """Create the health table if it doesn't exist."""
        await self._db.executescript(HEALTH_TABLE_DDL + HEALTH_INDEX_DDL)
        await self._db.commit()

    async def flush(self) -> None:
        """Persist current session counters to DB."""
        if not self._dirty:
            return
        now = datetime.now(UTC).isoformat()
        for op in self._counters.values():
            if op.total == 0:
                continue
            await self._db.execute(
                """
                INSERT INTO memory_health
                    (operation, session_id, success_count, failure_count,
                     last_failure_at, last_failure_msg, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(operation, session_id) DO UPDATE SET
                    success_count    = excluded.success_count,
                    failure_count    = excluded.failure_count,
                    last_failure_at  = COALESCE(excluded.last_failure_at, memory_health.last_failure_at),
                    last_failure_msg = COALESCE(excluded.last_failure_msg, memory_health.last_failure_msg),
                    updated_at       = excluded.updated_at
                """,
                (
                    op.operation,
                    self._session_id,
                    op.success_count,
                    op.failure_count,
                    op.last_failure_at,
                    op.last_failure_msg,
                    now,
                ),
            )
        await self._db.commit()
        self._dirty = False

    async def load_prior(self) -> None:
        """Load issues from recent sessions (not the current one).

        Only loads operations that had failures — healthy history is
        not interesting for self-diagnosis.
        """
        cursor = await self._db.execute(
            """
            SELECT operation,
                   SUM(failure_count) as total_failures,
                   MAX(last_failure_at) as latest_failure_at,
                   last_failure_msg
            FROM memory_health
            WHERE session_id != ?
              AND failure_count > 0
              AND updated_at > datetime('now', '-7 days')
            GROUP BY operation
            ORDER BY latest_failure_at DESC
            """,
            (self._session_id,),
        )
        rows = await cursor.fetchall()
        self._prior_issues = [
            OperationHealth(
                operation=row[0],
                failure_count=row[1],
                last_failure_at=row[2],
                last_failure_msg=row[3],
            )
            for row in rows
        ]

    # ── Reporting ─────────────────────────────────────────────────────

    def report(self) -> HealthReport:
        """Build a structured health report."""
        return HealthReport(
            session_id=self._session_id,
            operations=dict(self._counters),
            prior_session_issues=list(self._prior_issues),
        )

    @property
    def has_issues(self) -> bool:
        """Quick check: any failures this session or from prior sessions?"""
        if self._prior_issues:
            return True
        return any(not op.is_healthy for op in self._counters.values())
