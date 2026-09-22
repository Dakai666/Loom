"""
Room — the circadian layer where the world, not the clock, wakes the agent.

Spec: ``outputs/doc/circadian_room/spec_v0.1.md``. The daily bookends (dawn /
nightly close) and the rhythm anchors stay clock-driven; the room adds wakes
that come from *furniture*: assets with persistent state the agent owns or
lives with (喵吉, later the window, the calendar …).

Shape of one tick (every few minutes, a deterministic direct handler):

    re-decide queue ─┐
    furniture peek → signals → dedupe → wake matrix → one chime (or queue)

Contracts worth knowing before editing:

- **Furniture is a command.** Each ``[[furniture]]`` in
  ``autonomy/circadian/room.toml`` names a command that prints
  ``{"signals": [{"id", "key", "text"}], "next_signal_at": iso | null}``.
  The command must be read-only with respect to its own state — 喵吉's
  ``peek`` replays a *copy*. The core never imports skill code.
- **The furniture decides what is a signal; the room decides whether it
  wakes.** Priority comes from ``room.toml`` (per-furniture mapping +
  default), so the agent can retune it without touching either side.
- **A signal is first seen once, but decided every tick.** Dedupe stops a
  re-emitted key from being *added* twice; it does not freeze the decision.
  Everything queued is re-run through the wake matrix each tick, so a signal
  held back by sleep, the gap, or a failed delivery goes out as soon as it
  may (PR #591 review P0: "queued" must not mean "demoted to ambient").
- **The chime only describes the world.** Signal text is passed through
  verbatim; the room never writes the agent's first person (spec §1 red
  line 5), and silence is always a legal answer.
- **Nothing is dropped silently.** Queued entries leave only by being
  delivered or by age (``queue_ttl_hours``), and both are logged.
- **Every decision is logged raw** to ``~/.loom/circadian/log/room-*.jsonl``
  (peeks included, so a quiet furniture is visibly alive) and emitted as
  ``circadian:room_signal`` for in-process subscribers.

Awake means: inside active hours, today's daily session not closed, and —
when the rhythm table has a ``dawn`` anchor — dawn already delivered today.
The thread opens at ``start`` but she wakes at dawn; the room doesn't speak
first. ``RoomState.activity`` is the override slot a later ``room`` tool
will write (``focused`` / ``tending``); until then an awake agent is ``free``.

Known gaps (spec §5.5): the chime doesn't carry the current activity or
today's Program yet — Program has no structured source until §6 lands.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import tempfile
import tomllib
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from loom.autonomy.chime import ChimeRequest
from loom.autonomy.circadian.lifecycle import is_in_active_hours
from loom.autonomy.circadian.rhythm import load_rhythm
from loom.autonomy.circadian.state import CircadianState, _dir, _log_dir
from loom.autonomy.triggers import CronTrigger

logger = logging.getLogger(__name__)

DEFAULT_ROOM_PATH = Path("autonomy/circadian/room.toml")
TICK_TRIGGER = "circadian:room_tick"
CHIME_NAME = "circadian:room"
SIGNAL_EVENT = "circadian:room_signal"
TICK_CRON = "*/5 * * * *"
RUN_TIMEOUT_S = 30.0

# Bounds. A furniture is local code, but one bug must not flood a session.
MAX_OUTPUT_BYTES = 64 * 1024        # furniture stdout; larger ⇒ ignored
MAX_SIGNALS_PER_PEEK = 20           # extra signals in one peek are dropped (logged)
MAX_CHIME_LINES = 10                # the rest are counted, not listed
NEXT_CHECK_MAX = timedelta(hours=12)  # a furniture is re-peeked at least this often
# Dedupe memory, least-recently-seen evicted first. A key still being
# re-emitted is refreshed on every sighting, so only keys gone quiet age out.
SEEN_CAP = 500

PRIORITIES = ("urgent", "normal", "ambient")
ACTIVITIES = ("sleeping", "free", "focused", "tending")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Furniture:
    name: str
    label: str
    command: tuple[str, ...]
    priorities: dict[str, str] = field(default_factory=dict)
    default_priority: str = "normal"
    allowed_tools: tuple[str, ...] = ()

    def priority_for(self, signal_id: str) -> str:
        return self.priorities.get(signal_id, self.default_priority)


@dataclass(frozen=True)
class RoomConfig:
    enabled: bool = False
    daily_budget: int = 6
    min_gap_minutes: int = 45
    queue_ttl_hours: int = 12
    furniture: tuple[Furniture, ...] = ()


def _is_str_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def _parse_furniture(entry: dict[str, Any], idx: int) -> Furniture | None:
    name = entry.get("name")
    command = entry.get("command")
    if not isinstance(name, str) or not name:
        logger.warning("[room] furniture[%d] has no name; skipped", idx)
        return None
    if not (_is_str_list(command) and command):
        logger.warning("[room] furniture %r has no valid command; skipped", name)
        return None
    default = entry.get("default_priority", "normal")
    raw_prios = entry.get("priorities") or {}
    if default not in PRIORITIES or not isinstance(raw_prios, dict) or any(
        v not in PRIORITIES for v in raw_prios.values()
    ):
        logger.warning("[room] furniture %r has an unknown priority; skipped", name)
        return None
    tools = entry.get("allowed_tools", [])
    if not _is_str_list(tools):
        logger.warning("[room] furniture %r allowed_tools must be a list; skipped", name)
        return None
    return Furniture(
        name=name,
        label=str(entry.get("label", name)),
        command=tuple(command),
        priorities={str(k): v for k, v in raw_prios.items()},
        default_priority=default,
        allowed_tools=tuple(tools),
    )


def load_room(path: Path | None = None) -> RoomConfig:
    """Read ``room.toml``. Tolerant by contract (same as the rhythm table): a
    missing or broken file means "no room", never an exception; one bad
    furniture is dropped without silencing the rest. Duplicate furniture
    names keep the first — they would share dedupe and schedule state."""
    p = path or DEFAULT_ROOM_PATH
    if not p.exists():
        return RoomConfig()
    try:
        raw = tomllib.loads(p.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        logger.warning("[room] %s unreadable (%s); room disabled", p, exc)
        return RoomConfig()
    head = raw.get("room", {})
    entries = raw.get("furniture", [])
    if not isinstance(head, dict) or not isinstance(entries, list):
        logger.warning("[room] %s: [room] must be a table, [[furniture]] an array; room disabled", p)
        return RoomConfig()
    enabled = head.get("enabled", False)
    if not isinstance(enabled, bool):
        logger.warning("[room] %s: enabled must be true/false; room disabled", p)
        return RoomConfig()

    furniture: list[Furniture] = []
    for i, e in enumerate(entries):
        f = _parse_furniture(e, i) if isinstance(e, dict) else None
        if f is None:
            continue
        if any(x.name == f.name for x in furniture):
            logger.warning("[room] duplicate furniture %r; keeping the first", f.name)
            continue
        furniture.append(f)
    try:
        return RoomConfig(
            enabled=enabled,
            daily_budget=int(head.get("daily_budget", 6)),
            min_gap_minutes=int(head.get("min_gap_minutes", 45)),
            queue_ttl_hours=int(head.get("queue_ttl_hours", 12)),
            furniture=tuple(furniture),
        )
    except (TypeError, ValueError) as exc:
        logger.warning("[room] %s has a bad [room] value (%s); room disabled", p, exc)
        return RoomConfig()


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Signal:
    source: str
    id: str
    key: str
    text: str
    priority: str

    @property
    def dedupe_key(self) -> str:
        return f"{self.source}:{self.id}:{self.key}"


def _parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def parse_furniture_output(
    furniture: Furniture, raw: str
) -> tuple[list[Signal], datetime | None]:
    """Parse a furniture command's stdout. Garbage → no signals; a malformed
    signal is dropped individually."""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        logger.warning("[room] %s printed non-JSON output", furniture.name)
        return [], None
    if not isinstance(data, dict):
        return [], None
    signals: list[Signal] = []
    items = data.get("signals")
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        sid, key = item.get("id"), item.get("key")
        if not (isinstance(sid, str) and sid and isinstance(key, str) and key):
            logger.warning("[room] %s emitted a signal without id/key; dropped", furniture.name)
            continue
        signals.append(Signal(
            source=furniture.name,
            id=sid,
            key=key,
            text=str(item.get("text", "")).strip(),
            priority=furniture.priority_for(sid),
        ))
    return signals, _parse_dt(data.get("next_signal_at"))


Runner = Callable[[Furniture, datetime], Awaitable[tuple[list[Signal], datetime | None]]]


async def run_furniture(
    furniture: Furniture, now: datetime
) -> tuple[list[Signal], datetime | None]:
    """Run one furniture command from the daemon's cwd (the workspace, same
    convention as ``rhythm.toml`` commands). Any failure — not found,
    non-zero exit, timeout, oversized output — yields no signals (logged)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *furniture.command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        logger.warning("[room] %s command failed to start: %s", furniture.name, exc)
        return [], None
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=RUN_TIMEOUT_S)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        logger.warning("[room] %s command timed out", furniture.name)
        return [], None
    if proc.returncode != 0:
        logger.warning(
            "[room] %s command exited %s: %s",
            furniture.name, proc.returncode, err.decode(errors="replace")[-300:],
        )
        return [], None
    if len(out) > MAX_OUTPUT_BYTES:
        logger.warning(
            "[room] %s printed %d bytes (limit %d); ignored",
            furniture.name, len(out), MAX_OUTPUT_BYTES,
        )
        return [], None
    return parse_furniture_output(furniture, out.decode(errors="replace"))


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def room_state_path() -> Path:
    return _dir() / "room.json"


