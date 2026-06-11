"""Tests for `loom memory health` (#504).

When memory_hygiene retired, its stage-1 health snapshot was slimmed into a
lightweight one-shot CLI command. This covers the pure aggregation helper
that the command renders — read-only metrics over the live memory schema.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest_asyncio

from loom.core.memory.store import SQLiteStore
from loom.platform.cli.main import _gather_memory_health


@pytest_asyncio.fixture
async def seeded_db(tmp_path: Path):
    db_path = tmp_path / "memory.db"
    store = SQLiteStore(str(db_path))
    await store.initialize()
    async with store.connect() as conn:
        # Semantic: 3 entries, one low-confidence, spread across domains.
        sem_rows = [
            ("id-a", "self:a", "v", 0.9, "self", "2026-01-01T00:00:00+00:00"),
            ("id-b", "world:b", "v", 0.2, "world", "2026-03-01T00:00:00+00:00"),
            ("id-c", "self:c", "v", 0.8, "self", "2026-05-01T00:00:00+00:00"),
        ]
        for sid, key, val, conf, domain, ts in sem_rows:
            await conn.execute(
                "INSERT INTO semantic_entries "
                "(id, key, value, confidence, domain, temporal, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (sid, key, val, conf, domain, "stable", ts, ts),
            )
        # Episodic: session s1 uncompressed, s2 compressed.
        await conn.execute(
            "INSERT INTO episodic_entries (id, session_id, event_type, content, created_at, compressed_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("e1", "s1", "message", "c", "2026-05-01T00:00:00+00:00", None),
        )
        await conn.execute(
            "INSERT INTO episodic_entries (id, session_id, event_type, content, created_at, compressed_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("e2", "s2", "message", "c", "2026-05-01T00:00:00+00:00", "2026-05-02T00:00:00+00:00"),
        )
        await conn.commit()
        yield conn


async def test_gather_memory_health_metrics(seeded_db):
    health = await _gather_memory_health(seeded_db)

    assert health["semantic_total"] == 3
    assert health["semantic_low_conf"] == 1
    assert health["semantic_oldest"] == "2026-01-01T00:00:00+00:00"
    assert health["episodic_uncompressed_sessions"] == 1
    # domain breakdown surfaces the two domains
    assert dict(health["semantic_by_domain"])["self"] == 2
    assert dict(health["semantic_by_domain"])["world"] == 1


async def test_gather_memory_health_empty(tmp_path: Path):
    store = SQLiteStore(str(tmp_path / "empty.db"))
    await store.initialize()
    async with store.connect() as conn:
        health = await _gather_memory_health(conn)
    assert health["semantic_total"] == 0
    assert health["semantic_low_conf"] == 0
    assert health["semantic_oldest"] is None
    assert health["episodic_uncompressed_sessions"] == 0
