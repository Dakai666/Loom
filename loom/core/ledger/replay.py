"""Replay primitive — Layer 1 raw event sequences + Layer 2 TurnSnapshot.

doc/53 §6.3 (replay primitive) + §8 (snapshot reconstruction). This is
the read-side projection consumers use without knowing about SQLite.

Layer 1 — raw event sequences:
    events_for_turn / events_for_correlation / events_for_session

Layer 2 — reconstructed snapshots:
    turn_snapshot(turn_id) -> TurnSnapshot
    correlation_snapshots(corr_id) -> list[TurnSnapshot]

Forking primitives (`ledger.replay.fork`) are explicitly v2 territory
(§10.1) — branch_id is reserved on every event but no API is exposed.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from loom.core.ledger.schema import (
    DEFAULT_BRANCH,
    LedgerEvent,
)

if TYPE_CHECKING:
    from loom.core.ledger.store import LedgerStore


# ---------------------------------------------------------------------------
# Snapshot dataclasses (doc/53 §8.1)
# ---------------------------------------------------------------------------


@dataclass(kw_only=True)
class PromptStackSnapshot:
    """Frozen view of the PromptStack at turn_start (doc/53 §3.4)."""

    prompt_stack_hash: str
    prompt_stack_components: dict[str, Any]
    full_text: str | None = None


@dataclass(kw_only=True)
class ToolCallSummary:
    """One tool call, reconstructed from its lifecycle events (§8.3)."""

    tool_call_id: str
    tool_name: str
    args_digest: str
    state_history: list[str]
    """Phase sequence per §8.1 — e.g. ['BEGIN', 'END'] or ['BEGIN', 'ROLLBACK']."""
    state_transitions: list[dict]
    """Bundled ActionState transitions from the END payload (richer than
    state_history; v0.3 stuffs the whole lifecycle transition list into
    END.state_history per doc/53 §3.1 簡化說明)."""
    result_digest: str | None = None
    result_summary: str | None = None
    rolled_back: bool = False
    error: str | None = None


@dataclass(kw_only=True)
class MemoryOpSummary:
    operation: str
    memory_id: str | None = None
    memory_ids: list[str] | None = None
    predecessor_memory_id: str | None = None
    successor_memory_id: str | None = None
    type_summary: str | None = None
    trust_tier: str | None = None
    content_digest: str | None = None
    trigger: str | None = None


@dataclass(kw_only=True)
class PermissionDecisionSummary:
    decision: str
    tool_call_id: str
    trust_level: str
    scope_grant: dict | None = None
    reason: str | None = None


@dataclass(kw_only=True)
class ArtifactRef:
    artifact_type: str
    size_bytes: int
    digest: str
    location: str | None = None


@dataclass(kw_only=True)
class TurnSnapshot:
    """All facts about one turn, projected from the ledger (§8.1)."""

    turn_id: str
    branch_id: str
    correlation_ids: set[str]
    prompt_stack_snapshot: PromptStackSnapshot | None
    tool_calls: list[ToolCallSummary] = field(default_factory=list)
    memory_ops: list[MemoryOpSummary] = field(default_factory=list)
    permission_decisions: list[PermissionDecisionSummary] = field(default_factory=list)
    judge_verdict: dict | None = None
    """First judge_verdict payload — typically 0 or 1 per turn."""
    artifacts: list[ArtifactRef] = field(default_factory=list)
    outcome: str = "<incomplete>"
    """turn_end.outcome — '<incomplete>' when the turn has no turn_end yet."""
    duration_ms: int = 0


# ---------------------------------------------------------------------------
# Free reconstruction helper (§8.3)
# ---------------------------------------------------------------------------


def reconstruct_tool_calls(events: list[LedgerEvent]) -> list[ToolCallSummary]:
    """Group tool_lifecycle events by tool_call_id and build summaries.

    Reference: doc/53 §8.3. The returned list preserves the order of
    each call's first BEGIN event so consumers can render a timeline.
    """
    by_call_id: dict[str, list[LedgerEvent]] = defaultdict(list)
    first_seen_ts: dict[str, float] = {}
    for e in events:
        if e.event_type != "tool_lifecycle":
            continue
        call_id = e.payload.get("tool_call_id")
        if call_id is None:
            continue
        by_call_id[call_id].append(e)
        first_seen_ts.setdefault(call_id, e.timestamp)

    summaries: list[ToolCallSummary] = []
    for call_id, phases in sorted(
        by_call_id.items(), key=lambda kv: first_seen_ts[kv[0]]
    ):
        phases.sort(key=lambda p: p.timestamp)
        first = phases[0]
        last = phases[-1]
        summaries.append(
            ToolCallSummary(
                tool_call_id=call_id,
                tool_name=first.payload.get("tool_name", "<unknown>"),
                args_digest=first.payload.get("args_digest", ""),
                state_history=[p.payload.get("phase", "") for p in phases],
                state_transitions=last.payload.get("state_history", []) or [],
                result_digest=last.payload.get("result_digest"),
                result_summary=last.payload.get("result_summary"),
                rolled_back=bool(last.payload.get("rolled_back", False)),
                error=last.payload.get("error"),
            )
        )
    return summaries


# ---------------------------------------------------------------------------
# LedgerReplay
# ---------------------------------------------------------------------------


class LedgerReplay:
    """Replay primitive bound to a LedgerStore.

    Use ``store.replay`` to obtain an instance — this class is not
    intended to be instantiated by application code.
    """

    def __init__(self, store: "LedgerStore") -> None:
        self._store = store

    # -- Layer 1: raw event sequences -------------------------------------

    async def events_for_turn(
        self, turn_id: str, *, branch_id: str = DEFAULT_BRANCH
    ) -> list[LedgerEvent]:
        return await self._store.fetch_by_turn(turn_id, branch_id=branch_id)

    async def events_for_correlation(
        self, correlation_id: str, *, branch_id: str = DEFAULT_BRANCH
    ) -> list[LedgerEvent]:
        return await self._fetch(
            "correlation_id=?", (correlation_id,), branch_id=branch_id
        )

    async def events_for_session(
        self, session_id: str, *, branch_id: str = DEFAULT_BRANCH
    ) -> list[LedgerEvent]:
        return await self._fetch(
            "session_id=?", (session_id,), branch_id=branch_id
        )

    # -- Layer 2: snapshots ------------------------------------------------

    async def turn_snapshot(
        self, turn_id: str, *, branch_id: str = DEFAULT_BRANCH
    ) -> TurnSnapshot:
        events = await self.events_for_turn(turn_id, branch_id=branch_id)
        return self._build_turn_snapshot(turn_id, branch_id, events)

    async def correlation_snapshots(
        self, correlation_id: str, *, branch_id: str = DEFAULT_BRANCH
    ) -> list[TurnSnapshot]:
        """Return one snapshot per turn that the correlation touched.

        A single correlation_id can span multiple turns when a subagent
        is spawned (§4.3) or an exception triggers a reaction chain
        across turn boundaries (§4.2 三類例外延伸規則).
        """
        events = await self.events_for_correlation(
            correlation_id, branch_id=branch_id
        )
        # Distinct turn_ids in the order they first appear.
        seen: dict[str, None] = {}
        for e in events:
            seen.setdefault(e.turn_id, None)

        snapshots: list[TurnSnapshot] = []
        for tid in seen:
            # turn_snapshot pulls all events for the turn (not just those
            # carrying this correlation) — caller wants the full turn view.
            snapshots.append(await self.turn_snapshot(tid, branch_id=branch_id))
        return snapshots

    # -- internal ---------------------------------------------------------

    async def _fetch(
        self, where_clause: str, params: tuple, *, branch_id: str
    ) -> list[LedgerEvent]:
        import json

        conn = self._store._require_conn()
        sql = f"""
            SELECT event_id, session_id, turn_id, parent_event_id,
                   correlation_id, branch_id, event_type, timestamp, payload
            FROM events
            WHERE branch_id=? AND {where_clause}
            ORDER BY timestamp ASC
        """
        async with conn.execute(sql, (branch_id, *params)) as cur:
            rows = await cur.fetchall()
        return [
            LedgerEvent(
                event_id=r[0],
                session_id=r[1],
                turn_id=r[2],
                parent_event_id=r[3],
                correlation_id=r[4],
                branch_id=r[5],
                event_type=r[6],
                timestamp=r[7],
                payload=json.loads(r[8]),
            )
            for r in rows
        ]

    @staticmethod
    def _build_turn_snapshot(
        turn_id: str, branch_id: str, events: list[LedgerEvent]
    ) -> TurnSnapshot:
        """Project an event list onto a TurnSnapshot.

        See doc/53 §8.2 for per-field complexity. trivial fields are
        direct lookups; tool_calls and memory_ops are medium (group +
        sort).
        """
        turn_start = next(
            (e for e in events if e.event_type == "turn_start"), None
        )
        turn_end = next(
            (e for e in events if e.event_type == "turn_end"), None
        )

        if turn_start is not None:
            ps = PromptStackSnapshot(
                prompt_stack_hash=turn_start.payload.get("prompt_stack_hash", ""),
                prompt_stack_components=turn_start.payload.get(
                    "prompt_stack_components", {}
                ),
                full_text=turn_start.payload.get("full_text"),
            )
            actual_branch = turn_start.branch_id
        else:
            ps = None
            actual_branch = branch_id

        correlation_ids: set[str] = {e.correlation_id for e in events}

        # tool_calls — medium complexity (§8.3 helper)
        tool_calls = reconstruct_tool_calls(events)

        # memory_ops — straight projection per event (§8.2 medium: keep
        # batch_read in batched form rather than expanding)
        memory_ops = [
            MemoryOpSummary(
                operation=e.payload.get("operation", ""),
                memory_id=e.payload.get("memory_id"),
                memory_ids=e.payload.get("memory_ids"),
                predecessor_memory_id=e.payload.get("predecessor_memory_id"),
                successor_memory_id=e.payload.get("successor_memory_id"),
                type_summary=e.payload.get("type_summary"),
                trust_tier=e.payload.get("trust_tier"),
                content_digest=e.payload.get("content_digest"),
                trigger=e.payload.get("trigger"),
            )
            for e in events
            if e.event_type == "memory_op"
        ]

        permission_decisions = [
            PermissionDecisionSummary(
                decision=e.payload.get("decision", ""),
                tool_call_id=e.payload.get("tool_call_id", ""),
                trust_level=e.payload.get("trust_level", ""),
                scope_grant=e.payload.get("scope_grant"),
                reason=e.payload.get("reason"),
            )
            for e in events
            if e.event_type == "permission_decision"
        ]

        judge_evt = next(
            (e for e in events if e.event_type == "judge_verdict"), None
        )
        judge_payload = judge_evt.payload if judge_evt else None

        artifacts = [
            ArtifactRef(
                artifact_type=e.payload.get("artifact_type", ""),
                size_bytes=int(e.payload.get("size_bytes", 0) or 0),
                digest=e.payload.get("digest", ""),
                location=e.payload.get("location"),
            )
            for e in events
            if e.event_type == "artifact_emit"
        ]

        if turn_end is not None:
            outcome = turn_end.payload.get("outcome", "<incomplete>")
            duration_ms = int(turn_end.payload.get("duration_ms", 0) or 0)
        else:
            outcome = "<incomplete>"
            # Fallback: span between earliest and latest event timestamps
            # × 1000. Useful when an autonomy daemon writes synthetic
            # turns without a turn_end emitter.
            if events:
                duration_ms = int(
                    (events[-1].timestamp - events[0].timestamp) * 1000
                )
            else:
                duration_ms = 0

        return TurnSnapshot(
            turn_id=turn_id,
            branch_id=actual_branch,
            correlation_ids=correlation_ids,
            prompt_stack_snapshot=ps,
            tool_calls=tool_calls,
            memory_ops=memory_ops,
            permission_decisions=permission_decisions,
            judge_verdict=judge_payload,
            artifacts=artifacts,
            outcome=outcome,
            duration_ms=duration_ms,
        )
