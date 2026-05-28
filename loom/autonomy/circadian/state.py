"""
CircadianState — the single source of truth for "which daily session is today".

The daily Discord thread id changes every day and must survive daemon / bot
restarts, so it can't be hard-coded into ``loom.toml``. This module owns the
``~/.loom/circadian/state.json`` file that records today's thread, plus the
access discipline (atomic writes, corruption quarantine, cross-day detection)
spelled out in doc/57 §4.

Platform-internal, sibling to ``~/.loom/discord_threads.json`` — gitignored,
never committed. Reads are lock-free; writes go through ``state_lock()`` so a
daemon and a bot racing to spawn today's session can't corrupt the file.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

# Current on-disk schema version. Bumps require an explicit migration —
# ``load()`` quarantines anything it doesn't recognise rather than guessing.
STATE_VERSION = 1

# Directory holding state + archive. Computed at call time (not import) so a
# test can redirect it away from the real home dir via ``set_dir_for_test``.
_DIR_OVERRIDE: Path | None = None


def set_dir_for_test(path: Path | None) -> None:
    """Redirect the circadian state dir (tests only). Pass ``None`` to reset."""
    global _DIR_OVERRIDE
    _DIR_OVERRIDE = path


def _dir() -> Path:
    if _DIR_OVERRIDE is not None:
        return _DIR_OVERRIDE
    return Path("~/.loom/circadian").expanduser()


def state_path() -> Path:
    """Absolute path of the state file (exposed for tests / introspection)."""
    return _dir() / "state.json"


def _lock_path() -> Path:
    return _dir() / "state.json.lock"


def _log_dir() -> Path:
    return _dir() / "log"


def _today_str(tz: str) -> str:
    """Today's date (YYYY-MM-DD) in the configured timezone.

    The date *string* — not a UTC instant — is the single source of truth for
    "is this state today's?", so NTP corrections and DST edges resolve the same
    way the daemon's wall-clock cron does (doc/57 §5, clock-skew row).
    """
    return datetime.now(ZoneInfo(tz)).strftime("%Y-%m-%d")


@contextlib.contextmanager
def state_lock(blocking: bool = True) -> Iterator[bool]:
    """Process-level advisory lock guarding writes to ``state.json``.

    Yields ``True`` when the lock is held, ``False`` when ``blocking=False``
    and another holder has it. Uses ``fcntl`` on a sidecar lockfile so two
    daemons (or a daemon + bot ``on_ready``) racing through
    ``ensure_today_session`` serialise: the first writes, the rest observe a
    held lock and bail. Degrades to a held no-op where ``fcntl`` is
    unavailable (non-POSIX); Loom's runtime is POSIX.
    """
    try:
        import fcntl
    except ImportError:  # pragma: no cover — non-POSIX fallback
        yield True
        return

    _dir().mkdir(parents=True, exist_ok=True)
    fd = os.open(_lock_path(), os.O_CREAT | os.O_RDWR, 0o644)
    acquired = False
    try:
        flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        try:
            fcntl.flock(fd, flags)
            acquired = True
        except OSError:
            acquired = False  # non-blocking miss — someone else holds it
        yield acquired
    finally:
        if acquired:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


@dataclass
class CircadianState:
    """One day's daily-session bookkeeping. See doc/57 §4.2 for the schema.

    ``timezone`` is stamped at spawn so any later reader (esp. the Discord
    bot's ``circadian_today`` resolver) can verify the state is *for today*
    without having to know the engine's config — the date string alone is
    ambiguous across midnight when the reader doesn't know which tz produced
    it. PR #479 review P1: without this check, a stale state from a missed
    nightly close routes today's phase chimes into yesterday's thread.
    """

    date: str
    thread_id: int
    session_id: str
    channel_id: int
    started_at: str
    timezone: str
    closed_at: str | None = None
    version: int = STATE_VERSION
    phase_log: list[dict[str, Any]] = field(default_factory=list)

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    def new_for_today(
        cls,
        *,
        thread_id: int,
        session_id: str,
        channel_id: int,
        now: datetime,
        tz: str,
    ) -> "CircadianState":
        """Build a fresh state for today's just-spawned session."""
        return cls(
            date=_today_str(tz),
            thread_id=thread_id,
            session_id=session_id,
            channel_id=channel_id,
            started_at=now.isoformat(),
            timezone=tz,
        )

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    @classmethod
    def load(cls) -> "CircadianState | None":
        """Read today's state, or ``None`` when absent / unreadable.

        A malformed or unknown-version file is quarantined to
        ``state.json.broken-<ts>`` and treated as "no state" so the spawner
        rebuilds from scratch (doc/57 §5, corruption row). Never raises.
        """
        sp = state_path()
        if not sp.exists():
            return None
        try:
            raw = json.loads(sp.read_text(encoding="utf-8"))
            if raw.get("version") != STATE_VERSION:
                raise ValueError(f"unknown version {raw.get('version')!r}")
            return cls(
                date=raw["date"],
                thread_id=int(raw["thread_id"]),
                session_id=str(raw["session_id"]),
                channel_id=int(raw["channel_id"]),
                started_at=raw["started_at"],
                timezone=str(raw["timezone"]),
                closed_at=raw.get("closed_at"),
                version=int(raw["version"]),
                phase_log=list(raw.get("phase_log", [])),
            )
        except Exception as exc:
            cls._quarantine(exc)
            return None

    @staticmethod
    def _quarantine(exc: Exception) -> None:
        """Rename a corrupt state file aside so the next spawn rebuilds."""
        sp = state_path()
        ts = datetime.now().strftime("%Y%m%dT%H%M%S")
        broken = sp.with_name(f"state.json.broken-{ts}")
        try:
            sp.replace(broken)
            logger.warning(
                "[circadian] state.json unreadable (%s); quarantined to %s",
                exc, broken.name,
            )
        except OSError as move_exc:  # pragma: no cover — best effort
            logger.warning(
                "[circadian] state.json unreadable (%s) and quarantine failed: %s",
                exc, move_exc,
            )

    def save_atomic(self) -> None:
        """Write state via tempfile + ``os.replace`` so readers never see a
        half-written file and a crash mid-write can't corrupt the live state.
        Callers must hold ``state_lock()`` (writers are rare; readers don't)."""
        d = _dir()
        d.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(asdict(self), ensure_ascii=False, indent=2)
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".state-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, state_path())
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    def archive_and_reset(self) -> None:
        """Move today's record to ``log/YYYY-MM-DD.json`` and clear the live
        state. Called at nightly close (and cross-day startup recovery) so
        ``state.json`` never accumulates more than one day (doc/57 §4.3)."""
        ld = _log_dir()
        ld.mkdir(parents=True, exist_ok=True)
        archive = ld / f"{self.date}.json"
        try:
            archive.write_text(
                json.dumps(asdict(self), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:  # pragma: no cover — best effort
            logger.warning("[circadian] archive of %s failed: %s", self.date, exc)
        with contextlib.suppress(FileNotFoundError):
            state_path().unlink()

    # ------------------------------------------------------------------
    # Queries / mutations
    # ------------------------------------------------------------------

    def is_for_today(self, tz: str) -> bool:
        """True when this state's date equals today's date in ``tz``."""
        return self.date == _today_str(tz)

    def append_phase(
        self, phase: str, outcome: str, reason: str | None = None
    ) -> None:
        """Record one phase-trigger outcome (delivered / skipped / spawned…).

        Mutates in place; the caller persists with ``save_atomic`` under lock.
        The log is the audit trail for the anti-heartbeat gate and the
        watchdog's missed-spawn counter (doc/57 §13.2)."""
        entry: dict[str, Any] = {
            "phase": phase,
            "fired_at": datetime.now().isoformat(),
            "outcome": outcome,
        }
        if reason is not None:
            entry["reason"] = reason
        self.phase_log.append(entry)
