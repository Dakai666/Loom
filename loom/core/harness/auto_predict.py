"""
Prediction Spine — auto-betting "mouth" (epic #528 P0.5-a slice B, issue #537).

P0 built the spine's metabolism (store → reconcile → calibrate) but nothing made
bets, so the reconcile schedule had an empty table to chew on. This module gives
the spine its *involuntary heartbeat*: every tool action that actually executed
leaves behind a flat implicit bet — "this tool will succeed" — which the existing
reconcile settles against the very ``action_record`` the action produced.

It is the harness-layer bridge (harness may import memory — the one-way arrow
Platform → Cognition → Harness → Memory holds): an :class:`ActionRecord` in,
a :class:`PredictionRecord` out. Pure and side-effect free; the session's
``_on_lifecycle`` seam does the actual write so this stays trivially testable.

The two contract decisions live here, not at the call site:

* **executed-only.** Only an action whose history reached ``EXECUTING`` is bet
  on. A permission-denied or precondition-aborted action never *tried* — its
  "failure" is not a world-model miss, and betting on it would pollute
  calibration with permission noise (calibration measures capability, not
  whether the user allowed the act).
* **flat bet, per-tool grain.** ``resolver=tool_success``, ``domain=tool_name``,
  born ``pending``, due ``after_action(call_id)``. The signal it yields is a
  per-tool success rate — a low-calibration / high-uncertainty tool is one whose
  actions often fail, exactly what the exploration arm (P2) should later be drawn
  to. Richer, confidence-varying bets are slice A's job.
"""

from __future__ import annotations

import logging

from loom.core.harness.lifecycle import ActionRecord, ActionState
from loom.core.memory.prediction import PredictionRecord, PredictionStore

logger = logging.getLogger(__name__)

# Provenance tag on auto-generated bets — lets later analysis (and slice A's
# explicit bets) tell the involuntary heartbeat apart from deliberate wagers.
_AUTO_CONTEXT = "auto:implicit_tool_success"


def implicit_bet_for(record: ActionRecord, *, enabled: bool) -> PredictionRecord | None:
    """Return the implicit ``tool_success`` bet for a terminal action, or ``None``.

    ``None`` when betting is disabled, the record carries no call, or the action
    never reached ``EXECUTING`` (it didn't try — see module docstring). Otherwise
    a flat, pending, per-tool bet the existing reconcile pipeline will settle.
    """
    if not enabled or record.call is None:
        return None

    executed = any(t.to_state == ActionState.EXECUTING for t in record.state_history)
    if not executed:
        return None

    # No session → no bet. A session-less bet would reconcile fine (it settles by
    # call_id) but be an orphan to any per-session audit — and this writes
    # autonomously at volume, so "don't bet rather than write orphan data" keeps
    # the spine's own hygiene clean (絲絲 PR #538 P3). Drops the empty fallback.
    session_id = record.call.session_id
    if not session_id:
        return None

    tool = record.call.tool_name
    return PredictionRecord(
        session_id=session_id,
        claim=f"{tool} will succeed",
        due_condition={"kind": "after_action", "call_id": record.call.id},
        resolver={"kind": "tool_success", "expect": True},
        domain=tool,
        context=_AUTO_CONTEXT,
    )


async def co_write_implicit_bet(db, record: ActionRecord, *, enabled: bool) -> bool:
    """Persist the implicit bet for a terminal action; return whether one landed.

    The IO half, kept here (not inlined in the session) so it stays testable
    without a Session. **Isolated**: a betting failure is swallowed + logged and
    returns ``False`` — it must never crash the lifecycle persistence it rides
    on, exactly like the action_record write it sits beside.
    """
    bet = implicit_bet_for(record, enabled=enabled)
    if bet is None:
        return False
    try:
        await PredictionStore(db).write(bet)
        return True
    except Exception:  # noqa: BLE001 — betting must never crash the pipeline
        logger.debug("implicit bet write failed (suppressed)", exc_info=True)
        return False
