"""
Recall indexes over raw experience (#650 S1, spec docs/designs/61 r2).

``session_log`` already keeps every conversation turn (no TTL); what was
missing is a way to *find* a turn without knowing its date or session.  This
module owns the search projections that make it findable:

``session_log_fts``
    FTS5 ``trigram`` index over user/assistant text and the stored tool
    preview (``session_log`` keeps only the first 500 chars of a tool reply;
    the fuller output lives in ``action_records.output_prefix``).  External
    content via the ``session_log_search`` view, kept in sync by triggers in
    the same transaction as the row itself — no checkpoint, no scheduler,
    deletion invalidates automatically.

``session_log_terms``
    Contentless FTS5 index of CJK *bigrams* (other text kept as words), so a
    two-character term such as 「放下」 is an index lookup instead of a scan
    that grows with history.  Bigram projection needs Python, and a trigger
    calling a Python function would break every other writer of the DB (CLI,
    scripts, other agents), so this index is maintained in application code:
    :func:`sync_terms` indexes every row above a single ``memory_meta``
    watermark.  Recall calls it before reading, so rows written by any path
    become findable.

``semantic_fts`` moves from ``unicode61`` to ``trigram`` so Chinese text is
matchable by substring instead of as one whole-sentence token (#643).

Projections are rebuildable from their sources and never written back to
them.  The one-time migration takes a snapshot beside the DB first; if that
fails the migration is skipped and recall reports the conversation route as
unavailable rather than guessing.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import aiosqlite

from loom.core.memory.backup import snapshot_before_write

logger = logging.getLogger(__name__)

INDEX_VERSION = "s1-v1"
_VERSION_KEY = "recall_index_version"
_TERMS_MARK_KEY = "recall_terms_through"

# Tool rows are already <=500-char previews; index them whole but never more.
TOOL_PREVIEW_CHARS = 512
SEARCHABLE_ROLES = ("user", "assistant", "tool")

_CJK_RUN = re.compile(r"[㐀-䶿一-鿿豈-﫿]+")


def is_cjk_char(term: str) -> bool:
    return len(term) == 1 and bool(_CJK_RUN.fullmatch(term))


def bigram_projection(text: str) -> str:
    """CJK runs → space-separated bigrams; everything else kept as words."""
    def _expand(match: re.Match) -> str:
        run = match.group(0)
        if len(run) == 1:
            return f" {run} "
        return " " + " ".join(run[i:i + 2] for i in range(len(run) - 1)) + " "
    return _CJK_RUN.sub(_expand, text)


def _content_expr(prefix: str) -> str:
    """The one projection every index path must share (rebuild + triggers)."""
    return (
        f"CASE WHEN {prefix}role = 'tool' THEN substr({prefix}content, 1, {TOOL_PREVIEW_CHARS}) "
        f"WHEN {prefix}role IN ('user', 'assistant') THEN {prefix}content ELSE '' END"
    )


_LOG_FTS_DDL = f"""
CREATE VIEW IF NOT EXISTS session_log_search AS
  SELECT id, {_content_expr('')} AS content FROM session_log;

CREATE VIRTUAL TABLE IF NOT EXISTS session_log_fts
USING fts5(content, content='session_log_search', content_rowid='id', tokenize='trigram');

CREATE TRIGGER IF NOT EXISTS session_log_fts_ai AFTER INSERT ON session_log BEGIN
  INSERT INTO session_log_fts(rowid, content) VALUES (new.id, {_content_expr('new.')});
END;
CREATE TRIGGER IF NOT EXISTS session_log_fts_ad AFTER DELETE ON session_log BEGIN
  INSERT INTO session_log_fts(session_log_fts, rowid, content)
    VALUES ('delete', old.id, {_content_expr('old.')});
END;
CREATE TRIGGER IF NOT EXISTS session_log_fts_au AFTER UPDATE ON session_log BEGIN
  INSERT INTO session_log_fts(session_log_fts, rowid, content)
    VALUES ('delete', old.id, {_content_expr('old.')});
  INSERT INTO session_log_fts(rowid, content) VALUES (new.id, {_content_expr('new.')});
END;

CREATE VIRTUAL TABLE IF NOT EXISTS session_log_terms
USING fts5(terms, content='', contentless_delete=1, detail='none', tokenize='unicode61');
"""

_SEMANTIC_FTS_DDL = """
DROP TRIGGER IF EXISTS semantic_entries_ai;
DROP TRIGGER IF EXISTS semantic_entries_ad;
DROP TRIGGER IF EXISTS semantic_entries_au;
DROP TABLE IF EXISTS semantic_fts;

CREATE VIRTUAL TABLE semantic_fts
USING fts5(key, value, content='semantic_entries', content_rowid='rowid', tokenize='trigram');

