"""Distill dream P2 — execute contracts (docs/designs/57 §4.5, §5, §8).

``execute=True`` turns approved distillations into one survivor plus redirect
stubs via ``semantic.consolidate``. Every original stays recoverable from the
survivor's metadata, a stale or no-longer-eligible neighbourhood is skipped,
and nothing is written without a fresh backup.

Only the LLM and the vectors are faked; planning, gating, review, the stale
check and the write run for real.
"""

from __future__ import annotations

import pytest

import loom.core.memory.backup as backup
from loom.core.cognition.distillation import (
    PRODUCED_BY,
    STATUS_APPROVED,
    build_distill_plan,
    run_distill_dream,
)
from tests.test_distill_dream import (  # noqa: F401
    _call, _facts, _llm, _row, _snapshot, _topic, db, semantic,
)


def _cover_replaceable(user):
    """Distil every fact the prompt marks replaceable (protected=no)."""
    idx = [i for i, line in enumerate(_facts(user), 1) if "protected=no" in line]
    return {"distilled": "精煉後的一條", "covers": idx, "dropped": [], "rationale": "r"}


async def _execute(semantic, llm=None, **kw):
    return await run_distill_dream(semantic, llm or _llm(), execute=True, **kw)


def _approved(plan):
    (r,) = [r for r in plan.results if r.status == STATUS_APPROVED]
    return r


def _llm_mutating(db, mutate):
    """Default verdicts, but run ``mutate`` between planning and execution
    (during self-review, the last LLM step before the write)."""
    base = _llm(distill=_cover_replaceable)
    done = False

    async def fn(messages):
        nonlocal done
        from loom.core.cognition import distillation as dd
        if messages[0]["content"] is dd._DISTILL_REVIEW_SYSTEM and not done:
            done = True
            await mutate()
        return await base(messages)
    return fn


class TestWrite:
    async def test_covered_facts_fold_into_the_survivor(self, db, semantic):
        await _topic(db)
        plan, _ = await _execute(semantic)
        r = _approved(plan)
        survivor = await semantic.get(r.survivor_key)
        assert survivor.value == "精煉後的一條"
        for key in r.distillation.covers:
            resolved = await semantic.resolve_redirect(key)
            assert resolved.key == r.survivor_key
            if key != r.survivor_key:
                stub = await semantic.get(key)
                assert stub.metadata["redirected_to"] == r.survivor_key

    async def test_every_original_is_recoverable_verbatim(self, db, semantic):
        await _topic(db)
        originals = {k: v for k, v, *_ in await _snapshot(db)}
        plan, _ = await _execute(semantic)
        r = _approved(plan)
        ci = (await semantic.get(r.survivor_key)).metadata["consolidated_into"]
        kept = {s["key"]: s["value"] for s in ci["sacrificed_entries"]}
        kept[r.survivor_key] = ci["survivor_original"]["value"]
        assert kept == {k: originals[k] for k in r.distillation.covers}

    async def test_survivor_is_marked_and_keeps_its_source(self, db, semantic):
        await _topic(db)
        plan, _ = await _execute(semantic)
        r = _approved(plan)
        s = await semantic.get(r.survivor_key)
        assert s.metadata["produced_by"] == PRODUCED_BY
        assert s.metadata["distilled_at"]
        assert s.source == "session:s1:fact:0"

    async def test_dropped_claims_are_recorded_on_the_survivor(self, db, semantic):
        await _topic(db)
        llm = _llm(distill=lambda _u: {
            "distilled": "精煉", "covers": [1, 2, 3],
            "dropped": [{"text": "一個過時的細節", "reason": "superseded"}], "rationale": "r",
        })
        plan, _ = await _execute(semantic, llm)
        r = _approved(plan)
        ci = (await semantic.get(r.survivor_key)).metadata["consolidated_into"]
        assert "一個過時的細節" in ci["rationale"]
        assert plan.pass_id in ci["rationale"]

    async def test_context_and_uncovered_members_are_untouched(self, db, semantic):
        await _topic(db)
        await _row(db, "hand", 5, source="manual")
        before = {row[0]: row for row in await _snapshot(db)}
        plan, _ = await _execute(semantic, _llm(distill=_cover_replaceable))
        covers = set(_approved(plan).distillation.covers)
        after = {row[0]: row for row in await _snapshot(db)}
        untouched = set(before) - covers
        assert "hand" in untouched
        assert {k: after[k] for k in untouched} == {k: before[k] for k in untouched}

    async def test_a_distilled_survivor_is_not_replaced_again_soon(self, db, semantic):
        await _topic(db)
        plan, _ = await _execute(semantic)
        survivor = _approved(plan).survivor_key
        again = await build_distill_plan(semantic)
        for nb in again.neighbourhoods:
            if survivor in {m.key for m in nb.members}:
                assert survivor in nb.context_keys

    async def test_rejected_and_deferred_are_not_written(self, db, semantic):
        await _topic(db)
        before = await _snapshot(db)
        await _execute(semantic, _llm(review="skip"))
        await _execute(semantic, _llm(review="defer"))
        assert await _snapshot(db) == before

    async def test_read_only_default_still_writes_nothing(self, db, semantic):
        await _topic(db)
        before = await _snapshot(db)
        plan, report = await run_distill_dream(semantic, _llm())
        assert await _snapshot(db) == before
        assert "read-only" in report


