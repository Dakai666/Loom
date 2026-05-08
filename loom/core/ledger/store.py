"""LedgerStore — append-only event sink backed by SQLite WAL.

Pure storage layer (Phase 2 Step 1 of doc/53). Zero coupling to emitters:
this module knows nothing about middleware / session / memory / scheduler.

See doc/53 §5 (storage), §3.3 (thought blob storage), §5.7 (compaction
chain walker), §11.2 (six-step roadmap).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

import aiosqlite

from loom.core.ledger.schema import (
    CREATE_EVENTS_TABLE,
    CREATE_INDEXES,
    DEFAULT_BRANCH,
    LEDGER_BLOB_SUBDIR,
    THOUGHT_EXTERNAL_THRESHOLD,
    LedgerEvent,
    ThoughtPayload,
)

DEFAULT_DB_PATH = Path.home() / ".loom" / "ledger.db"


def _payload_to_dict(payload: Any) -> dict[str, Any]:
    """Accept dataclass payload or dict; always return dict."""
    if is_dataclass(payload):
        return asdict(payload)
    if isinstance(payload, dict):
        return payload
    raise TypeError(f"payload must be dataclass or dict, got {type(payload).__name__}")


class LedgerStore:
    """Async, append-only ledger backed by SQLite WAL.

    Lifecycle:
        store = LedgerStore(db_path=...)
        await store.open()
        await store.emit(event)
        await store.close()

    Or as async context manager:
        async with LedgerStore() as store:
            await store.emit(event)
    """

    def __init__(
        self,
        db_path: Path | str | None = None,
        blob_dir: Path | str | None = None,
    ) -> None:
        self.db_path = Path(db_path) if db_path else DEFAULT_DB_PATH
        # Default blob_dir sits next to ledger.db: ~/.loom/ledger_blobs/
        self.blob_dir = (
            Path(blob_dir)
            if blob_dir
            else self.db_path.parent / LEDGER_BLOB_SUBDIR
        )
        self._conn: aiosqlite.Connection | None = None

    # -- lifecycle ---------------------------------------------------------

    async def open(self) -> None:
        if self._conn is not None:
            return
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.blob_dir.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.db_path)
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA synchronous=NORMAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.execute(CREATE_EVENTS_TABLE)
        for stmt in CREATE_INDEXES:
            await self._conn.execute(stmt)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def __aenter__(self) -> "LedgerStore":
        await self.open()
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.close()

    # -- emit --------------------------------------------------------------

    async def emit(self, event: LedgerEvent) -> None:
        """Append a single event. Caller owns event_id uniqueness."""
        conn = self._require_conn()
        payload = _payload_to_dict(event.payload)
        await conn.execute(
            """
            INSERT INTO events (
                event_id, session_id, turn_id, parent_event_id,
                correlation_id, branch_id, event_type, timestamp, payload
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.event_id,
                event.session_id,
                event.turn_id,
                event.parent_event_id,
                event.correlation_id,
                event.branch_id,
                event.event_type,
                event.timestamp,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
            ),
        )
        await conn.commit()

    async def update_thought_full_text(
        self, event_id: str, full_text: str, *, turn_id: str
    ) -> None:
        """Late-arrival commit of thought.full_text (§3.3 buffered capture).

        This is the only mutating operation allowed on a written event,
        endorsed by §2 principle 1 as an explicit exception.
        """
        conn = self._require_conn()
        async with conn.execute(
            "SELECT payload FROM events WHERE event_id=?", (event_id,)
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            raise KeyError(f"unknown event_id: {event_id}")
        payload = json.loads(row[0])
        full_text, external_ref, digest = await self.store_thought_text(
            full_text, turn_id=turn_id, event_id=event_id
        )
        payload["full_text"] = full_text
        payload["external_ref"] = external_ref
        payload["digest"] = digest
        await conn.execute(
            "UPDATE events SET payload=? WHERE event_id=?",
            (json.dumps(payload, ensure_ascii=False, sort_keys=True), event_id),
        )
        await conn.commit()

    # -- thought blob helper (§3.3) ---------------------------------------

    async def store_thought_text(
        self, raw_text: str, *, turn_id: str, event_id: str
    ) -> tuple[str | None, str | None, str]:
        """Return (inline_full_text, external_ref, digest).

        ≤ THOUGHT_EXTERNAL_THRESHOLD bytes → inline.
        > threshold → write to .loom/ledger_blobs/{turn_id}/{event_id}.txt
                      and return relative path as external_ref.

        Blob writes are dispatched via ``asyncio.to_thread`` so the event
        loop is not blocked. Today's threshold (50 KB) is small, but this
        helper may also serve future artifact paths where blobs grow.
        """
        encoded = raw_text.encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()
        if len(encoded) <= THOUGHT_EXTERNAL_THRESHOLD:
            return raw_text, None, digest
        rel_path = f"{turn_id}/{event_id}.txt"
        abs_path = self.blob_dir / rel_path

        def _write() -> None:
            abs_path.parent.mkdir(parents=True, exist_ok=True)
            abs_path.write_text(raw_text, encoding="utf-8")

        await asyncio.to_thread(_write)
        return None, rel_path, digest

    async def build_thought_payload(
        self,
        raw_text: str,
        *,
        turn_id: str,
        event_id: str,
        duration_ms: int,
        produced_tool_calls: int,
    ) -> ThoughtPayload:
        full_text, external_ref, digest = await self.store_thought_text(
            raw_text, turn_id=turn_id, event_id=event_id
        )
        return ThoughtPayload(
            digest=digest,
            duration_ms=duration_ms,
            produced_tool_calls=produced_tool_calls,
            full_text=full_text,
            external_ref=external_ref,
        )

    # -- compaction chain walker (§5.7) -----------------------------------

    async def resolve_memory_id(
        self, mem_id: str, *, branch_id: str = DEFAULT_BRANCH
    ) -> str:
        """Walk compaction chain forward to current live memory_id.

        Each compact event stores a single (predecessor → successor) edge.
        We walk forward until no further edge exists.

        Note:
            Assumes linear write order — when two compact events share a
            predecessor, the earlier ``timestamp`` wins. Safe under doc/53
            §2 principle 6 (solo operator, single-writer). If concurrent
            emit is ever introduced, ordering must be revisited.
        """
        conn = self._require_conn()
        current = mem_id
        seen: set[str] = set()
        while True:
            if current in seen:
                # Cycle defence — should never happen with append-only
                # writes, but bail rather than loop forever.
                return current
            seen.add(current)
            async with conn.execute(
                """
                SELECT json_extract(payload, '$.successor_memory_id')
                FROM events
                WHERE event_type='memory_op'
                  AND branch_id=?
                  AND predecessor_memory_id=?
                  AND json_extract(payload, '$.operation')='compact'
                ORDER BY timestamp ASC
                LIMIT 1
                """,
                (branch_id, current),
            ) as cur:
                row = await cur.fetchone()
            if row is None or row[0] is None:
                return current
            current = row[0]

    # -- maintenance (§5.4) ------------------------------------------------

    async def maintenance(self) -> None:
        """v1: PRAGMA optimize + VACUUM. Called on demand, not on a timer."""
        conn = self._require_conn()
        await conn.execute("PRAGMA optimize")
        await conn.commit()
        # VACUUM cannot run inside a transaction; aiosqlite autocommits the
        # implicit one when we call commit() above.
        await conn.execute("VACUUM")

    # -- minimal query helpers (storage-layer self-tests / Step 3 will
    #    replace these with the proper Pull / Replay APIs) --------------

    async def fetch_event(self, event_id: str) -> LedgerEvent | None:
        conn = self._require_conn()
        async with conn.execute(
            """
            SELECT event_id, session_id, turn_id, parent_event_id,
                   correlation_id, branch_id, event_type, timestamp, payload
            FROM events WHERE event_id=?
            """,
            (event_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return LedgerEvent(
            event_id=row[0],
            session_id=row[1],
            turn_id=row[2],
            parent_event_id=row[3],
            correlation_id=row[4],
            branch_id=row[5],
            event_type=row[6],
            timestamp=row[7],
            payload=json.loads(row[8]),
        )

    async def fetch_by_turn(
        self, turn_id: str, *, branch_id: str = DEFAULT_BRANCH
    ) -> list[LedgerEvent]:
        conn = self._require_conn()
        async with conn.execute(
            """
            SELECT event_id, session_id, turn_id, parent_event_id,
                   correlation_id, branch_id, event_type, timestamp, payload
            FROM events
            WHERE branch_id=? AND turn_id=?
            ORDER BY timestamp ASC
            """,
            (branch_id, turn_id),
        ) as cur:
            rows = await cur.fetchall()
        return [
            LedgerEvent(
                event_id=r[0],
                session_id=r[1],
                turn_id=r[2],
                parent_event_id=r[3],
                correlation_id=r[4],
                branch_id=r[5],
                event_type=r[6],
                timestamp=r[7],
                payload=json.loads(r[8]),
            )
            for r in rows
        ]

    async def explain_query_plan(self, sql: str, params: tuple = ()) -> list[str]:
        """Return the EXPLAIN QUERY PLAN detail strings for `sql`.

        .. warning::
            Test-only helper. Accepts raw SQL — do **not** call from
            production paths. Step 3-4 of doc/53 §11.2 introduces the
            proper Pull / Replay APIs; this method exists solely to let
            unit tests assert that high-frequency queries hit the
            intended covering index.
        """
        conn = self._require_conn()
        async with conn.execute(
            f"EXPLAIN QUERY PLAN {sql}", params
        ) as cur:
            rows = await cur.fetchall()
        return [r[-1] for r in rows]

    # -- internal ----------------------------------------------------------

    def _require_conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("LedgerStore is not open(); call await store.open() first")
        return self._conn
