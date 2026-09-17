"""
In-session settlement of explicit ``predict`` bets (#528 retirement, 2026-09-17).

A ``predict`` bet binds to the *next* run of a named tool in the same session
(``next_action`` due_condition). When that run is persisted, the session calls
``settle_bets_for_action`` and appends the verdict to the tool's result, so the
agent sees "I was right / wrong" while it can still act on it.

This replaces the retired batch pipeline (dawn reconcile → calibration residue →
affect). Its corpus was ~99% the auto-heartbeat's hard-coded ``expect: fast`` /
``expect: true`` bets, narrated back to the agent as its own beliefs; the value
of a bet turned out to live at the moment it is written and the moment it is
judged — not in a rolled-up score.

Ground truth only (I2): verdicts come from the ``action_records`` row via the
resolver whitelist, never from narrative. A bet its settling action can't judge
goes ``stale`` with an explicit line — never a silent score.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from loom.core.memory.observation import (
    find_settling_observation,
    resolve_observation_ref,
)
from loom.core.memory.prediction import PredictionStore
from loom.core.memory.resolvers import resolve

# An open bet older than this is expired at session start. Long enough that a
# resumed session (Discord threads resume under the same session_id after a
# restart) can still settle its bets; short enough that a bet whose target tool
# never ran doesn't rot ``pending`` forever.
OPEN_BET_TTL = timedelta(hours=24)


async def settle_bets_for_action(db, *, session_id: str, tool_name: str) -> list[str]:
    """Settle this session's open bets on ``tool_name`` against its latest run.

    Returns one verdict line per bet touched (empty when none). Writes are the
    ``PredictionStore`` transitions only.
    """
    store = PredictionStore(db)
    lines: list[str] = []
    for bet in await store.list_open_for_session(session_id):
        cond = bet.due_condition
        if cond.get("kind") != "next_action" or cond.get("tool") != tool_name:
            continue
        ref = await find_settling_observation(db, cond)
        if ref is None:
            continue
        observation = await resolve_observation_ref(db, ref)
        try:
            if observation is None:
                raise KeyError("observation_gone")
            result = resolve(bet.resolver, observation)
        except (KeyError, ValueError):
            if await _transition(store.mark_stale(bet.id)):
                lines.append(
                    f"«{bet.claim}» — could not judge ({bet.resolver.get('kind')} "
                    f"can't read this {tool_name} run); marked stale"
                )
            continue
        if not await _transition(store.mark_reconciled(
            bet.id, score=result.error_score, observation_ref=ref,
        )):
            continue
        if result.error_score == 0.0:
            verdict = "HIT"
        elif result.error_score >= 1.0:
            verdict = "MISS"
        else:
            verdict = f"PARTIAL (error {result.error_score:.2f})"
        detail = f" — {result.detail}" if result.detail else ""
        lines.append(f"«{bet.claim}» — {verdict}{detail}")
    return lines


async def _transition(coro) -> bool:
    """Run one state transition; False when the bet is no longer open.

    Parallel runs of the same tool can both pick up one open bet — the loser's
    transition raises on the state-machine check. Skip that bet instead of
    letting it unwind the whole batch and drop the other verdicts.
    """
    try:
        await coro
    except ValueError:
        return False
    return True


async def expire_stale_bets(db, *, now: datetime | None = None) -> int:
    """Mark every open bet older than ``OPEN_BET_TTL`` stale; return the count.

    The exit for a bet whose target tool never ran. Runs at session start, so it
    doesn't depend on the ``predict`` tool being enabled or on ``stop()`` (which
    fires on bot shutdown for sessions that later resume).
    """
    cutoff = ((now or datetime.now(UTC)) - OPEN_BET_TTL).isoformat()
    cur = await db.execute(
        "UPDATE prediction_records SET status = 'stale' "
        "WHERE status IN ('pending', 'due') AND created_at < ?",
        (cutoff,),
    )
    await db.commit()
    return cur.rowcount


def format_settlement_suffix(lines: list[str]) -> str:
    """Render verdict lines as a block appended to the settling tool result."""
    if not lines:
        return ""
    body = "\n".join(f"- {line}" for line in lines)
    return f"\n\n[predict settled against this run]\n{body}"
