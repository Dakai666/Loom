"""
TriggerHistory — persists trigger fire times across daemon restarts.

Backed by the shared SQLite store (trigger_history table).
Injected into TriggerEvaluator so that cron deduplication survives restarts.
"""

from __future__ import annotations

from datetime import datetime


class TriggerHistory:
    """
    Read/write last-fire timestamps for triggers.

    Args:
        db: an open ``aiosqlite.Connection`` (already initialized with schema).
    """

    def __init__(self, db) -> None:
        self._db = db

    async def get_all(self) -> list[dict]:
        """Return all rows as a list of dicts with trigger_name / last_fire_iso / fire_count."""
        async with self._db.execute(
            "SELECT trigger_name, last_fire_iso, fire_count FROM trigger_history"
        ) as cur:
            rows = await cur.fetchall()
        return [
            {"trigger_name": r[0], "last_fire_iso": r[1], "fire_count": r[2]}
            for r in rows
        ]

    async def get_last_fire(self, trigger_name: str) -> datetime | None:
        """Return the last fire time for a trigger, or None if never fired."""
        async with self._db.execute(
            "SELECT last_fire_iso FROM trigger_history WHERE trigger_name = ?",
            (trigger_name,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        try:
            return datetime.fromisoformat(row[0])
        except ValueError:
            return None

    async def record_fire(self, trigger_name: str, fired_at: datetime) -> None:
        """Upsert the fire record for a trigger."""
        await self._db.execute(
            """
            INSERT INTO trigger_history (trigger_name, last_fire_iso, fire_count)
            VALUES (?, ?, 1)
            ON CONFLICT(trigger_name) DO UPDATE SET
                last_fire_iso = excluded.last_fire_iso,
                fire_count    = fire_count + 1
            """,
            (trigger_name, fired_at.isoformat()),
        )
        await self._db.commit()