@dataclass
class RoomState:
    date: str = ""
    activity: str | None = None
    """Declared activity override; ``None`` ⇒ ``free`` while awake."""
    wakes_today: int = 0
    last_wake_at: str | None = None
    queue: list[dict[str, Any]] = field(default_factory=list)
    seen: list[str] = field(default_factory=list)
    next_check: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls) -> "RoomState":
        """Read ``room.json``. Each field is type-checked and falls back to
        its default on its own, so one bad field (hand edit, a future writer)
        never kills every tick."""
        p = room_state_path()
        if not p.exists():
            return cls()
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            logger.warning("[room] room.json unreadable (%s); starting fresh", exc)
            return cls()
        if not isinstance(raw, dict):
            return cls()

        def opt_str(v: Any) -> str | None:
            return v if isinstance(v, str) else None

        wakes = raw.get("wakes_today")
        queue, seen, nxt = raw.get("queue"), raw.get("seen"), raw.get("next_check")
        return cls(
            date=raw.get("date") if isinstance(raw.get("date"), str) else "",
            activity=opt_str(raw.get("activity")),
            wakes_today=wakes if isinstance(wakes, int) and not isinstance(wakes, bool) else 0,
            last_wake_at=opt_str(raw.get("last_wake_at")),
            queue=[q for q in queue if isinstance(q, dict)] if isinstance(queue, list) else [],
            seen=[s for s in seen if isinstance(s, str)] if isinstance(seen, list) else [],
            next_check=(
                {k: v for k, v in nxt.items() if isinstance(v, str)}
                if isinstance(nxt, dict) else {}
            ),
        )

    def save(self) -> None:
        p = room_state_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".room-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(asdict(self), fh, ensure_ascii=False, indent=2)
            os.replace(tmp, p)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------