class TestStale:
    async def test_member_edited_after_planning_skips_the_neighbourhood(self, db, semantic):
        await _topic(db)

        async def edit():
            await db.execute("UPDATE semantic_entries SET value='edited', "
                             "updated_at='2026-10-19T13:00:00+00:00' WHERE key='t1'")
            await db.commit()
        plan, report = await _execute(semantic, _llm_mutating(db, edit))
        r = _approved(plan)
        assert r.outcome == "stale"
        for k in ("t0", "t1", "t2", "t3"):
            assert (await semantic.get(k)).metadata.get("redirected_to") is None
        assert "stale" in report

    async def test_context_fact_edited_also_counts_as_stale(self, db, semantic):
        await _topic(db)
        await _row(db, "hand", 5, source="manual")

        async def edit():
            await db.execute("UPDATE semantic_entries SET value='changed my mind', "
                             "updated_at='2026-10-19T13:00:00+00:00' WHERE key='hand'")
            await db.commit()
        plan, _ = await _execute(semantic, _llm_mutating(db, edit))
        assert _approved(plan).outcome == "stale"

    async def test_member_deleted_after_planning_is_stale(self, db, semantic):
        await _topic(db)

        async def drop():
            await db.execute("DELETE FROM semantic_entries WHERE key='t2'")
            await db.commit()
        plan, _ = await _execute(semantic, _llm_mutating(db, drop))
        assert _approved(plan).outcome == "stale"

    async def test_archived_after_planning_is_rechecked_at_execute(self, db, semantic):
        # lifecycle demotes by setting temporal only — updated_at is unchanged,
        # so the snapshot check alone would pass; eligibility must be re-derived.
        await _topic(db)

        async def archive():
            await db.execute("UPDATE semantic_entries SET temporal='archived' WHERE key='t1'")
            await db.commit()
        plan, _ = await _execute(semantic, _llm_mutating(db, archive))
        r = _approved(plan)
        assert r.outcome == "ineligible"
        assert (await semantic.get("t0")).metadata.get("redirected_to") is None

    async def test_one_stale_neighbourhood_does_not_block_another(self, db, semantic):
        await _topic(db, "a", 0)
        await _topic(db, "b", 180)

        async def edit():
            await db.execute("UPDATE semantic_entries SET updated_at='2026-10-19T13:00:00+00:00' "
                             "WHERE key='a1'")
            await db.commit()
        plan, _ = await _execute(semantic, _llm_mutating(db, edit))
        outcomes = sorted(r.outcome for r in plan.results)
        assert outcomes == ["executed", "stale"]


class TestBackup:
    async def test_snapshot_is_taken_before_writing(self, db, semantic, monkeypatch):
        await _topic(db)
        seen = []
        real = backup.snapshot_before_write

        async def spy(conn, **kw):
            cur = await conn.execute("SELECT count(*) FROM semantic_entries "
                                     "WHERE json_extract(metadata, '$.redirected_to') IS NOT NULL")
            seen.append(((await cur.fetchone())[0], kw["prefix"]))
            return await real(conn, **kw)
        monkeypatch.setattr(backup, "snapshot_before_write", spy)
        plan, _ = await _execute(semantic)
        assert seen == [(0, "memory-pre-distill")]
        assert _approved(plan).outcome == "executed"

    async def test_failed_backup_downgrades_to_read_only(self, db, semantic, monkeypatch):
        await _topic(db)
        before = await _snapshot(db)

        async def broken(conn, **kw):
            return None
        monkeypatch.setattr(backup, "snapshot_before_write", broken)
        plan, report = await _execute(semantic)
        assert await _snapshot(db) == before
        assert plan.mode == "read-only"
        assert "備份失敗" in report

    async def test_no_backup_when_nothing_was_approved(self, db, semantic, monkeypatch):
        await _topic(db)
        calls = []

        async def spy(conn, **kw):
            calls.append(kw)
            return None
        monkeypatch.setattr(backup, "snapshot_before_write", spy)
        await _execute(semantic, _llm(review="skip"))
        assert calls == []


class TestFailure:
    async def test_consolidate_error_is_isolated(self, db, semantic, monkeypatch):
        await _topic(db, "a", 0)
        await _topic(db, "b", 180)
        real = semantic.consolidate

        async def flaky(entries, **kw):
            if kw["survivor_key"].startswith("a"):
                raise RuntimeError("disk full")
            return await real(entries, **kw)
        monkeypatch.setattr(semantic, "consolidate", flaky)
        plan, report = await _execute(semantic)
        outcomes = sorted(r.outcome.split(":")[0] for r in plan.results)
        assert outcomes == ["error", "executed"]
        assert "disk full" in report