CREATE TRIGGER semantic_entries_ai AFTER INSERT ON semantic_entries BEGIN
  INSERT INTO semantic_fts(rowid, key, value) VALUES (new.rowid, new.key, new.value);
END;
CREATE TRIGGER semantic_entries_ad AFTER DELETE ON semantic_entries BEGIN
  INSERT INTO semantic_fts(semantic_fts, rowid, key, value) VALUES ('delete', old.rowid, old.key, old.value);
END;
CREATE TRIGGER semantic_entries_au AFTER UPDATE ON semantic_entries BEGIN
  INSERT INTO semantic_fts(semantic_fts, rowid, key, value) VALUES ('delete', old.rowid, old.key, old.value);
  INSERT INTO semantic_fts(rowid, key, value) VALUES (new.rowid, new.key, new.value);
END;
"""


async def _meta_get(db: aiosqlite.Connection, key: str) -> str | None:
    cursor = await db.execute("SELECT value FROM memory_meta WHERE key = ?", (key,))
    row = await cursor.fetchone()
    return row[0] if row else None


async def _meta_set(db: aiosqlite.Connection, key: str, value: str) -> None:
    await db.execute(
        "INSERT INTO memory_meta (key, value, updated_at) VALUES (?, ?, datetime('now')) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, value),
    )


async def indexes_ready(db: aiosqlite.Connection) -> bool:
    """True once the S1 migration completed on this DB."""
    try:
        return await _meta_get(db, _VERSION_KEY) == INDEX_VERSION
    except Exception:
        return False


async def _has_data(db: aiosqlite.Connection) -> bool:
    for table in ("semantic_entries", "session_log"):
        cursor = await db.execute(f"SELECT EXISTS (SELECT 1 FROM {table})")
        if (await cursor.fetchone())[0]:
            return True
    return False


async def ensure_recall_indexes(db: aiosqlite.Connection, db_path: Path) -> bool:
    """Run the one-time S1 index migration; idempotent afterwards.

    Returns True when the indexes are ready.  A DB holding data is snapshotted
    to ``<db dir>/backups/recall-index-*.db`` first; no snapshot → no
    migration (the old indexes stay, recall reports the gap).
    """
    if await indexes_ready(db):
        await sync_terms(db)
        return True
    if await _has_data(db):
        snapshot = await snapshot_before_write(
            db, dest_dir=Path(db_path).parent / "backups", prefix="recall-index", keep=1,
        )
        if snapshot is None:
            logger.warning("[recall-index] backup failed; conversation index migration skipped")
            return False
    await db.executescript(_SEMANTIC_FTS_DDL + _LOG_FTS_DDL)
    await db.execute("INSERT INTO semantic_fts(semantic_fts) VALUES ('rebuild')")
    await db.execute("INSERT INTO session_log_fts(session_log_fts) VALUES ('rebuild')")
    await db.execute("DELETE FROM session_log_terms")
    await _meta_set(db, _TERMS_MARK_KEY, "0")
    await db.commit()
    await sync_terms(db)
    await _meta_set(db, _VERSION_KEY, INDEX_VERSION)
    await db.commit()
    return True


async def sync_terms(db: aiosqlite.Connection, *, budget: int | None = None) -> int:
    """Index session_log rows above the watermark into ``session_log_terms``.

    Returns how many rows remain unindexed (0 when caught up).  ``budget``
    bounds the work done inside a recall call; the remainder is reported as a
    coverage gap and picked up next time.
    """
    mark = int(await _meta_get(db, _TERMS_MARK_KEY) or 0)
    done = 0
    while budget is None or done < budget:
        batch = 2000 if budget is None else min(2000, budget - done)
        cursor = await db.execute(
            f"SELECT id, {_content_expr('')} FROM session_log WHERE id > ? ORDER BY id LIMIT ?",
            (mark, batch),
        )
        rows = await cursor.fetchall()
        if not rows:
            break
        await db.executemany(
            "INSERT INTO session_log_terms (rowid, terms) VALUES (?, ?)",
            [(row_id, bigram_projection(text or "")) for row_id, text in rows if text],
        )
        mark = rows[-1][0]
        done += len(rows)
        await _meta_set(db, _TERMS_MARK_KEY, str(mark))
        await db.commit()
    cursor = await db.execute("SELECT COUNT(*) FROM session_log WHERE id > ?", (mark,))
    return (await cursor.fetchone())[0]


async def forget_terms_for_session(db: aiosqlite.Connection, session_id: str) -> None:
    """Drop short-term index rows before the session's rows are deleted."""
    try:
        await db.execute(
            "DELETE FROM session_log_terms WHERE rowid IN "
            "(SELECT id FROM session_log WHERE session_id = ?)",
            (session_id,),
        )
    except aiosqlite.OperationalError:
        pass  # index not built on this DB; nothing to forget