def _awake(now: datetime, circadian: Any) -> bool:
    local = now.astimezone(ZoneInfo(circadian.timezone))
    if not is_in_active_hours(local, circadian):
        return False
    day = CircadianState.load()
    if day is None or day.date != local.strftime("%Y-%m-%d"):
        # No session today: a wake can't be delivered anyway and stays queued.
        return True
    if day.closed_at is not None:
        return False
    if any(a.name == "dawn" for a in load_rhythm()):
        return any(
            e.get("phase") == "dawn" and e.get("outcome") == "delivered"
            for e in day.phase_log
        )
    return True


def current_activity(state: RoomState, now: datetime, circadian: Any) -> str:
    if not _awake(now, circadian):
        return "sleeping"
    if state.activity in ACTIVITIES and state.activity != "sleeping":
        return state.activity
    return "free"


def decide(
    priority: str, activity: str, state: RoomState, config: RoomConfig, now: datetime
) -> str:
    """The wake matrix (spec §5.1). Returns ``"wake"`` or ``"queue"``.

    urgent  → wakes in every waking activity; bypasses budget and gap
    normal  → wakes only in ``free``, within budget and min gap
    ambient → never wakes on its own
    """
    if activity == "sleeping" or priority not in ("urgent", "normal"):
        return "queue"
    if priority == "urgent":
        return "wake"
    if activity != "free":
        return "queue"
    if state.wakes_today >= config.daily_budget:
        return "queue"
    last = _parse_dt(state.last_wake_at)
    if last is not None and now - last < timedelta(minutes=config.min_gap_minutes):
        return "queue"
    return "wake"


