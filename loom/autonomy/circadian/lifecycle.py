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
from datetime import datetime, timezone
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from loom.autonomy.circadian.state import CircadianState, _today_str, state_lock
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


async def setup_circadian(
    daemon: Any, platform: CircadianPlatform, config: CircadianConfig
) -> bool:
    """One-shot wiring called from the bot+daemon assembly point *after the
    Discord client is ready* (so adapter calls reach a live connection).

    Three startup steps (doc/57 §5, §9 restart-verification block):

    1. Register the dawn/close cron triggers.
    2. Recover leftover state (resume same-day, force-close stale yesterday).
    3. Catch-up spawn: if we came up during active hours, run the idempotent
       ``ensure_today_session`` so a daemon/bot that started after dawn still
       joins today's rhythm. ``ensure`` resumes when today's session is already
       alive, so this never double-spawns. Outside active hours we don't spawn —
       the dawn cron owns that.

    Returns False (no-op) when circadian is disabled."""
    if not config.enabled:
        return False
    register_triggers(daemon, platform, config)
    await recover_on_startup(platform, config, evaluator=daemon.evaluator)
    if is_in_active_hours(datetime.now(ZoneInfo(config.timezone)), config):
        logger.info("[circadian] startup is within active hours — ensuring today's session")
        await ensure_today_session(
            datetime.now(timezone.utc), platform, config, evaluator=daemon.evaluator
        )
    return True
