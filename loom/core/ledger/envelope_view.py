"""LedgerEnvelopeProjector — build ExecutionEnvelopeView from ledger events.

Phase 2 Step 5 cutover (#325 / doc/53 §11.2). Replaces the envelope's
``records[]`` mutation path: instead of ``ExecutionEnvelope.records``
being the source of truth for what happened in a tool batch, the
projector queries ``tool_lifecycle`` events for the tool_call_ids
belonging to the batch and reconstructs the view from there.

What the ledger does not track today:
- full args (Step 2 commit 5 chose args_digest only)
- auth_decision / auth_selector (live in call.metadata, set by
  BlastRadiusMiddleware)
- auth_expires (read live from session.perm at view-build time)

Those bits are supplied by the caller via ``CallMeta`` records and
the ``live_record_lookup`` / ``auth_expires_lookup`` callables. The
caller (LoomSession) populates them at tool dispatch / state-change
time. When the deferred STATE_CHANGE event emits land, the live
lookup can drop in favour of pure ledger projection.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

from loom.core.events import ExecutionEnvelopeView, ExecutionNodeView
from loom.core.ledger.replay import reconstruct_tool_calls

if TYPE_CHECKING:
    from loom.core.harness.lifecycle import ActionRecord
    from loom.core.harness.registry import ToolRegistry
    from loom.core.ledger.store import LedgerStore


# Mirrors ``ActionState._TERMINAL_STATES`` / ``_FAILURE_STATES`` as
# strings so the projector does not import the lifecycle module
# (zero-coupling: ledger reads, lifecycle writes).
_TERMINAL_STATES: frozenset[str] = frozenset(
    {"memorialized", "denied", "aborted", "timed_out"}
)
_FAILURE_STATES: frozenset[str] = frozenset(
    {"denied", "aborted", "timed_out", "reverted"}
)


@dataclass
class CallMeta:
    """Per-tool-call metadata not represented in the ledger.

    Populated by LoomSession at dispatch / authorization time; queried
    by the projector when building an ExecutionEnvelopeView. This is
    the small surface that survives Step 5's elimination of
    ``ExecutionEnvelope.records[].ActionRecord``.
    """

    call_id: str
    tool_name: str
    full_args: dict[str, Any] = field(default_factory=dict)
    args_preview: str = ""
    auth_decision: str = ""
    auth_selector: str = ""


class LedgerEnvelopeProjector:
    """Builds ExecutionEnvelopeView snapshots from ledger events.

    The projector is stateless across calls — each ``build_view()``
    invocation queries the ledger fresh. Per-batch identity (which
    call_ids belong to this envelope) and per-call metadata (args,
    auth) are passed in by the caller.
    """

    def __init__(
        self,
        store: "LedgerStore",
        registry: "ToolRegistry | None" = None,
    ) -> None:
        self._store = store
        self._registry = registry

    async def build_view(
        self,
        *,
        envelope_id: str,
        session_id: str,
        turn_id: str,
        turn_index: int,
        call_ids: list[str],
        call_meta: dict[str, CallMeta],
        live_record_lookup: Callable[[str], "ActionRecord | None"] | None = None,
        auth_expires_lookup: Callable[[str], float] | None = None,
        batch_t0: float = 0.0,
    ) -> ExecutionEnvelopeView:
        """Project the ledger into an ExecutionEnvelopeView.

        ``call_ids`` is the batch's tool_call_id set in dispatch order.
        ``call_meta`` carries args / auth fields the ledger does not
        store. ``live_record_lookup`` is a fallback for in-flight calls
        (BEGIN seen, END not yet) where the ledger does not yet have
        an authoritative state — this lets cutover preserve mid-batch
        TUI / CLI behaviour during parallel dispatch. When the deferred
        STATE_CHANGE event emits land (doc/53 §3.1 v0.3 simplification
        note), the live lookup becomes redundant.
        """
        events = await self._store.replay.events_for_turn(turn_id)
        # Filter to tool_lifecycle events whose tool_call_id belongs to
        # this envelope. reconstruct_tool_calls preserves first-seen
        # ordering — matches dispatch order natively.
        call_id_set = set(call_ids)
        batch_events = [
            e
            for e in events
            if e.event_type == "tool_lifecycle"
            and e.payload.get("tool_call_id") in call_id_set
        ]
        summaries = reconstruct_tool_calls(batch_events)

        # If a call was registered but no BEGIN landed yet (rare race),
        # also surface it as an empty placeholder so the UI sees the
        # node count caller expects.
        seen_call_ids = {s.tool_call_id for s in summaries}
        missing = [cid for cid in call_ids if cid not in seen_call_ids]

        nodes: list[ExecutionNodeView] = []
        terminal_count = 0
        failure_count = 0

        for s in summaries:
            meta = call_meta.get(s.tool_call_id)
            tdef = self._registry.get(s.tool_name) if self._registry else None

            # Derive current state. Priority:
            #   1) ledger state_transitions last "to" (authoritative when END seen)
            #   2) live_record_lookup (covers in-flight parallel dispatch)
            #   3) "executing" (BEGIN seen, no END yet)
            state = self._derive_state(s, live_record_lookup)
            if state in _TERMINAL_STATES:
                terminal_count += 1
            if state in _FAILURE_STATES or s.rolled_back:
                failure_count += 1

            duration_ms = self._batch_duration_ms(batch_events, s.tool_call_id)
            auth_expires = (
                auth_expires_lookup(s.tool_call_id) if auth_expires_lookup else 0.0
            )
            error_snippet = (s.error or "")[:80] if s.error else ""
            output_preview = (s.result_summary or "")[:200]

            nodes.append(
                ExecutionNodeView(
                    node_id=s.tool_call_id,
                    call_id=s.tool_call_id,
                    action_id=s.tool_call_id,
                    tool_name=s.tool_name,
                    level=0,
                    state=state,
                    trust_level=tdef.trust_level.plain if tdef else "SAFE",
                    capabilities=[c.name for c in tdef.capabilities] if tdef else [],
                    args_preview=meta.args_preview if meta else "",
                    duration_ms=duration_ms,
                    error_snippet=error_snippet,
                    full_args=dict(meta.full_args) if meta else {},
                    state_history=list(s.state_transitions),
                    auth_decision=meta.auth_decision if meta else "",
                    auth_expires=auth_expires,
                    auth_selector=meta.auth_selector if meta else "",
                    output_preview=output_preview,
                )
            )

        # Surface placeholders for registered-but-not-yet-emitted calls.
        for cid in missing:
            meta = call_meta.get(cid)
            tdef = (
                self._registry.get(meta.tool_name)
                if (meta and self._registry)
                else None
            )
            nodes.append(
                ExecutionNodeView(
                    node_id=cid,
                    call_id=cid,
                    action_id=cid,
                    tool_name=meta.tool_name if meta else "(unknown)",
                    level=0,
                    state="declared",
                    trust_level=tdef.trust_level.plain if tdef else "SAFE",
                    capabilities=[c.name for c in tdef.capabilities] if tdef else [],
                    args_preview=meta.args_preview if meta else "",
                    full_args=dict(meta.full_args) if meta else {},
                    auth_decision=meta.auth_decision if meta else "",
                    auth_selector=meta.auth_selector if meta else "",
                )
            )

        # Aggregate status — matches the legacy _build_envelope_view
        # logic (all terminal AND any failure → "failed"; all terminal
        # AND no failure → "completed"; otherwise "running").
        node_count = len(nodes)
        all_terminal = node_count > 0 and terminal_count == node_count
        if all_terminal and failure_count > 0:
            status = "failed"
        elif all_terminal:
            status = "completed"
        else:
            status = "running"

        elapsed_ms = (time.monotonic() - batch_t0) * 1000 if batch_t0 else 0.0

        return ExecutionEnvelopeView(
            envelope_id=envelope_id,
            session_id=session_id,
            turn_index=turn_index,
            status=status,
            node_count=node_count,
            parallel_groups=1,  # all current parallel dispatch = single level
            elapsed_ms=elapsed_ms,
            levels=[[n.node_id for n in nodes]] if nodes else [],
            nodes=nodes,
        )

    # -- helpers ---------------------------------------------------------

    @staticmethod
    def _derive_state(
        summary,
        live_record_lookup: Callable[[str], "ActionRecord | None"] | None,
    ) -> str:
        # 1) ledger has full transition history (BEGIN+END seen)
        if summary.state_transitions:
            last = summary.state_transitions[-1]
            return last.get("to", "executing")
        # 2) live ActionRecord — bridges the v0.3 STATE_CHANGE-emit gap
        if live_record_lookup is not None:
            rec = live_record_lookup(summary.tool_call_id)
            if rec is not None:
                return rec.state.value
        # 3) BEGIN seen, no END yet → assume executing
        return "executing" if "BEGIN" in summary.state_history else "declared"

    @staticmethod
    def _batch_duration_ms(events, call_id: str) -> float:
        """Compute duration_ms from BEGIN/END timestamps for one call."""
        begin_ts = None
        end_ts = None
        for e in events:
            if e.payload.get("tool_call_id") != call_id:
                continue
            phase = e.payload.get("phase")
            if phase == "BEGIN":
                begin_ts = e.timestamp
            elif phase == "END":
                end_ts = e.timestamp
        if begin_ts is None:
            return 0.0
        # Use END if available; otherwise current wall-clock vs BEGIN
        # (matches ActionRecord.elapsed_ms behaviour for in-flight calls).
        anchor = end_ts if end_ts is not None else time.time()
        return (anchor - begin_ts) * 1000.0
