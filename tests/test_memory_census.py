"""Tests for the memory corpus census (issue #587).

The census is the evidence Loom Agent lacked on 2026-09-18: the composition
numbers were right but had to be hand-queried, and the mechanism built on them
was wrong. Each metric is tied to a decision the agent actually made that day.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest_asyncio

from loom.core.memory.census import (
    load_baseline,
    save_snapshot,
    take_census,
)
from loom.core.memory.store import SQLiteStore

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def db(tmp_path: Path):
    store = SQLiteStore(str(tmp_path / "memory.db"))
    await store.initialize()
    async with store.connect() as conn:
        yield conn


_n = 0


async def _row(
    db, *, source="session:s1:fact:0", value="一筆夠長的事實內容在這裡",
    confidence=0.8, days_old=0, accessed_days_ago=None, temporal="recent",
    domain="knowledge", embedding=None, metadata=None,
):
    global _n
    _n += 1
    ts = (NOW - timedelta(days=days_old)).isoformat()
    accessed = (
        None if accessed_days_ago is None
        else (NOW - timedelta(days=accessed_days_ago)).isoformat()
    )
    await db.execute(
        "INSERT INTO semantic_entries (id, key, value, confidence, source, "
        "metadata, created_at, updated_at, embedding, domain, temporal, "
        "last_accessed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"id{_n}", f"k{_n}", value, confidence, source,
         json.dumps(metadata or {}), ts, ts,
         json.dumps(embedding) if embedding is not None else None,
         domain, temporal, accessed),
    )


def _vec(angle_deg: float) -> list[float]:
    a = math.radians(angle_deg)
    return [math.cos(a), math.sin(a), 0.0, 0.0]


class TestComposition:
    async def test_source_families_and_machine_to_hand_ratio(self, db):
        for _ in range(6):
            await _row(db, source="session:abc:fact:1")
        for _ in range(3):
            await _row(db, source="dreaming")
        await _row(db, source="memorize")
        await _row(db, source="user")
        await _row(db, source="task_reflector:ab12")

        c = await take_census(db, now=NOW)
        assert c.total == 12
        assert c.by_source == {
            "session": 6, "dreaming": 3, "memorize": 1, "user": 1,
            "task_reflector": 1,
        }
        assert (c.machine, c.hand) == (9, 2)

    async def test_short_values_are_counted(self, db):
        await _row(db, value="")
        await _row(db, value="  ...  ")
        await _row(db, value="十個字以上的一般內容，這筆不算短")
        c = await take_census(db, now=NOW)
        assert c.short_values == 2


class TestDuplicateDensity:
    async def test_groups_of_three_or_more_at_consolidation_threshold(self, db):
        # Three near-identical (within ~5°, cos > 0.99) → one group of 3.
        for deg in (0, 2, 4):
            await _row(db, embedding=_vec(deg), value=f"每日節奏 dawn 版本 {deg}")
        # A pair (cos ≈ 0.996) → below the ≥3 bar.
        for deg in (60, 65):
            await _row(db, embedding=_vec(deg))
        # A singleton, far away.
        await _row(db, embedding=[0.0, 0.0, 1.0, 0.0])

        c = await take_census(db, now=NOW)
        assert c.dup is not None
        assert c.dup.groups == 1
        assert c.dup.facts_in_groups == 3
        assert c.dup.largest == 3
        assert "每日節奏 dawn" in c.dup.largest_sample

    async def test_dreaming_stubs_and_unembedded_are_not_clustered(self, db):
        """Same exemptions consolidation applies: dreaming output and redirect
        stubs are not facts to merge. Rows without an embedding can't be
        measured — they are counted, not silently skipped."""
        await _row(db, embedding=_vec(0))
        await _row(db, embedding=_vec(1), source="dreaming")
        await _row(db, embedding=_vec(2), metadata={"redirected_to": "k1"})
        await _row(db, embedding=_vec(3))
        await _row(db)  # no embedding

        c = await take_census(db, now=NOW)
        assert c.dup.groups == 0
        assert c.dup.embedded == 2
        assert c.dup.unembedded == 1


class TestLifespanAndUse:
    async def test_decay_buckets_and_next_prune(self, db):
        await _row(db, confidence=0.8)                          # 0.8–1.0
        await _row(db, confidence=0.8, days_old=90)             # 0.4 → 0.3–0.5
        await _row(db, confidence=0.8, days_old=400)            # < 0.1 → due archive
        await _row(db, confidence=0.8, days_old=400, temporal="archived")  # due delete
        await _row(db, confidence=0.95, days_old=400, domain="user",
                   temporal="milestone")                         # never decays

        c = await take_census(db, now=NOW)
        assert c.decay == {
            "<0.1": 2, "0.1–0.3": 0, "0.3–0.5": 1, "0.5–0.8": 0, "≥0.8": 2,
        }
        assert c.due_archive == 1
        assert c.due_delete == 1

    async def test_access_freshness(self, db):
        await _row(db, accessed_days_ago=1)
        await _row(db, accessed_days_ago=20)
        await _row(db, accessed_days_ago=90)
        await _row(db)
        c = await take_census(db, now=NOW)
        assert c.access == {"7d": 1, "30d": 2, "never": 1}

    async def test_archived_by_layer(self, db):
        await _row(db, temporal="archived", source="session:x:fact:0")
        await _row(db, temporal="archived", source="memorize")
        await _row(db, temporal="archived", source="session:y:fact:1")
        c = await take_census(db, now=NOW)
        assert c.archived_by_source == {"session": 2, "memorize": 1}


class TestTrend:
    async def test_baseline_is_the_latest_snapshot_a_week_old(self, db):
        await _row(db)
        old = await take_census(db, now=NOW - timedelta(days=8))
        await save_snapshot(db, old)
        recent = await take_census(db, now=NOW - timedelta(days=2))
        await save_snapshot(db, recent)

        base = await load_baseline(db, now=NOW)
        assert base is not None
        assert base.taken_at == old.taken_at

    async def test_no_baseline_without_week_old_snapshot(self, db):
        c = await take_census(db, now=NOW - timedelta(days=3))
        await save_snapshot(db, c)
        assert await load_baseline(db, now=NOW) is None

    async def test_detail_shows_week_over_week_delta(self, db):
        for _ in range(2):
            await _row(db)
        base = await take_census(db, now=NOW - timedelta(days=7))
        for _ in range(3):
            await _row(db, source="dreaming")
        c = await take_census(db, now=NOW)
        detail = c.render_detail(baseline=base)
        assert "+3" in detail
        assert "machine:hand" in c.render_summary()


class TestMemoryHealthCorpusView:
    async def test_corpus_view_reports_and_snapshots(self, db):
        from unittest.mock import MagicMock
        from loom.core.harness.middleware import ToolCall
        from loom.platform.cli.tools import make_memory_health_tool

        await _row(db, source="memorize")
        tool = make_memory_health_tool(MagicMock(), db)
        call = ToolCall(id="c", tool_name="memory_health", args={"view": "corpus"},
                        trust_level=tool.trust_level, session_id="s")
        r = await tool.executor(call)
        assert r.success and r.output.startswith("corpus: 1 facts")
        assert "no snapshot ≥7d old yet" in r.output
        cur = await db.execute("SELECT COUNT(*) FROM memory_meta WHERE key LIKE 'census:%'")
        assert (await cur.fetchone())[0] == 1

    async def test_ops_view_is_the_default(self, db):
        from unittest.mock import MagicMock
        from loom.core.harness.middleware import ToolCall
        from loom.platform.cli.tools import make_memory_health_tool

        gov = MagicMock()
        gov.health.report.return_value.render_summary.return_value = "ops ok"
        tool = make_memory_health_tool(gov, db)
        call = ToolCall(id="c", tool_name="memory_health", args={},
                        trust_level=tool.trust_level, session_id="s")
        assert (await tool.executor(call)).output == "ops ok"
