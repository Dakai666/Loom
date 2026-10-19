"""Big dream — one-off whole-corpus tidy-up (docs/designs/57 §6).

Census → convergent dream over the whole corpus with no caps → distill
passes until no eligible neighbourhood is left (or a pass makes no progress)
→ census again. Read-only unless each stage's execute gate is passed in.
Only the LLM is faked; store, planning, gates, backups and writes are real.
"""

from __future__ import annotations

import json

import pytest

import loom.core.memory.backup as backup
from loom.core.cognition.big_dream import run_big_dream
from tests.test_distill_dream import _llm, _row, _snapshot, _topic, db, semantic  # noqa: F401


def _dream_llm(**kw):
    """Distill prompts go to the distill fake; convergent prompts get an
    answer that defers everything (its own contracts are tested elsewhere)."""
    import loom.core.cognition.distillation as dd
    distill = _llm(**kw)
    distill_systems = (dd._DISTILL_SYSTEM, dd._DISTILL_GATE_SYSTEM, dd._DISTILL_REVIEW_SYSTEM)

    async def fn(messages):
        if messages[0]["content"] in distill_systems:
            return await distill(messages)
        return json.dumps([])
    return fn


async def _big(db, semantic, tmp_path, llm=None, **kw):
    kw.setdefault("execute_consolidation", False)
    kw.setdefault("execute_distill", False)
    return await run_big_dream(db, semantic, llm or _dream_llm(),
                               dreams_dir=tmp_path / "dreams", log=lambda *_: None, **kw)


async def _live(db) -> int:
    cur = await db.execute("SELECT count(*) FROM semantic_entries "
                           "WHERE json_extract(metadata, '$.redirected_to') IS NULL")
    return (await cur.fetchone())[0]


class TestReadOnly:
    async def test_read_only_runs_one_distill_pass_and_writes_nothing(self, db, semantic, tmp_path):
        await _topic(db, "a", 0)
        await _topic(db, "b", 180)
        before = await _snapshot(db)
        s = await _big(db, semantic, tmp_path, distill_batch=1)
        assert await _snapshot(db) == before
        assert s.distill_passes == 1
        assert s.census_before.total == s.census_after.total == 8
        assert s.mode == "read-only"

    async def test_reports_reach_the_journal(self, db, semantic, tmp_path):
        await _topic(db, "a", 0)
        await _big(db, semantic, tmp_path)
        (report,) = list((tmp_path / "dreams").glob("*.md"))
        text = report.read_text()
        assert "記憶蒸餾" in text and "夢境鞏固" in text


class TestExecute:
    async def test_passes_continue_until_nothing_is_left(self, db, semantic, tmp_path):
        await _topic(db, "a", 0)
        await _topic(db, "b", 180)
        s = await _big(db, semantic, tmp_path, execute_distill=True, distill_batch=1)
        assert s.distill_executed == 2
        assert s.distill_passes == 3                   # the third finds nothing
        assert s.stop_reason == "no eligible neighbourhood left"
        assert await _live(db) == 8 - 2 * 2            # 3 covers each → 2 stubs each

    async def test_rejections_are_moved_past(self, db, semantic, tmp_path):
        await _topic(db, "a", 0)
        await _topic(db, "b", 180)
        s = await _big(db, semantic, tmp_path, _dream_llm(review="skip"),
                       execute_distill=True, distill_batch=1)
        assert s.distill_rejected == 2 and s.distill_executed == 0
        assert s.stop_reason == "no eligible neighbourhood left"

    async def test_no_progress_stops_instead_of_looping(self, db, semantic, tmp_path):
        await _topic(db, "a", 0)
        s = await _big(db, semantic, tmp_path, _dream_llm(review="defer"),
                       execute_distill=True, max_passes=10)
        assert s.distill_passes == 1
        assert "no progress" in s.stop_reason

    async def test_an_unwritable_neighbourhood_is_tried_once(self, db, semantic, tmp_path, monkeypatch):
        await _topic(db, "a", 0)
        for i, base in enumerate((90, 180, 270)):
            await _topic(db, f"r{i}", base, n=3)
        real = semantic.consolidate

        async def broken_for_a(entries, **kw):
            if kw["survivor_key"].startswith("a"):
                raise RuntimeError("disk full")
            return await real(entries, **kw)
        monkeypatch.setattr(semantic, "consolidate", broken_for_a)
        reviewed = []
        base_llm = _dream_llm()

        async def llm(messages):
            import loom.core.cognition.distillation as dd
            if messages[0]["content"] is dd._DISTILL_SYSTEM:
                reviewed.append('key="a' in messages[-1]["content"])
            return await base_llm(messages)
        s = await _big(db, semantic, tmp_path, llm, execute_distill=True, distill_batch=1)
        assert reviewed.count(True) == 1               # not once per pass
        assert s.distill_executed == 3

    async def test_max_passes_is_respected(self, db, semantic, tmp_path):
        for i, base in enumerate((0, 90, 180, 270)):
            await _topic(db, f"t{i}", base, n=3)
        s = await _big(db, semantic, tmp_path, execute_distill=True,
                       distill_batch=1, max_passes=2)
        assert s.distill_passes == 2
        assert "max_passes" in s.stop_reason

    async def test_consolidation_backs_up_first(self, db, semantic, tmp_path, monkeypatch):
        prefixes = []
        real = backup.snapshot_before_write

        async def spy(conn, **kw):
            prefixes.append(kw["prefix"])
            return await real(conn, **kw)
        monkeypatch.setattr(backup, "snapshot_before_write", spy)
        await _topic(db, "a", 0)
        await _big(db, semantic, tmp_path, execute_consolidation=True, execute_distill=True)
        assert prefixes[0] == "memory-pre-consolidation"
        assert "memory-pre-distill" in prefixes

    async def test_failed_consolidation_backup_keeps_it_read_only(
            self, db, semantic, tmp_path, monkeypatch):
        async def broken(conn, **kw):
            return None
        monkeypatch.setattr(backup, "snapshot_before_write", broken)
        await _topic(db, "a", 0)
        before = await _snapshot(db)
        s = await _big(db, semantic, tmp_path, execute_consolidation=True, execute_distill=True)
        assert await _snapshot(db) == before
        assert any("backup" in n for n in s.notes)
        assert "backup" in s.stop_reason

    async def test_summary_compares_censuses(self, db, semantic, tmp_path):
        await _topic(db, "a", 0)
        s = await _big(db, semantic, tmp_path, execute_distill=True)
        text = s.render()
        assert "before" in text and "after" in text
        assert str(s.census_before.total) in text


async def test_skipping_consolidation_still_counts_as_execute(db, semantic, tmp_path):
    await _topic(db, "a", 0)
    s = await _big(db, semantic, tmp_path, execute_distill=True, skip_consolidation=True)
    assert s.consolidation_mode == "skipped" and s.mode == "execute"
    assert not list((tmp_path / "dreams").glob("*.md"))[0].read_text().count("夢境鞏固")


async def test_one_gate_off_is_partial(db, semantic, tmp_path):
    await _topic(db, "a", 0)
    s = await _big(db, semantic, tmp_path, execute_distill=True)   # consolidation read-only
    assert s.mode == "partial"


@pytest.mark.parametrize("value", [0, -1])
async def test_bad_batch_is_refused(db, semantic, tmp_path, value):
    with pytest.raises(ValueError):
        await _big(db, semantic, tmp_path, distill_batch=value)
