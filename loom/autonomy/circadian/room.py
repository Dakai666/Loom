"""
Room — the circadian layer where the world, not the clock, wakes the agent.

Spec: ``outputs/doc/circadian_room/spec_v0.1.md``. The daily bookends (dawn /
nightly close) and the rhythm anchors stay clock-driven; the room adds wakes
that come from *furniture*: assets with persistent state the agent owns or
lives with (喵吉, later the window, the calendar …).

Shape of one tick (every few minutes, a deterministic direct handler):

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
- **The chime only describes the world.** Signal text is passed through
  verbatim; the room never writes the agent's first person (spec §1 red
  line 5), and silence is always a legal answer.
- **Nothing is dropped silently.** A signal that doesn't wake is queued and
  rides along with the next wake; an undelivered wake stays queued; queued
  entries expire only by age (``queue_ttl_hours``) and the expiry is logged.
- **Every decision is logged raw** to ``~/.loom/circadian/log/room-*.jsonl``
  so DK can read what happened without an agent summary.

Activity (``sleeping`` / ``free`` / ``focused`` / ``tending``) is derived from
active hours for now; ``RoomState.activity`` is the override slot a later
``room`` tool will write when the agent declares what she's doing.
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
from loom.autonomy.circadian.state import _dir, _log_dir
from loom.autonomy.triggers import CronTrigger

logger = logging.getLogger(__name__)

DEFAULT_ROOM_PATH = Path("autonomy/circadian/room.toml")
TICK_TRIGGER = "circadian:room_tick"
CHIME_NAME = "circadian:room"
TICK_CRON = "*/5 * * * *"
RUN_TIMEOUT_S = 30.0
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


def _parse_furniture(entry: dict[str, Any], idx: int) -> Furniture | None:
    name = entry.get("name")
    command = entry.get("command")
    if not isinstance(name, str) or not name:
        logger.warning("[room] furniture[%d] has no name; skipped", idx)
        return None
    if not (isinstance(command, list) and command and all(isinstance(c, str) for c in command)):
        logger.warning("[room] furniture %r has no valid command; skipped", name)
        return None
    default = entry.get("default_priority", "normal")
    raw_prios = entry.get("priorities") or {}
    if default not in PRIORITIES or not isinstance(raw_prios, dict) or any(
        v not in PRIORITIES for v in raw_prios.values()
    ):
        logger.warning("[room] furniture %r has an unknown priority; skipped", name)
        return None
    tools = entry.get("allowed_tools") or []
    return Furniture(
        name=name,
        label=str(entry.get("label", name)),
        command=tuple(command),
        priorities={str(k): v for k, v in raw_prios.items()},
        default_priority=default,
        allowed_tools=tuple(str(t) for t in tools),
    )


def load_room(path: Path | None = None) -> RoomConfig:
    """Read ``room.toml``. Tolerant by contract (same as the rhythm table): a
    missing or broken file means "no room", never an exception; one bad
    furniture is dropped without silencing the rest."""
    p = path or DEFAULT_ROOM_PATH
    if not p.exists():
        return RoomConfig()
    try:
        raw = tomllib.loads(p.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        logger.warning("[room] %s unreadable (%s); room disabled", p, exc)
        return RoomConfig()
    head = raw.get("room") or {}
    furniture = tuple(
        f for i, e in enumerate(raw.get("furniture") or [])
        if isinstance(e, dict) and (f := _parse_furniture(e, i)) is not None
    )
    try:
        return RoomConfig(
            enabled=bool(head.get("enabled", False)),
            daily_budget=int(head.get("daily_budget", 6)),
            min_gap_minutes=int(head.get("min_gap_minutes", 45)),
            queue_ttl_hours=int(head.get("queue_ttl_hours", 12)),
            furniture=furniture,
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
    for item in data.get("signals") or []:
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
    """Run one furniture command from the workspace root. Any failure — not
    found, non-zero exit, timeout — yields no signals (logged)."""
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
    """Declared activity override; ``None`` ⇒ derived from active hours."""
    wakes_today: int = 0
    last_wake_at: str | None = None
    queue: list[dict[str, Any]] = field(default_factory=list)
    seen: list[str] = field(default_factory=list)
    next_check: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls) -> "RoomState":
        p = room_state_path()
        if not p.exists():
            return cls()
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            known = {k: raw[k] for k in cls.__dataclass_fields__ if k in raw}
            return cls(**known)
        except (OSError, json.JSONDecodeError, TypeError) as exc:
            logger.warning("[room] room.json unreadable (%s); starting fresh", exc)
            return cls()

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

def current_activity(state: RoomState, now: datetime, circadian: Any) -> str:
    from loom.autonomy.circadian.lifecycle import is_in_active_hours

    if not is_in_active_hours(now.astimezone(ZoneInfo(circadian.timezone)), circadian):
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
    if activity == "sleeping" or priority == "ambient":
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
    """World description only: furniture text, verbatim, under its label."""

    def line(s: dict[str, Any]) -> str:
        label = labels.get(s["source"], s["source"])
        return f"- {label}：{s.get('text') or s['id']}"

    parts = ["**房間**", *(line(s) for s in woken)]
    if merged:
        parts += ["", "**稍早（排隊中）**", *(line(s) for s in merged)]
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

_tick_lock: asyncio.Lock | None = None


def _get_lock() -> asyncio.Lock:
    global _tick_lock
    if _tick_lock is None:
        _tick_lock = asyncio.Lock()
    return _tick_lock


async def room_tick(
    daemon: Any,
    circadian: Any,
    now: datetime,
    *,
    config: RoomConfig | None = None,
    runner: Runner = run_furniture,
) -> None:
    """One pass: peek due furniture, decide, deliver at most one chime."""
    cfg = config if config is not None else load_room()
    if not cfg.enabled or not cfg.furniture:
        return
    tz = circadian.timezone
    async with _get_lock():
        state = RoomState.load()
        today = now.astimezone(ZoneInfo(tz)).strftime("%Y-%m-%d")
        if state.date != today:
            state.date, state.wakes_today = today, 0

        # Age out queued entries (logged, never silent).
        cutoff = now - timedelta(hours=cfg.queue_ttl_hours)
        kept = []
        for q in state.queue:
            ts = _parse_dt(q.get("at"))
            if ts is not None and ts < cutoff:
                _log("expired", now, tz, source=q.get("source"), id=q.get("id"), key=q.get("key"))
            else:
                kept.append(q)
        state.queue = kept

        activity = current_activity(state, now, circadian)
        seen = set(state.seen)
        woken: list[dict[str, Any]] = []
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
                state.next_check[f.name] = nxt.isoformat()
            else:
                state.next_check.pop(f.name, None)
            for s in signals:
                if s.dedupe_key in seen:
                    continue
                seen.add(s.dedupe_key)
                state.seen.append(s.dedupe_key)
                decision = decide(s.priority, activity, state, cfg, now)
                _log("signal", now, tz, source=s.source, id=s.id, key=s.key,
                     priority=s.priority, activity=activity, decision=decision)
                entry = {**asdict(s), "at": now.isoformat()}
                (woken if decision == "wake" else state.queue).append(entry)
        state.seen = state.seen[-SEEN_CAP:]

        if woken:
            labels = {f.name: f.label for f in cfg.furniture}
            merged = list(state.queue)
            sources = {s["source"] for s in woken} | {s["source"] for s in merged}
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
                 woken=[s["id"] for s in woken], merged=[s["id"] for s in merged])
            if delivered:
                state.wakes_today += 1
                state.last_wake_at = now.isoformat()
                state.queue = []
            else:
                state.queue.extend(woken)

        state.save()


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
