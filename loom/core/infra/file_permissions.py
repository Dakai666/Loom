"""POSIX file-permission baseline for the Loom store directory.

#342 — minimal at-rest hardening:

- ``~/.loom/`` is tightened to ``0o700`` (owner-only traversal) so other
  users on the same host can't enumerate session DB / blob files.
- ``memory.db`` / ``ledger.db`` (plus their SQLite WAL / SHM siblings)
  are tightened to ``0o600`` (owner-only read/write).
- A single warning fires per process when files are found with looser
  permissions; subsequent encounters stay silent so resume / Discord
  reconnect don't re-spam the log.

Windows / non-POSIX: every helper is a no-op. Read/write perms on those
platforms are governed by ACLs, which the SQLite open path doesn't
expose; we leave that to follow-up work if there's a real demand signal.

This is a baseline, not full at-rest encryption. Cloud-sync leaks
(iCloud / Dropbox auto-include) and lost-laptop full-disk reads still
expose plaintext content. Stronger options (SQLCipher, retention
pruning) tracked in #342.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


_LOOSE_FILE_MASK = 0o077  # any group / other bit set → loose
_TARGET_FILE_MODE = 0o600
_TARGET_DIR_MODE = 0o700

# Module-scoped flag so a single process logs at most one "tightened
# loose perms" warning. Subsequent calls during the same process stay
# quiet — the user's already been told once.
_warned_loose: bool = False


def _is_posix() -> bool:
    return os.name == "posix"


def tighten_file(path: Path) -> None:
    """chmod ``path`` to 0o600 on POSIX. Missing files / non-POSIX are no-ops.

    Logs at most one process-wide warning when an existing file was
    found with looser permissions; the chmod itself is silent.
    """
    if not _is_posix():
        return
    try:
        st = path.stat()
    except FileNotFoundError:
        return
    except OSError as exc:
        logger.debug("file_permissions: stat(%s) failed: %s", path, exc)
        return

    was_loose = (st.st_mode & _LOOSE_FILE_MASK) != 0
    if not was_loose and (st.st_mode & 0o777) == _TARGET_FILE_MODE:
        return

    try:
        os.chmod(path, _TARGET_FILE_MODE)
    except OSError as exc:
        logger.debug("file_permissions: chmod(%s) failed: %s", path, exc)
        return

    if was_loose:
        _warn_loose_once(path)


def tighten_dir(path: Path) -> None:
    """chmod ``path`` to 0o700 on POSIX. Missing dirs / non-POSIX are no-ops."""
    if not _is_posix():
        return
    try:
        st = path.stat()
    except FileNotFoundError:
        return
    except OSError as exc:
        logger.debug("file_permissions: stat(%s) failed: %s", path, exc)
        return

    was_loose = (st.st_mode & _LOOSE_FILE_MASK) != 0
    if not was_loose and (st.st_mode & 0o777) == _TARGET_DIR_MODE:
        return

    try:
        os.chmod(path, _TARGET_DIR_MODE)
    except OSError as exc:
        logger.debug("file_permissions: chmod(%s) failed: %s", path, exc)
        return

    if was_loose:
        _warn_loose_once(path)


def tighten_sqlite_db(path: Path) -> None:
    """Tighten a SQLite DB file plus its WAL / SHM / rollback-journal siblings.

    SQLite in WAL mode keeps two side files (``*-wal``, ``*-shm``) that
    contain plaintext content too; rollback-journal mode adds
    ``*-journal``. All three are tightened if present so an attacker
    can't read state by reaching for the side file.
    """
    tighten_file(path)
    base = str(path)
    for suffix in ("-wal", "-shm", "-journal"):
        tighten_file(Path(base + suffix))


def _warn_loose_once(path: Path) -> None:
    global _warned_loose
    if _warned_loose:
        return
    _warned_loose = True
    logger.warning(
        "Tightened loose permissions on %s. Loom stores raw session "
        "content and secrets here; future sessions will keep this path "
        "owner-only. (#342)",
        path,
    )


def reset_warning_state_for_tests() -> None:
    """Test hook — reset the once-per-process warning flag."""
    global _warned_loose
    _warned_loose = False
