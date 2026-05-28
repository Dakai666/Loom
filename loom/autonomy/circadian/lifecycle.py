"""
Circadian lifecycle — the idempotent daily-session entry point and its wiring.

Everything that needs "make sure today's daily session exists" funnels through
``ensure_today_session`` (doc/57 §5.1): dawn cron, bot ``on_ready`` catch-up,
and (later) the state-drift watchdog. Calling it once or a thousand times has
the same effect — a per-process async lock plus a cross-process ``state_lock``
arbitrate so only one caller spawns.

This module is platform-agnostic. The Discord-specific work (creating a thread,
verifying it's alive, closing + stopping the session) lives behind the
``CircadianPlatform`` protocol, which the bot implements. Triggers, delivery,
and scheduling are all reused from the existing autonomy stack — circadian adds
no new runtime (doc/57 §1.1, §6).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from loom.autonomy.chime import ChimeRequest
from loom.autonomy.circadian.proposal import (
    APPLIED_SUBDIR,
    CONFLICTS_SUBDIR,
    PROPOSALS_DIR,
    load_proposal,
    proposal_path,
)
from loom.autonomy.circadian.rhythm import Anchor, load_rhythm
from loom.autonomy.circadian.state import CircadianState, _today_str, state_lock
from loom.autonomy.circadian.weave import load_weave
from loom.autonomy.triggers import CronTrigger

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CircadianConfig:
    """Parsed ``[autonomy.circadian]`` block. Behaviour only — the channel id
    comes from the ``DISCORD_CHANNEL_ID`` env var the bot already reads, not
    from here (doc/57 §2, .env-vs-toml convention)."""

    enabled: bool = False
    timezone: str = "Asia/Taipei"
    start: str = "08:00"
    sleep: str = "00:00"
    session_prefix: str = "daily-life"

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "CircadianConfig":
        raw = raw or {}
        return cls(
            enabled=bool(raw.get("enabled", False)),
            timezone=str(raw.get("timezone", "Asia/Taipei")),
            start=str(raw.get("start", "08:00")),
            sleep=str(raw.get("sleep", "00:00")),
            session_prefix=str(raw.get("session_prefix", "daily-life")),
        )

    def thread_name(self, tz_date: str) -> str:
        return f"{self.session_prefix}-{tz_date}"


# ---------------------------------------------------------------------------
# Platform adapter
# ---------------------------------------------------------------------------

class CircadianPlatform(Protocol):
    """The platform-side hooks circadian needs. The Discord bot supplies these;
    the CLI can supply no-ops. Keeps the autonomy package free of any Discord
    import (Platform → Cognition → Harness one-way rule, doc/57 §3.3)."""

    def resolve_channel_id(self) -> int | None:
        """Channel the daily thread is opened in (from ``DISCORD_CHANNEL_ID`` /
        the bot's allowed channels). ``None`` ⇒ cannot spawn."""
        ...

    async def spawn_daily_thread(self, *, name: str, channel_id: int) -> tuple[int, str]:
        """Create the daily thread and start its session. Returns
        ``(thread_id, session_id)``. Raises on failure (caller logs + skips)."""
        ...

    async def verify_thread_alive(self, thread_id: int) -> bool:
        """True if the thread still exists on the platform (not deleted)."""
        ...

    async def ensure_session_loaded(self, thread_id: int) -> None:
        """Idempotently resume the in-memory session for an existing thread so
        chimes / user messages have somewhere to land (bot-restart case)."""
        ...

    async def close_daily_session(self, thread_id: int) -> None:
        """Close the thread's session, triggering ``session.stop()`` →
        memory finalization / compression. Must be safe to call once."""
        ...


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def _parse_hhmm(hhmm: str) -> int:
    h, m = (int(x) for x in hhmm.split(":"))
    return h * 60 + m


def is_in_active_hours(now_local: datetime, config: CircadianConfig) -> bool:
    """Whether ``now_local`` falls inside [start, sleep). ``sleep="00:00"`` means
    end-of-day (24:00); a sleep earlier than start wraps past midnight."""
    start = _parse_hhmm(config.start)
    sleep = _parse_hhmm(config.sleep) or 24 * 60  # 00:00 ⇒ 1440 (end of day)
    now_min = now_local.hour * 60 + now_local.minute
    if start < sleep:
        return start <= now_min < sleep
    return now_min >= start or now_min < sleep  # wraps midnight


def local_hhmm_to_utc_cron(hhmm: str, tz: str) -> str:
    """Convert a local ``HH:MM`` to a UTC 5-field cron the evaluator can match.

    The evaluator fires crons against ``datetime.now(UTC)`` (``CronTrigger``'s
    ``timezone`` field is not applied at match time), so we resolve the local
    wall-clock time to UTC here. Computed once at registration; timezones with
    DST could drift by an hour across a transition — acceptable for V1 since
    the default zone (Asia/Taipei) has no DST. The date-string comparison in
    ``CircadianState`` remains the source of truth for day boundaries (§5)."""
    minutes = _parse_hhmm(hhmm)
    local = datetime.now(ZoneInfo(tz)).replace(
        hour=minutes // 60, minute=minutes % 60, second=0, microsecond=0
    )
    utc = local.astimezone(timezone.utc)
    return f"{utc.minute} {utc.hour} * * *"


# ---------------------------------------------------------------------------
# In-process serialization
# ---------------------------------------------------------------------------

_inproc_lock: asyncio.Lock | None = None
_inproc_lock_loop: Any = None


def _get_inproc_lock() -> asyncio.Lock:
    """An ``asyncio.Lock`` bound to the running loop. Rebuilt if the loop
    changed (each pytest test gets a fresh loop) so we never acquire a lock
    bound to a closed loop. Pairs with the cross-process ``state_lock``: this
    one stops two coroutines in *this* process from both holding the blocking
    fcntl lock across an ``await`` (which would deadlock the single thread)."""
    global _inproc_lock, _inproc_lock_loop
    loop = asyncio.get_running_loop()
    if _inproc_lock is None or _inproc_lock_loop is not loop:
        _inproc_lock = asyncio.Lock()
        _inproc_lock_loop = loop
    return _inproc_lock


async def _emit(evaluator: Any, name: str, payload: dict[str, Any]) -> None:
    """Fire a circadian lifecycle event, swallowing subscriber failures.

    Always called *after* releasing the state lock so a subscriber's agent run
    can't stall the lifecycle (doc/57 §13.1)."""
    if evaluator is None:
        return
    try:
        await evaluator.emit(name, dict(payload))
    except Exception:  # noqa: BLE001 — a bad subscriber must not break the day
        logger.exception("[circadian] emit %s failed", name)


# ---------------------------------------------------------------------------
# Core lifecycle
# ---------------------------------------------------------------------------

async def ensure_today_session(
    now: datetime,
    platform: CircadianPlatform,
    config: CircadianConfig,
    *,
    evaluator: Any = None,
) -> CircadianState | None:
    """Guarantee today's daily session exists; return its state (or ``None`` if
    spawning was impossible / handled by another holder). Idempotent.

    Decision (doc/57 §5.1): if today's state is present and its thread is alive,
    resume the session and return. If it's yesterday's leftover, recover it
    first. Otherwise spawn a fresh thread + session and record state."""
    tz = config.timezone
    pending_emits: list[tuple[str, dict[str, Any]]] = []
    spawned: CircadianState | None = None

    async with _get_inproc_lock():
        with state_lock(blocking=False) as acquired:
            if not acquired:
                # Another process is mid-spawn; let it win (idempotent outcome).
                logger.info("[circadian] ensure: state lock held elsewhere — deferring")
                return CircadianState.load()

            state = CircadianState.load()
            if state is not None and state.is_for_today(tz):
                if await platform.verify_thread_alive(state.thread_id):
                    await platform.ensure_session_loaded(state.thread_id)
                    return state
                logger.warning(
                    "[circadian] today's thread %s no longer exists — rebuilding",
                    state.thread_id,
                )
                # Best-effort finalize the orphaned session before replacing it.
                # Without this the old thread's session stays loaded + registered
                # until shutdown, so a stale discord_thread chime target would
                # still pass deliver_chime() and the session would miss its
                # session.stop() memory finalization (PR #476 review P1).
                try:
                    await platform.close_daily_session(state.thread_id)
                except Exception:  # noqa: BLE001 — never block the rebuild
                    logger.exception(
                        "[circadian] closing orphaned session for thread %s failed",
                        state.thread_id,
                    )
            elif state is not None and not state.is_for_today(tz):
                closed = await _close_and_archive(state, platform, config)
                if closed is not None:
                    pending_emits.append(closed)

            channel_id = platform.resolve_channel_id()
            if channel_id is None:
                logger.warning(
                    "[circadian] no channel configured (DISCORD_CHANNEL_ID); "
                    "cannot spawn daily session"
                )
                return None

            name = config.thread_name(_today_str(tz))
            try:
                thread_id, session_id = await platform.spawn_daily_thread(
                    name=name, channel_id=channel_id
                )
            except Exception:  # noqa: BLE001 — spawn IO failed; leave no state
                logger.exception("[circadian] spawn_daily_thread failed for %s", name)
                return None

            spawned = CircadianState.new_for_today(
                thread_id=thread_id,
                session_id=session_id,
                channel_id=channel_id,
                now=now,
                tz=tz,
            )
            spawned.append_phase("dawn", "spawned")
            spawned.save_atomic()
            pending_emits.append(
                ("circadian:day_started", {
                    "date": spawned.date,
                    "thread_id": spawned.thread_id,
                    "session_id": spawned.session_id,
                })
            )

    for evt_name, payload in pending_emits:
        await _emit(evaluator, evt_name, payload)
    return spawned


async def close_today(
    platform: CircadianPlatform,
    config: CircadianConfig,
    *,
    evaluator: Any = None,
) -> None:
    """Nightly close: stop today's session (→ memory finalization), stamp
    ``closed_at``, and archive the state. No-op when there's no live state.

    The heavy ``session.stop()`` runs *outside* the state lock so a long
    compaction can't block a racing reader; only the small archive write is
    serialized."""
    state = CircadianState.load()
    if state is None:
        logger.info("[circadian] nightly close: no live state to close")
        return
    if state.closed_at is not None:
        logger.info("[circadian] nightly close: %s already closed", state.date)
        return

    await platform.close_daily_session(state.thread_id)
    closed_at = datetime.now(ZoneInfo(config.timezone)).isoformat()

    async with _get_inproc_lock():
        with state_lock() as acquired:
            if not acquired:  # pragma: no cover — blocking lock yields True
                return
            fresh = CircadianState.load() or state
            fresh.closed_at = closed_at
            fresh.append_phase("nightly_close", "closed")
            fresh.archive_and_reset()

    await _emit(evaluator, "circadian:day_closed", {
        "date": state.date,
        "thread_id": state.thread_id,
        "closed_at": closed_at,
    })


async def recover_on_startup(
    platform: CircadianPlatform,
    config: CircadianConfig,
    *,
    evaluator: Any = None,
) -> None:
    """Reconcile leftover state when the daemon (re)starts (doc/57 §5, rows 2–4).

    - same-day, thread alive → resume the session (bot may have lost it)
    - yesterday, not yet closed → force-close + archive (the missed nightly)
    - yesterday, already closed → just archive away the stale record

    Never spawns today — that's the dawn cron's job (or the bot ``on_ready``
    catch-up if the daemon came up after dawn)."""
    state = CircadianState.load()
    if state is None:
        return

    if state.is_for_today(config.timezone):
        if await platform.verify_thread_alive(state.thread_id):
            await platform.ensure_session_loaded(state.thread_id)
            logger.info("[circadian] startup: resumed today's session %s", state.session_id)
        else:
            logger.warning(
                "[circadian] startup: today's thread %s gone; dawn/on_ready will rebuild",
                state.thread_id,
            )
        return

    logger.info("[circadian] startup: reconciling stale state from %s", state.date)
    closed = await _close_and_archive(state, platform, config)
    if closed is not None:
        await _emit(evaluator, *closed)


async def _close_and_archive(
    state: CircadianState,
    platform: CircadianPlatform,
    config: CircadianConfig,
) -> tuple[str, dict[str, Any]] | None:
    """Close (if still open) and archive a stale day's state. Returns the
    ``circadian:day_closed`` emit tuple when this call performed the close, else
    ``None``. Shared by startup recovery and the defensive cross-day path in
    ``ensure_today_session``."""
    emit: tuple[str, dict[str, Any]] | None = None
    if state.closed_at is None:
        try:
            await platform.close_daily_session(state.thread_id)
        except Exception:  # noqa: BLE001 — best effort; still archive the record
            logger.exception(
                "[circadian] recovery close failed for thread %s", state.thread_id
            )
        state.closed_at = datetime.now(ZoneInfo(config.timezone)).isoformat()
        emit = ("circadian:day_closed", {
            "date": state.date,
            "thread_id": state.thread_id,
            "closed_at": state.closed_at,
        })
    state.archive_and_reset()
    return emit


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------

_DAWN_INTENT = (
    "Circadian dawn: ensure today's daily-life session exists. Deterministic "
    "spawn — not an agent run."
)
_CLOSE_INTENT = (
    "Circadian nightly close: stop today's daily-life session and trigger "
    "memory finalization. Deterministic — not an agent run."
)


def register_triggers(daemon: Any, platform: CircadianPlatform, config: CircadianConfig) -> None:
    """Register the dawn-spawn and nightly-close cron triggers with deterministic
    direct handlers (bypassing the LLM planner). Idempotent registration: the
    evaluator keys triggers by name, so re-registering overwrites cleanly."""
    evaluator = daemon.evaluator
    dawn_cron = local_hhmm_to_utc_cron(config.start, config.timezone)
    close_cron = local_hhmm_to_utc_cron(config.sleep, config.timezone)

    async def _dawn_handler(_trigger: Any, _context: dict[str, Any]) -> None:
        await ensure_today_session(
            datetime.now(timezone.utc), platform, config, evaluator=evaluator
        )

    async def _close_handler(_trigger: Any, _context: dict[str, Any]) -> None:
        await close_today(platform, config, evaluator=evaluator)

    evaluator.register(CronTrigger(
        name="circadian:dawn_spawn", intent=_DAWN_INTENT,
        cron=dawn_cron, timezone=config.timezone, notify=False,
    ))
    daemon.register_direct_handler("circadian:dawn_spawn", _dawn_handler)

    evaluator.register(CronTrigger(
        name="circadian:nightly_close", intent=_CLOSE_INTENT,
        cron=close_cron, timezone=config.timezone, notify=False,
    ))
    daemon.register_direct_handler("circadian:nightly_close", _close_handler)

    logger.info(
        "[circadian] registered dawn=%s close=%s (%s) for channel resolution at spawn",
        dawn_cron, close_cron, config.timezone,
    )


def _make_phase_handler(daemon: Any, config: CircadianConfig, anchor: Anchor):
    """Build the cron handler for one rhythm anchor.

    Bound as a default arg so closing over the loop variable in
    ``register_rhythm_anchors`` doesn't collapse all handlers onto the last
    anchor (the classic Python late-binding trap).
    """

    async def _phase_handler(_trigger: Any, _context: dict[str, Any]) -> None:
        await _deliver_phase_chime(daemon, config, anchor)

    return _phase_handler


async def _deliver_phase_chime(
    daemon: Any, config: CircadianConfig, anchor: Anchor
) -> None:
    """Fire one phase chime: build the request, route via the daemon's chime
    callback, append the outcome to ``phase_log``.

    Body composition (PR3 #461 adds the second layer):
      - ``anchor.meaning`` (rhythm.toml, stable) — *why* this phase exists
      - ``daily_weave.md`` section matching ``anchor.name`` — today's *what*

    Both are markdown; the agent sees them concatenated under a 「今日織程」
    sub-heading so it can tell the difference between scaffolding and today's
    specifics. Missing weave file or missing section is normal — chimes fall
    through to ``meaning`` alone so a fresh install with no weave plan still
    works (PR2 acceptance preserved).

    When the bot can't deliver (no live thread, ``deliver_chime`` returns
    False) we still log a ``skipped`` outcome so phase_log is a complete
    audit trail.
    """
    intent = _compose_chime_intent(anchor, config)
    req = ChimeRequest(
        schedule_name=anchor.trigger_name,
        intent=intent,
        fired_at=datetime.now(timezone.utc),
        target={"type": "circadian_today", "fallback": "skip"},
    )
    delivered = await daemon.deliver_chime(req)
    outcome = "delivered" if delivered else "skipped"
    reason = None if delivered else "no_today_session"
    _record_phase_outcome(anchor.name, outcome, reason, config.timezone)


def _compose_chime_intent(anchor: Anchor, config: CircadianConfig) -> str:
    """Stitch rhythm meaning + today's weave section into the chime body.

    Layers (composed in order, each optional):
      1. ``anchor.meaning`` — rhythm.toml, the *why* of this phase (PR2)
      2. ``**今日織程**\\n{section}`` — daily_weave.md, today's *what* (PR3)
      3. ``**昨夜你改了什麼**\\n…`` — yesterday's weave_revise summary,
         dawn-anchor only (PR4 #462). Lets DK find out about overnight
         self-revisions without a confirm round-trip.

    When every layer is empty we fall back to a generic intent so the chime
    is never an empty string (agents handle that poorly). Each sub-heading
    is the contract that tells the agent which layer it's reading.
    """
    meaning = anchor.meaning.strip()
    weave = load_weave()
    section = weave.section_for(anchor.name)

    parts: list[str] = []
    if meaning:
        parts.append(meaning)
    if section:
        parts.append(f"**今日織程**\n{section}")

    if anchor.name == "dawn":
        revision_report = _yesterday_revision_report(config.timezone)
        if revision_report:
            parts.append(revision_report)

    if not parts:
        return f"Circadian phase: {anchor.name}"
    return "\n\n".join(parts)


def _yesterday_revision_report(tz: str) -> str | None:
    """Look up yesterday's evening_closure proposal artifact and turn it
    into the 「昨夜你改了什麼」 chime layer.

    Two possible artifacts:
      - ``proposals/applied/{yesterday}-evening.toml`` — revise succeeded
      - ``proposals/conflicts/{yesterday}-evening.toml`` — DK hand-edit
        won the mtime race; proposal was parked, daily_weave.md untouched

    Returns ``None`` when neither exists (no overnight revision happened).
    The applied case nudges the agent to brief DK; the conflicts case
    surfaces the parked attempt so DK can decide what to do.
    """
    yesterday = (
        datetime.now(ZoneInfo(tz)) - timedelta(days=1)
    ).strftime("%Y-%m-%d")

    applied = proposal_path(yesterday, PROPOSALS_DIR / APPLIED_SUBDIR)
    conflict = proposal_path(yesterday, PROPOSALS_DIR / CONFLICTS_SUBDIR)

    proposal = load_proposal(applied)
    if proposal is not None:
        summary = "\n".join(f"- {line}" for line in proposal.summary_lines())
        return (
            "**昨夜你改了什麼**\n"
            f"{summary}\n"
            f"\n_理由_：{proposal.rationale}\n"
            "\n→ 開場時跟 DK 簡述一下你昨夜對明天織程的調整。"
        )

    parked = load_proposal(conflict)
    if parked is not None:
        summary = "\n".join(f"- {line}" for line in parked.summary_lines())
        return (
            "**昨夜的調整被擋下了（DK 半夜手改過 daily_weave.md）**\n"
            f"{summary}\n"
            f"\n_當時的理由_：{parked.rationale}\n"
            f"_park 在_：{conflict}\n"
            "\n→ 告訴 DK 這件事，問他要不要重 propose 或直接跳過。"
        )

    return None


def _record_phase_outcome(
    phase: str, outcome: str, reason: str | None, tz: str
) -> None:
    """Append one phase fire to today's ``phase_log`` under the state lock.

    Best-effort: if there's no live state, or it's not for today, or another
    writer holds the lock, we drop the record rather than block the cron.
    The lifecycle ``ensure_today_session`` / ``close_today`` paths are the
    authoritative writers; this is a sidecar audit trail.
    """
    with state_lock(blocking=False) as acquired:
        if not acquired:
            logger.debug("[circadian] phase_log skip for %s — state lock held", phase)
            return
        state = CircadianState.load()
        if state is None or not state.is_for_today(tz):
            return
        state.append_phase(phase, outcome, reason)
        try:
            state.save_atomic()
        except OSError:
            logger.exception("[circadian] phase_log save failed for %s", phase)


def register_rhythm_anchors(
    daemon: Any, config: CircadianConfig, anchors: list[Anchor]
) -> int:
    """Register one CronTrigger + direct handler per rhythm anchor.

    Idempotent: re-registering overwrites by trigger name (the evaluator
    keys triggers by name). Anchor times are local wall-clock in
    ``config.timezone`` — converted to UTC cron the same way the dawn/close
    triggers in :func:`register_triggers` are, so a single timezone source
    of truth governs them all. Returns the number of anchors wired.
    """
    evaluator = daemon.evaluator
    for anchor in anchors:
        cron = local_hhmm_to_utc_cron(anchor.time, config.timezone)
        evaluator.register(CronTrigger(
            name=anchor.trigger_name,
            intent=f"Circadian phase '{anchor.name}' — read from rhythm table",
            cron=cron,
            timezone=config.timezone,
            notify=False,
        ))
        daemon.register_direct_handler(
            anchor.trigger_name, _make_phase_handler(daemon, config, anchor)
        )
    if anchors:
        logger.info(
            "[circadian] registered %d phase anchor(s) from rhythm table: %s",
            len(anchors),
            ", ".join(f"{a.name}@{a.time}" for a in anchors),
        )
    return len(anchors)


async def setup_circadian(
    daemon: Any, platform: CircadianPlatform, config: CircadianConfig
) -> bool:
    """One-shot wiring called from the bot+daemon assembly point *after the
    Discord client is ready* (so adapter calls reach a live connection).

    Startup steps (doc/57 §5, §9 restart-verification block; PR2 #460 adds
    step 2 — rhythm-driven phase anchors):

    1. Register the dawn/close cron triggers.
    2. Read the per-agent rhythm table and register one cron anchor per phase
       (zero ``loom.toml`` schedule entries; the table is the source of truth
       for "what does today look like"). A missing/malformed table logs a
       warning and leaves circadian to dawn + close only — the lifecycle
       still runs.
    3. Recover leftover state (resume same-day, force-close stale yesterday).
    4. Catch-up spawn: if we came up during active hours, run the idempotent
       ``ensure_today_session`` so a daemon/bot that started after dawn still
       joins today's rhythm. ``ensure`` resumes when today's session is already
       alive, so this never double-spawns. Outside active hours we don't spawn —
       the dawn cron owns that.

    Returns False (no-op) when circadian is disabled."""
    if not config.enabled:
        return False
    register_triggers(daemon, platform, config)
    anchors = load_rhythm()
    register_rhythm_anchors(daemon, config, anchors)
    await recover_on_startup(platform, config, evaluator=daemon.evaluator)
    if is_in_active_hours(datetime.now(ZoneInfo(config.timezone)), config):
        logger.info("[circadian] startup is within active hours — ensuring today's session")
        await ensure_today_session(
            datetime.now(timezone.utc), platform, config, evaluator=daemon.evaluator
        )
    return True