class TestReport:
    async def test_report_states_execute_mode_and_outcome(self, db, semantic):
        await _topic(db)
        plan, report = await _execute(semantic)
        assert "mode: execute" in report
        assert "read-only" not in report
        assert "已寫入" in report


class TestToolExecute:
    async def test_tool_executes_when_asked(self, db, semantic, tmp_path):
        from loom.core.memory.maintenance import make_distill_dream_tool

        await _topic(db)
        tool = make_distill_dream_tool(semantic, _llm(), dreams_dir=tmp_path, allow_execute=True)
        res = await tool.executor(_call({"execute": True}))
        assert res.success, res.error
        assert "executed=1" in res.output
        stubs = [r for r in await _snapshot(db) if "redirected_to" in r[4]]
        assert len(stubs) == 2

    @pytest.mark.parametrize("value", ["true", 1, "yes"])
    async def test_tool_requires_a_real_boolean(self, db, semantic, tmp_path, value):
        from loom.core.memory.maintenance import make_distill_dream_tool

        await _topic(db)
        before = await _snapshot(db)
        tool = make_distill_dream_tool(semantic, _llm(), dreams_dir=tmp_path, allow_execute=True)
        res = await tool.executor(_call({"execute": value}))
        assert not res.success
        assert await _snapshot(db) == before



class TestReviewFixes:
    async def test_write_racing_the_check_is_not_overwritten(self, db, semantic, monkeypatch):
        # a memorize lands after execute's reads but before consolidate's UPDATE
        await _topic(db)
        real = semantic.consolidate

        async def racing(entries, **kw):
            await db.execute("UPDATE semantic_entries SET value='just memorized', "
                             "updated_at='2099-01-01T00:00:00+00:00' WHERE key=?",
                             (entries[-1].key,))
            await db.commit()
            return await real(entries, **kw)
        monkeypatch.setattr(semantic, "consolidate", racing)
        plan, _ = await _execute(semantic)
        r = _approved(plan)
        assert r.outcome == "stale"
        assert (await semantic.get(r.distillation.covers[-1])).value == "just memorized"

    async def test_footer_does_not_claim_writes_when_none_landed(self, db, semantic):
        await _topic(db)

        async def edit():
            await db.execute("UPDATE semantic_entries SET updated_at='2099-01-01T00:00:00+00:00' "
                             "WHERE key='t1'")
            await db.commit()
        _, report = await _execute(semantic, _llm_mutating(db, edit))
        assert "已寫入的蒸餾" not in report
        assert "沒有任何蒸餾寫入" in report


class TestExecuteGate:
    async def test_tool_refuses_execute_unless_config_allows(self, db, semantic, tmp_path):
        from loom.core.memory.maintenance import make_distill_dream_tool

        await _topic(db)
        before = await _snapshot(db)
        tool = make_distill_dream_tool(semantic, _llm(), dreams_dir=tmp_path)
        res = await tool.executor(_call({"execute": True}))
        assert not res.success and "[memory.distill_dream]" in res.error
        assert await _snapshot(db) == before
        assert (await tool.executor(_call({}))).success   # read-only still fine

    @pytest.mark.parametrize("cfg, expected", [
        ({}, False),
        ({"memory": {"distill_dream": {"execute": False}}}, False),
        ({"memory": {"distill_dream": {"execute": "true"}}}, False),
        ({"memory": {"distill_dream": {"execute": 1}}}, False),
        ({"memory": {"distill_dream": {"execute": True}}}, True),
    ])
    def test_config_gate_needs_a_literal_true(self, cfg, expected):
        from loom.core.memory.maintenance import distill_execute_enabled
        assert distill_execute_enabled(cfg) is expected

    async def test_nothing_approved_is_not_reported_as_backup_failure(self, db, semantic, tmp_path):
        from loom.core.memory.maintenance import make_distill_dream_tool

        await _topic(db)
        tool = make_distill_dream_tool(semantic, _llm(review="skip"), dreams_dir=tmp_path,
                                       allow_execute=True)
        res = await tool.executor(_call({"execute": True}))
        assert "backup failed" not in res.output
        assert "nothing was approved" in res.output

    async def test_failed_backup_is_reported_as_such(self, db, semantic, tmp_path, monkeypatch):
        from loom.core.memory.maintenance import make_distill_dream_tool

        async def broken(conn, **kw):
            return None
        monkeypatch.setattr(backup, "snapshot_before_write", broken)
        await _topic(db)
        tool = make_distill_dream_tool(semantic, _llm(), dreams_dir=tmp_path, allow_execute=True)
        res = await tool.executor(_call({"execute": True}))
        assert "backup failed" in res.output
