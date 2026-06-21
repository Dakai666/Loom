"""
Prediction Spine — auto-betting "mouth" (epic #528 P0.5-a slice B, issue #537).

P0 built the spine's metabolism (store → reconcile → calibrate) but nothing made
bets, so the reconcile schedule had an empty table to chew on. This module gives
the spine its *involuntary heartbeat*: every tool action that actually executed
leaves behind implicit bets the existing reconcile settles against the very
``action_record`` the action produced.

Two orthogonal heartbeats ride the same seam:

* **reliability** — "this tool will succeed" (``tool_success``, domain
  ``<tool>``). The original P0.5-a bet.
* **latency** — "this tool will respond fast (<1s)" (``duration_bucket``, domain
  ``<tool>@latency``). Added for the #528 acceptance gate: the reliability bet is
  near-degenerate (tools almost always succeed, so the residue is a flat row of
  zeros), while latency genuinely varies per tool — ``read_file`` is always fast,
  ``recall`` is a coin-flip — so it carries the non-trivial, per-domain variance
  the calibration residue needs to be worth anything. Both settle against the
  same action observation (``duration_ms`` is already on it), so the latency bet
  needs no change to the I2 ground-truth surface.

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

# Provenance tags on auto-generated bets — let later analysis (and slice A's
# explicit bets) tell each involuntary heartbeat apart from deliberate wagers,
# and from each other.
_AUTO_CONTEXT = "auto:implicit_tool_success"
_AUTO_DURATION_CONTEXT = "auto:implicit_duration_bucket"

# The latency heartbeat lands in a sibling domain so its residue
# (``calibration:<tool>@latency``) stays orthogonal to the reliability residue
# (``calibration:<tool>``). compute_calibration groups purely by ``domain``, so
# without this suffix a latency-miss would average into a success-miss and the
# per-domain world-model fact would be meaningless.
_LATENCY_DOMAIN_SUFFIX = "@latency"

# The naive latency prior. "fast" is the maximum-likelihood global bucket
# (~77% of all actions resolve <1s), so a per-tool ``duration_bucket=fast`` bet
# is wrong exactly where a tool is *unpredictably* slow — that residual is the
# non-trivial, per-domain variance the bare tool_success heartbeat can't produce.
_LATENCY_PRIOR = "fast"


def _betting_session(record: ActionRecord, *, enabled: bool) -> str | None:
    """Shared eligibility gate for the involuntary heartbeats.

    Returns the ``session_id`` a bet should be written under, or ``None`` when no
    bet should be made: betting disabled, the record carries no call, the action
    never reached ``EXECUTING`` (it didn't *try* — a denied/aborted action is not
    a world-model miss), or the call is sessionless (a session-less bet settles
    fine by call_id but is an orphan to per-session audit, and this writes at
    volume — 絲絲 PR #538 P3). Both heartbeats share this exact contract.
    """
    if not enabled or record.call is None:
        return None
    executed = any(t.to_state == ActionState.EXECUTING for t in record.state_history)
    if not executed:
        return None
    return record.call.session_id or None


def implicit_bet_for(record: ActionRecord, *, enabled: bool) -> PredictionRecord | None:
    """Return the implicit ``tool_success`` bet for a terminal action, or ``None``.

    ``None`` when the action is ineligible (see :func:`_betting_session`).
    Otherwise a flat, pending, per-tool bet the existing reconcile pipeline will
    settle against the action_record being persisted.
    """
    session_id = _betting_session(record, enabled=enabled)
    if session_id is None:
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


def implicit_duration_bet_for(
    record: ActionRecord, *, enabled: bool
) -> PredictionRecord | None:
    """Return the implicit *latency* bet for a terminal action, or ``None``.

    The second heartbeat (#528 acceptance-gate work). Same eligibility as
    :func:`implicit_bet_for`, but it predicts the ``duration_bucket`` against the
    naive ``fast`` prior and lands in a distinct ``<tool>@latency`` domain. This
    is the axis that actually varies per tool — ``read_file`` is always fast,
    ``recall`` is a coin-flip — so it's where the calibration residue stops being
    a flat row of zeros. Settles against the same action observation (which
    already carries ``duration_ms``), so it needs no change to the I2 surface.
    """
    session_id = _betting_session(record, enabled=enabled)
    if session_id is None:
        return None

    tool = record.call.tool_name
    return PredictionRecord(
        session_id=session_id,
        claim=f"{tool} will respond fast (<1s)",
        due_condition={"kind": "after_action", "call_id": record.call.id},
        resolver={"kind": "duration_bucket", "expect": _LATENCY_PRIOR},
        domain=f"{tool}{_LATENCY_DOMAIN_SUFFIX}",
        context=_AUTO_DURATION_CONTEXT,
    )


async def co_write_implicit_bet(db, record: ActionRecord, *, enabled: bool) -> bool:
    """Persist the implicit heartbeat bets for a terminal action.

    Returns whether *any* bet landed. The two heartbeats — reliability
    (``tool_success``) and latency (``duration_bucket``) — are written
    independently so one failing never suppresses the other.

    The IO half, kept here (not inlined in the session) so it stays testable
    without a Session. **Isolated**: a betting failure is swallowed + logged — it
    must never crash the lifecycle persistence it rides on, exactly like the
    action_record write it sits beside.
    """
    bets = [
        bet
        for bet in (
            implicit_bet_for(record, enabled=enabled),
            implicit_duration_bet_for(record, enabled=enabled),
        )
        if bet is not None
    ]
    if not bets:
        return False

    store = PredictionStore(db)
    wrote = False
    for bet in bets:
        try:
            await store.write(bet)
            wrote = True
        except Exception:  # noqa: BLE001 — betting must never crash the pipeline
            logger.debug("implicit bet write failed (suppressed)", exc_info=True)
    return wrote
