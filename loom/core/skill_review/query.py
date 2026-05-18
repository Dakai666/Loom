"""Per-skill ledger query (doc/54 §4.3, §5 P0-4).

The agent (or weekly worker) asks: "what happened around skill X over
the last N days?" — this module answers with a shaped digest, not a
score. The consumer reasons about the evidence.

Shape choices:

- An *episode* is one load_skill activation. It carries the raw events
  that followed (tool_lifecycle, memory_op, judge_verdict, turn_end),
  capped to keep payloads bounded.
- Activation window:
    start = load_skill END timestamp
    end   = earliest of (explicit unload_skill END for same skill in
            the same session, next load_skill END for same skill in
            the same session = implicit re-activation boundary,
            query window's until_ts when neither exists)
  The window spans turns deliberately: agents routinely keep a skill
  loaded across many turns. Per-turn scoping silently lost cross-turn
  usage and miscounted feedback density. It does NOT span sessions:
  an unload in a different session must not close this episode and
  another session's events must not be attributed here.
- ``unload_inferred`` flags episodes whose window was closed by
  heuristic (re-load) or remained open (no unload before until_ts);
  ``unload_inferred_reason`` distinguishes the two. Surfacing these
  lets reviewers see when the agent's load/unload bookend habit fails.
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
    """One load_skill activation and the events that followed it."""

    load_event_id: str
    session_id: str
    turn_id: str                          # turn that issued load_skill
    loaded_at: float
    unloaded_at: float | None             # window-end ts, None when still open at until_ts
    turn_outcome: str | None              # outcome of the loading turn, if it ended
    events_after_load: list[LedgerEvent] = field(default_factory=list)
    truncated: bool = False               # True if events_after_load was capped
    unload_inferred: bool = False         # True when window closed by heuristic
    unload_inferred_reason: str | None = None
    # One of: None (explicit unload), "reloaded_without_unload" (next
    # load_skill END for same skill closed the window),
    # "no_unload_in_window" (no boundary found before until_ts).


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

    # Build one episode per load. Activation windows span turns within
    # the loading session: an agent may load a skill in turn N and only
    # unload it in turn N+k, with plenty of in-between work to attribute.
    # Closing the window at the loading turn's end (the original
    # behaviour) silently dropped all that evidence and made feedback
    # density look zero. Crossing *session* boundaries, however, is
    # never correct — a load in session A must not be closed by an
    # unload in session B, and B's events must not appear in A's
    # episode (see PR #393 review).
    episodes: list[SkillEpisode] = []
    for load_idx, load in enumerate(load_ends_for_episodes):
        loaded_at = load.timestamp
        load_session_id = load.session_id

        # Earliest explicit unload for this skill in the same session
        # after loaded_at. unload_ends came from a timestamp-ordered
        # fetch, so the first session-matching hit is the earliest.
        explicit_unload_ts: float | None = next(
            (
                un.timestamp
                for un in unload_ends
                if un.session_id == load_session_id and un.timestamp > loaded_at
            ),
            None,
        )

        # Earliest subsequent reload of the same skill in the same
        # session. Reloading is treated as an implicit boundary: the
        # prior activation is over as soon as the agent declares a new
        # one in the same session.
        next_reload_ts: float | None = next(
            (
                la.timestamp
                for la in load_ends
                if la.session_id == load_session_id and la.timestamp > loaded_at
            ),
            None,
        )

        # Pick the earliest of the two candidates. Each carries whether
        # it represents an inferred boundary.
        boundary_ts: float | None = None
        unload_inferred = False
        unload_inferred_reason: str | None = None
        candidates: list[tuple[float, bool, str | None]] = []
        if explicit_unload_ts is not None:
            candidates.append((explicit_unload_ts, False, None))
        if next_reload_ts is not None:
            candidates.append((next_reload_ts, True, "reloaded_without_unload"))
        if candidates:
            boundary_ts, unload_inferred, unload_inferred_reason = min(
                candidates, key=lambda c: c[0]
            )
        else:
            # No boundary in the query window — episode stays open through until_ts.
            unload_inferred = True
            unload_inferred_reason = "no_unload_in_window"

        window_end = boundary_ts if boundary_ts is not None else until_ts

        # Fetch by time range, scoped to the loading session (cross-turn
        # within session is the intended span; cross-session would
        # mis-attribute evidence — see PR #393 review). The cap keeps
        # the in-memory working set bounded for long-open windows; we
        # apply it after filtering so the cap reflects the kept events,
        # not raw fetch size.
        events_in_window = await (
            ledger.events
            .where(session_id=load_session_id)
            .since(loaded_at)
            .until(window_end)
            .order_by("timestamp")
            .all()
        )
        in_window = [
            e for e in events_in_window
            if loaded_at < e.timestamp <= window_end
            and e.event_id != load.event_id
        ]

        # turn_outcome stays bound to the loading turn (semantic anchor:
        # "the turn that activated this skill"). Scope by session_id
        # too — turn_id is not guaranteed globally unique across
        # sessions (see PR #393 review).
        load_turn_events = await (
            ledger.events
            .where(turn_id=load.turn_id, session_id=load_session_id)
            .order_by("timestamp")
            .all()
        )
        turn_outcome: str | None = None
        for e in load_turn_events:
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
            unloaded_at=boundary_ts,
            turn_outcome=turn_outcome,
            events_after_load=events_kept,
            truncated=truncated,
            unload_inferred=unload_inferred,
            unload_inferred_reason=unload_inferred_reason,
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
