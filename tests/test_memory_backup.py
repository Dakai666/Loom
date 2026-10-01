"""
Pre-write snapshot of the memory DB (#603 step B, made structural).

The consolidation pass runs with ``execute=true`` on a weekly schedule; #603's
plan requires a backup before any real merge. Instead of relying on someone
remembering, the daemon snapshots the DB right before every executing pass
and falls back to read-only when the snapshot fails ("no backup → no write").
"""

from __future__ import annotations

import os
import sqlite3

import aiosqlite
import pytest_asyncio

from loom.core.memory.backup import snapshot_before_write


@pytest_asyncio.fixture
async def src(tmp_path):
    path = tmp_path / "memory.db"
    conn = await aiosqlite.connect(path)
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("CREATE TABLE t (k TEXT PRIMARY KEY, v TEXT)")
    await conn.executemany("INSERT INTO t VALUES (?, ?)", [("a", "1"), ("b", "2")])
    await conn.commit()
    yield conn
    await conn.close()


class TestSnapshot:
    async def test_snapshot_is_a_complete_readable_copy(self, src, tmp_path):
        out = await snapshot_before_write(src, dest_dir=tmp_path / "bk", prefix="memory-pre-x")
        assert out is not None and out.exists()
        assert out.name.startswith("memory-pre-x-") and out.suffix == ".db"
        rows = sqlite3.connect(out).execute("select k, v from t order by k").fetchall()
        assert rows == [("a", "1"), ("b", "2")]

    async def test_keeps_only_newest_n(self, src, tmp_path):
        dest = tmp_path / "bk"
        dest.mkdir()
        for i in range(5):
            p = dest / f"memory-pre-x-2026010{i}T000000.db"
            p.write_bytes(b"old")
            os.utime(p, (1_000_000 + i, 1_000_000 + i))
        unrelated = dest / "memory.pre-p1-20260503.db"
        unrelated.write_bytes(b"manual")

        out = await snapshot_before_write(src, dest_dir=dest, prefix="memory-pre-x", keep=3)
        mine = sorted(p.name for p in dest.glob("memory-pre-x-*.db"))
        assert len(mine) == 3
        assert out.name in mine
        assert unrelated.exists()  # never touches files outside its prefix

    async def test_failure_returns_none_and_leaves_no_partial(self, src, tmp_path):
        blocker = tmp_path / "not-a-dir"
        blocker.write_text("x")  # dest_dir path is a file → mkdir fails
        out = await snapshot_before_write(src, dest_dir=blocker, prefix="memory-pre-x")
        assert out is None

    async def test_failed_copy_cleans_tmp(self, tmp_path):
        dest = tmp_path / "bk"
        conn = await aiosqlite.connect(tmp_path / "m.db")
        await conn.close()  # closed connection → backup raises
        out = await snapshot_before_write(conn, dest_dir=dest, prefix="memory-pre-x")
        assert out is None
        assert not dest.exists() or list(dest.glob("*")) == []
