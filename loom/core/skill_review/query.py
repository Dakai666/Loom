"""Per-skill ledger query (doc/54 §4.3, §5 P0-4).

The agent (or weekly worker) asks: "what happened around skill X over
the last N days?" — this module answers with a shaped digest, not a
score. The consumer reasons about the evidence.

Shape choices:

- An *episode* is one load_skill activation. It carries the raw events
  that followed in the same turn (tool_lifecycle, memory_op, judge_verdict,
  turn_end), capped to keep payloads bounded.
- Activation window per turn: from the load_skill END event up to either
  the matching unload_skill in the same turn or the turn_end, whichever
  comes first. Most turns have at most one activation; multiple loads
  in the same turn produce overlapping episodes (no de-dup).
- Distinct sessions are surfaced for cross-session reasoning ("this skill
  has been active in 3 different sessions this week").
- No "health score". doc/54 §1 rules out process-signal grading.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from loom.core.ledger.schema import LedgerEvent

if TYPE_CHECKING:
    from loom.core.ledger.store import LedgerStore


_DEFAULT_WINDOW_SECONDS = 7 * 24 * 3600       # one week
_DEFAULT_MAX_EVENTS_PER_EPISODE = 50
_DEFAULT_MAX_EPISODES = 100


@dataclass(frozen=True)
class SkillEpisode:
    """One load_skill activation and the same-turn events that followed."""

    load_event_id: str
    session_id: str
    turn_id: str
    loaded_at: float
    unloaded_at: float | None
    turn_outcome: str | None              # from turn_end payload, if present
    events_after_load: list[LedgerEvent] = field(default_factory=list)
    truncated: bool = False               # True if events_after_load was capped


@dataclass(frozen=True)
class SkillUsageDigest:
    """Aggregate digest for a single skill over a time window."""

    skill_id: str
    since_ts: float
    until_ts: float
    load_count: int
    unload_count: int
    sessions: tuple[str, ...]
    episodes: list[SkillEpisode] = field(default_factory=list)
    episodes_truncated: bool = False      # True if total episodes > cap


async def query_skill_ledger(
    ledger: "LedgerStore",
    skill_id: str,
    *,
    since_ts: float | None = None,
    until_ts: float | None = None,
    max_events_per_episode: int = _DEFAULT_MAX_EVENTS_PER_EPISODE,
    max_episodes: int = _DEFAULT_MAX_EPISODES,
) -> SkillUsageDigest:
    """Build a per-skill usage digest from ledger events.

    Args:
        ledger: open LedgerStore.
        skill_id: target skill name (matches ``payload.skill_id``).
        since_ts: window start (Unix seconds). Defaults to 7 days ago.
        until_ts: window end (Unix seconds). Defaults to now.
        max_events_per_episode: cap on per-episode follow-on events.
        max_episodes: cap on total episodes returned.

    Returns:
        :class:`SkillUsageDigest`. ``episodes`` is sorted by ``loaded_at`` asc.
    """
    now = time.time()
    if until_ts is None:
        until_ts = now
    if since_ts is None:
        since_ts = until_ts - _DEFAULT_WINDOW_SECONDS

    # Skill-scoped events: load_skill / unload_skill tool_lifecycle pairs.
    skill_events = (
        await ledger.events
        .where(skill_id=skill_id)
        .since(since_ts)
        .until(until_ts)
        .order_by("timestamp")
        .all()
    )

    load_ends = [
        e for e in skill_events
        if e.payload.get("phase") == "END"
        and e.payload.get("tool_name") == "load_skill"
    ]
    unload_ends = [
        e for e in skill_events
        if e.payload.get("phase") == "END"
        and e.payload.get("tool_name") == "unload_skill"
    ]

    load_count = len(load_ends)
    unload_count = len(unload_ends)
    sessions = tuple(sorted({e.session_id for e in skill_events}))

    # Bound work: cap episodes constructed.
    episodes_truncated = load_count > max_episodes
    load_ends_for_episodes = load_ends[-max_episodes:] if episodes_truncated else load_ends

    # Build one episode per load. Same-turn events: fetched by turn_id and
    # filtered to the activation window. We accept the per-turn fetch cost
    # because turns are short and the alternative (one giant query then
    # in-memory bucket) is messier with no payoff at Phase 1 scale.
    episodes: list[SkillEpisode] = []
    for load in load_ends_for_episodes:
        loaded_at = load.timestamp

        # Find unload_skill END for the same skill in the same turn, if any.
        turn_unload_ts: float | None = None
        for un in unload_ends:
            if un.turn_id == load.turn_id and un.timestamp > loaded_at:
                turn_unload_ts = un.timestamp
                break

        turn_events = await ledger.events.where(turn_id=load.turn_id).order_by("timestamp").all()

        # Window: (loaded_at, unload_ts or +inf]. Strict > on loaded_at so
        # we don't re-include the load event itself.
        window_end = turn_unload_ts if turn_unload_ts is not None else float("inf")
        in_window = [
            e for e in turn_events
            if loaded_at < e.timestamp <= window_end
            and e.event_id != load.event_id
        ]

        # turn_outcome from the turn's own turn_end, regardless of window
        # (a turn either ends or it doesn't).
        turn_outcome: str | None = None
        for e in turn_events:
            if e.event_type == "turn_end":
                turn_outcome = e.payload.get("outcome")
                break

        truncated = len(in_window) > max_events_per_episode
        events_kept = in_window[:max_events_per_episode]

        episodes.append(SkillEpisode(
            load_event_id=load.event_id,
            session_id=load.session_id,
            turn_id=load.turn_id,
            loaded_at=loaded_at,
            unloaded_at=turn_unload_ts,
            turn_outcome=turn_outcome,
            events_after_load=events_kept,
            truncated=truncated,
        ))

    return SkillUsageDigest(
        skill_id=skill_id,
        since_ts=since_ts,
        until_ts=until_ts,
        load_count=load_count,
        unload_count=unload_count,
        sessions=sessions,
        episodes=episodes,
        episodes_truncated=episodes_truncated,
    )
