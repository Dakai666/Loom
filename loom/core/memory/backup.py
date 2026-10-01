"""
Pre-write snapshots of the memory DB (#603 step B).

Autonomous passes that rewrite memory (the weekly convergent dream with
``execute=true``) must not run without a fresh backup. :func:`snapshot_before_write`
takes an online SQLite backup — consistent under WAL with concurrent writers —
into ``dest_dir``, keeps the newest ``keep`` snapshots for its ``prefix``, and
returns ``None`` instead of raising on failure so callers can fall back to a
read-only pass ("no backup → no write").
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)

DEFAULT_BACKUP_DIR = Path.home() / ".loom" / "backups"


async def snapshot_before_write(
    db: aiosqlite.Connection,
    *,
    dest_dir: Path = DEFAULT_BACKUP_DIR,
    prefix: str,
    keep: int = 3,
) -> Path | None:
    """Back up *db* to ``dest_dir/<prefix>-<UTC timestamp>.db``.

    Returns the snapshot path, or ``None`` if any step failed (logged). The copy
    is written to a ``.tmp`` file and renamed into place, so a failure never
    leaves a half-written snapshot that pruning could mistake for a good one.
    Only files matching ``<prefix>-*.db`` are ever pruned.
    """
    tmp: Path | None = None
    try:
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        final = dest_dir / f"{prefix}-{stamp}.db"
        tmp = final.with_suffix(".db.tmp")
        async with aiosqlite.connect(tmp) as target:
            await db.backup(target)
        tmp.replace(final)
        tmp = None
    except Exception as exc:
        logger.warning("[memory-backup] snapshot failed (%s): %s", prefix, exc)
        if tmp is not None:
            tmp.unlink(missing_ok=True)
        return None

    snapshots = sorted(
        dest_dir.glob(f"{prefix}-*.db"), key=lambda p: p.stat().st_mtime, reverse=True,
    )
    for old in snapshots[max(1, keep):]:
        try:
            old.unlink()
        except OSError as exc:  # pruning is housekeeping — never fail the snapshot
            logger.warning("[memory-backup] could not prune %s: %s", old, exc)
    logger.info("[memory-backup] snapshot written: %s", final)
    return final