# ---------------------------------------------------------------------------
# Chime composition
# ---------------------------------------------------------------------------

def compose_intent(
    woken: list[dict[str, Any]], merged: list[dict[str, Any]], labels: dict[str, str]
) -> str:
    """World description only: furniture text, verbatim, under its label.
    At most ``MAX_CHIME_LINES`` lines; the rest are counted (and logged)."""

    def line(s: dict[str, Any]) -> str:
        label = labels.get(s.get("source", ""), s.get("source", ""))
        return f"- {label}：{s.get('text') or s.get('id', '')}"

    shown_woken = woken[:MAX_CHIME_LINES]
    shown_merged = merged[:max(0, MAX_CHIME_LINES - len(shown_woken))]
    hidden = len(woken) + len(merged) - len(shown_woken) - len(shown_merged)

    parts = ["**房間**", *(line(s) for s in shown_woken)]
    if shown_merged:
        parts += ["", "**稍早（排隊中）**", *(line(s) for s in shown_merged)]
    if hidden:
        parts += ["", f"（還有 {hidden} 則，見房間 log）"]
    parts += ["", "這是房間裡發生的事，不是任務；不回應也可以。"]
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Log
# ---------------------------------------------------------------------------

def _log(event: str, now: datetime, tz: str, **fields: Any) -> None:
    try:
        d = _log_dir()
        d.mkdir(parents=True, exist_ok=True)
        day = now.astimezone(ZoneInfo(tz)).strftime("%Y-%m-%d")
        rec = {"ts": now.isoformat(), "event": event, **fields}
        with open(d / f"room-{day}.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        logger.exception("[room] log write failed")


# ---------------------------------------------------------------------------
# Tick
# ---------------------------------------------------------------------------

# In-process only: the daemon is room.json's single writer today. A second
# writer (the planned ``room`` tool, a CLI) must also take ``state_lock()``.
_tick_lock: asyncio.Lock | None = None


def _get_lock() -> asyncio.Lock:
    global _tick_lock
    if _tick_lock is None:
        _tick_lock = asyncio.Lock()
    return _tick_lock


def _touch_seen(state: RoomState, key: str) -> bool:
    """Record a sighting (LRU). Returns True when the key is new."""
    if key in state.seen:
        state.seen.remove(key)
        state.seen.append(key)
        return False
    state.seen.append(key)
    return True


async def room_tick(
    daemon: Any,
    circadian: Any,
    now: datetime,
    *,
    config: RoomConfig | None = None,
    runner: Runner = run_furniture,
) -> None:
    """One pass: re-decide the queue, peek due furniture, deliver at most
    one chime, then emit each new signal for subscribers."""
    cfg = config if config is not None else load_room()
    if not cfg.enabled or not cfg.furniture:
        return
    tz = circadian.timezone
    emits: list[dict[str, Any]] = []
    async with _get_lock():
        state = RoomState.load()
        today = now.astimezone(ZoneInfo(tz)).strftime("%Y-%m-%d")
        if state.date != today:
            state.date, state.wakes_today = today, 0

        # Age out queued entries (logged, never silent). No readable time ⇒
        # no way to age it, so it goes now rather than never.
        cutoff = now - timedelta(hours=cfg.queue_ttl_hours)
        kept = []
        for q in state.queue:
            ts = _parse_dt(q.get("at"))
            if ts is None or ts < cutoff:
                _log("expired", now, tz, source=q.get("source"), id=q.get("id"), key=q.get("key"))
            else:
                kept.append(q)

        activity = current_activity(state, now, circadian)

        # Everything still queued is decided again under today's conditions.
        woken: list[dict[str, Any]] = []
        state.queue = []
        for q in kept:
            if decide(q.get("priority", "ambient"), activity, state, cfg, now) == "wake":
                woken.append(q)
            else:
                state.queue.append(q)
        if woken:
            _log("redecide", now, tz, activity=activity, woken=[q.get("id") for q in woken])

        for f in cfg.furniture:
            due = _parse_dt(state.next_check.get(f.name))
            if due is not None and now < due:
                continue
            try:
                signals, nxt = await runner(f, now)
            except Exception:  # noqa: BLE001 — one furniture must not stop the room
                logger.exception("[room] furniture %s failed", f.name)
                _log("furniture_error", now, tz, source=f.name)
                continue
            if nxt is not None and nxt > now:
                nxt = min(nxt, now + NEXT_CHECK_MAX)
                state.next_check[f.name] = nxt.isoformat()
            else:
                state.next_check.pop(f.name, None)
            dropped = len(signals) - MAX_SIGNALS_PER_PEEK
            signals = signals[:MAX_SIGNALS_PER_PEEK]
            _log("peek", now, tz, source=f.name, count=len(signals),
                 dropped=max(0, dropped), next_check=state.next_check.get(f.name))
            for s in signals:
                if not _touch_seen(state, s.dedupe_key):
                    continue
                decision = decide(s.priority, activity, state, cfg, now)
                _log("signal", now, tz, source=s.source, id=s.id, key=s.key,
                     priority=s.priority, activity=activity, decision=decision)
                emits.append({"source": s.source, "id": s.id, "key": s.key,
                              "priority": s.priority, "activity": activity,
                              "decision": decision})
                entry = {**asdict(s), "at": now.isoformat()}
                (woken if decision == "wake" else state.queue).append(entry)
        state.seen = state.seen[-SEEN_CAP:]

        if woken:
            labels = {f.name: f.label for f in cfg.furniture}
            merged = list(state.queue)
            sources = {s.get("source") for s in woken} | {s.get("source") for s in merged}
            tools = tuple(dict.fromkeys(
                t for f in cfg.furniture if f.name in sources for t in f.allowed_tools
            ))
            # Unique per wake: the bot dedupes pending chimes by name
            # (latest wins), which would silently swallow an earlier room
            # wake still waiting behind a user turn.
            stamp = now.astimezone(ZoneInfo(tz)).strftime("%H%M")
            req = ChimeRequest(
                schedule_name=f"{CHIME_NAME}@{stamp}",
                intent=compose_intent(woken, merged, labels),
                fired_at=now,
                target={"type": "circadian_today", "fallback": "skip"},
                allowed_tools=tools,
            )
            try:
                delivered = bool(await daemon.deliver_chime(req))
            except Exception:  # noqa: BLE001
                logger.exception("[room] chime delivery raised")
                delivered = False
            _log("wake", now, tz, delivered=delivered,
                 woken=[s.get("id") for s in woken], merged=[s.get("id") for s in merged])
            if delivered:
                # Urgent wakes don't spend the normal budget, but they do
                # reset the gap: a normal right after one would be noise.
                if any(s.get("priority") != "urgent" for s in woken):
                    state.wakes_today += 1
                state.last_wake_at = now.isoformat()
                state.queue = []
            else:
                state.queue = woken + state.queue

        state.save()

    # Outside the lock, like lifecycle's _emit: a subscriber's run can't
    # stall the room.
    emit = getattr(getattr(daemon, "evaluator", None), "emit", None)
    for ctx in emits if emit is not None else []:
        try:
            await emit(SIGNAL_EVENT, ctx)
        except Exception:  # noqa: BLE001 — a bad subscriber must not break the room
            logger.exception("[room] emit %s failed", SIGNAL_EVENT)


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------

def register_room(daemon: Any, circadian: Any, path: Path | None = None) -> bool:
    """Register the room tick when ``room.toml`` exists and is enabled. The
    config is re-read every tick, so edits apply without a restart."""
    cfg = load_room(path)
    if not cfg.enabled:
        return False

    async def _tick_handler(_trigger: Any, _context: dict[str, Any]) -> None:
        await room_tick(daemon, circadian, datetime.now(timezone.utc))

    daemon.evaluator.register(CronTrigger(
        name=TICK_TRIGGER,
        intent="Circadian room tick — furniture signals (deterministic)",
        cron=TICK_CRON,
        timezone="UTC",
        notify=False,
    ))
    daemon.register_direct_handler(TICK_TRIGGER, _tick_handler)
    logger.info(
        "[room] registered tick %s with furniture: %s",
        TICK_CRON, ", ".join(f.name for f in cfg.furniture) or "none",
    )
    return True
