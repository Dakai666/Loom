"""Distill dream P1 — read-only contracts (docs/designs/57 §4–§5, §8).

A distill pass picks a topic neighbourhood (cos ∈ [0.70, 0.85) plus anything
closer), asks for one refined fact that replaces its machine-written sources,
gates the result and lets Loom veto it. P1 plans and reports; it never writes.

Only the LLM and the vectors are faked; the store, planning, gating and
review run for real.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

import loom.core.cognition.distillation as dd
from loom.core.cognition.distillation import (
    PRODUCED_BY,
    STATUS_APPROVED,
    STATUS_DEFERRED,
    STATUS_FAILED,
    STATUS_REJECTED,
    build_distill_plan,
    planned_survivor_metadata,
    run_distill_dream,
)
from loom.core.memory.semantic import SemanticMemory
from loom.core.memory.store import SQLiteStore

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def db(tmp_path):
    store = SQLiteStore(str(tmp_path / "distill.db"))
    await store.initialize()
    async with store.connect() as conn:
        yield conn


@pytest_asyncio.fixture
async def semantic(db):
    return SemanticMemory(db)


def _vec(deg: float) -> list[float]:
    a = math.radians(deg)
    return [math.cos(a), math.sin(a), 0.0, 0.0]


async def _row(db, key, deg, *, source="session:s1:fact:0", value=None,
               temporal="recent", domain="knowledge", metadata=None, days_old=10):
    ts = (NOW - timedelta(days=days_old)).isoformat()
    await db.execute(
        "INSERT INTO semantic_entries (id, key, value, confidence, source, metadata, "
        "created_at, updated_at, embedding, domain, temporal) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (key, key, value or f"fact {key}", 0.8, source, json.dumps(metadata or {}),
         ts, ts, json.dumps(_vec(deg)) if deg is not None else None, domain, temporal),
    )
    await db.commit()


async def _topic(db, prefix="t", base=0.0, n=4, **kw):
    """n facts within ~38° of each other: cos ≥ 0.78, a distillable topic."""
    for i, off in enumerate((0, 10, 25, 38)[:n]):
        await _row(db, f"{prefix}{i}", base + off, **kw)


async def _snapshot(db):
    cur = await db.execute(
        "SELECT key, value, confidence, source, metadata, updated_at, embedding, "
        "domain, temporal FROM semantic_entries ORDER BY key")
    return await cur.fetchall()


def _llm(*, distill=None, gate=None, review="approve", fail_distill_for=None):
    """Dispatch on the system prompt. ``distill(facts_text) -> dict``."""
    async def fn(messages):
        system, user = messages[0]["content"], messages[-1]["content"]
        if system is dd._DISTILL_SYSTEM:
            if fail_distill_for and fail_distill_for in user:
                raise RuntimeError("provider down")
            out = distill(user) if distill else {
                "distilled": "精煉後的一條", "covers": [1, 2, 3], "dropped": [],
                "rationale": "same topic",
            }
            return json.dumps(out)
        if system is dd._DISTILL_GATE_SYSTEM:
            if gate is not None:
                return gate(user)
            n = user.count("[S")
            return json.dumps({"sources": {str(i): "covered" for i in range(1, n + 1)},
                               "handwritten_restated": [], "rationale": "ok"})
        if system is dd._DISTILL_REVIEW_SYSTEM:
            ids = [line.split('"')[1] for line in user.splitlines()
                   if line.startswith('neighbourhood_id="')]
            return json.dumps([{"cluster_id": i, "verdict": review, "reason": "r"} for i in ids])
        raise AssertionError(f"unexpected prompt: {system[:60]}")
    return fn


# ── seeds and neighbourhoods ──────────────────────────────────────────────

class TestNeighbourhoods:
    async def test_band_membership_and_minimum_size(self, db, semantic):
        await _topic(db)
        await _row(db, "far", 90)            # cos ≤ 0.62 to every topic fact
        await _row(db, "lonely1", 170)       # a 2-fact topic: too small
        await _row(db, "lonely2", 175)
        plan = await build_distill_plan(semantic)
        assert len(plan.neighbourhoods) == 1
        assert {m.key for m in plan.neighbourhoods[0].members} == {"t0", "t1", "t2", "t3"}

    async def test_seed_is_never_dream_hand_written_milestone_or_stub(self, db, semantic):
        await _topic(db, "d", 0, source="dreaming")
        await _topic(db, "h", 90, source="manual")
        await _topic(db, "m", 180, temporal="milestone")
        await _topic(db, "s", 270, metadata={"redirected_to": "x"})
        plan = await build_distill_plan(semantic)
        assert plan.neighbourhoods == []

    async def test_protected_members_ride_along_as_context(self, db, semantic):
        await _topic(db, n=3)
        await _row(db, "hand", 5, source="manual")
        plan = await build_distill_plan(semantic)
        (nb,) = plan.neighbourhoods
        assert "hand" in {m.key for m in nb.members}
        assert nb.protected_keys == {"hand"}

    async def test_neighbourhoods_are_disjoint(self, db, semantic):
        # A long arc (5° apart): every fact's 11 nearest overlap its
        # neighbours', so a second neighbourhood must avoid the first's members.
        for i in range(20):
            await _row(db, f"a{i:02d}", i * 5)
        plan = await build_distill_plan(semantic, max_neighbourhoods=5)
        assert len(plan.neighbourhoods) >= 2
        seen: list[str] = [m.key for nb in plan.neighbourhoods for m in nb.members]
        assert len(seen) == len(set(seen))

    async def test_recently_distilled_survivor_is_not_reseeded(self, db, semantic):
        await _topic(db, metadata={"produced_by": PRODUCED_BY,
                                   "distilled_at": datetime.now(UTC).isoformat()})
        plan = await build_distill_plan(semantic)
        assert plan.neighbourhoods == []


    async def test_hand_written_fact_is_context_for_every_neighbourhood(self, db, semantic):
        """Context facts aren't consumed: two topics around one hand-written
        fact both see it, so neither can restate it unchecked (#622 review)."""
        await _row(db, "hand", 30, source="manual")
        for i, deg in enumerate((0, 6, 12)):
            await _row(db, f"a{i}", deg)
        for i, deg in enumerate((60, 66, 72)):   # ≤45° from hand, ≥48° from a
            await _row(db, f"b{i}", deg)
        plan = await build_distill_plan(semantic)
        assert len(plan.neighbourhoods) == 2
        assert all("hand" in nb.protected_keys for nb in plan.neighbourhoods)

    async def test_needs_two_replaceable_members(self, db, semantic):
        await _row(db, "seed", 0)
        await _row(db, "h1", 5, source="manual")
        await _row(db, "h2", 10, source="manual")
        plan = await build_distill_plan(semantic)
        assert plan.neighbourhoods == []

    async def test_unreadable_distilled_at_counts_as_recent(self, db, semantic):
        await _topic(db, metadata={"produced_by": PRODUCED_BY, "distilled_at": "2026-10-19T08:00:00"})
        plan = await build_distill_plan(semantic)
        assert plan.neighbourhoods == []


# ── hard boundaries enforced in code, not by the LLM ──────────────────────

class TestHardBoundaries:
    async def test_covers_with_protected_fact_is_rejected(self, db, semantic):
        await _topic(db, n=3)
        await _row(db, "hand", 5, source="manual")

        def distill(user):
            idx = next(i for i, line in enumerate(_facts(user), 1) if 'key="hand"' in line)
            return {"distilled": "x", "covers": [1, idx], "dropped": [], "rationale": ""}

        plan, _ = await run_distill_dream(semantic, _llm(distill=distill))
        (r,) = plan.results
        assert r.status == STATUS_REJECTED
        assert "protected" in r.reason

    async def test_recently_distilled_neighbour_is_context_only(self, db, semantic):
        await _topic(db, n=3)
        await _row(db, "fresh", 5, metadata={"produced_by": PRODUCED_BY,
                                             "distilled_at": datetime.now(UTC).isoformat()})

        def distill(user):
            idx = next(i for i, line in enumerate(_facts(user), 1) if 'key="fresh"' in line)
            assert "protected=yes" in _facts(user)[idx - 1]
            return {"distilled": "x", "covers": [1, idx], "dropped": [], "rationale": ""}

        plan, _ = await run_distill_dream(semantic, _llm(distill=distill))
        (r,) = plan.results
        assert r.status == STATUS_REJECTED
        assert "context-only" in r.reason

    async def test_no_non_dream_survivor_is_rejected(self, db, semantic):
        await _row(db, "seed", 0)
        await _row(db, "dr1", 8, source="dreaming")
        await _row(db, "dr2", 16, source="dreaming")

        def distill(user):
            lines = _facts(user)
            dream = [i for i, l in enumerate(lines, 1) if 'trust=dreaming' in l]
            return {"distilled": "x", "covers": dream, "dropped": [], "rationale": ""}

        plan, _ = await run_distill_dream(semantic, _llm(distill=distill))
        (r,) = plan.results
        assert r.status == STATUS_REJECTED
        assert "survivor" in r.reason

    async def test_survivor_prefers_a_key_nothing_will_rewrite(self, db, semantic):
        # rel:{S}::{P} keys are derived from the triple, so a later triple with
        # the same subject/predicate lands on the same key; a session fact key
        # is unique. Same trust → the non-rel key survives, even if younger.
        await _row(db, "rel:user::has_skill", 0, days_old=30)
        await _row(db, "rel:x::pass_rate", 10, days_old=20)
        await _row(db, "session:s1:20261001T000000:fact:3", 25, days_old=5)
        plan, _ = await run_distill_dream(semantic, _llm())
        (r,) = plan.results
        assert r.survivor_key == "session:s1:20261001T000000:fact:3"

    async def test_higher_trust_still_beats_key_shape(self, db, semantic):
        await _row(db, "rel:user::has_skill", 0)                 # session_compress 0.8
        await _row(db, "rel:x::pass_rate", 10, days_old=5)
        await _row(db, "skill:image", 25, source="skill_eval:r1")  # agent_inferred 0.7
        plan, _ = await run_distill_dream(semantic, _llm())
        (r,) = plan.results
        assert r.survivor_key == "rel:user::has_skill"

    async def test_single_source_cover_is_rejected(self, db, semantic):
        await _topic(db)
        plan, _ = await run_distill_dream(semantic, _llm(
            distill=lambda u: {"distilled": "x", "covers": [1], "dropped": [], "rationale": ""}))
        assert plan.results[0].status == STATUS_REJECTED

    async def test_survivor_is_highest_trust_non_dream_cover(self, db, semantic):
        await _row(db, "sess", 0)
        await _row(db, "agent", 8, source="skill_eval:x")      # agent_inferred 0.7
        await _row(db, "dream", 16, source="dreaming")
        plan, _ = await run_distill_dream(semantic, _llm(
            distill=lambda u: {"distilled": "x", "covers": [1, 2, 3], "dropped": [], "rationale": ""}))
        (r,) = plan.results
        assert r.status == STATUS_APPROVED
        assert r.survivor_key == "sess"                        # session 0.8 > agent 0.7


# ── the gate ──────────────────────────────────────────────────────────────

class TestGate:
    async def test_lost_source_blocks(self, db, semantic):
        await _topic(db, n=3)
        gate = lambda u: json.dumps({"sources": {"1": "covered", "2": "lost", "3": "covered"},
                                     "handwritten_restated": [], "rationale": "2 lost"})
        plan, _ = await run_distill_dream(semantic, _llm(gate=gate))
        assert plan.results[0].status == STATUS_REJECTED

    async def test_restating_hand_written_claim_blocks(self, db, semantic):
        await _topic(db, n=3)
        await _row(db, "hand", 5, source="manual")
        gate = lambda u: json.dumps({"sources": {"1": "covered", "2": "covered", "3": "covered"},
                                     "handwritten_restated": ["H1"], "rationale": "dup"})
        plan, _ = await run_distill_dream(semantic, _llm(gate=gate))
        (r,) = plan.results
        assert r.status == STATUS_REJECTED
        assert "hand-written" in r.reason

    async def test_gate_accepts_the_S_labels_it_was_shown(self, db, semantic):
        await _topic(db, n=3)
        gate = lambda u: json.dumps({"sources": {"S1": "covered", "[S2]": "dropped", "s3": "covered"},
                                     "handwritten_restated": [], "rationale": "ok"})
        plan, _ = await run_distill_dream(semantic, _llm(gate=gate))
        assert plan.results[0].status == STATUS_APPROVED

    async def test_restated_ids_not_shown_are_ignored(self, db, semantic):
        await _topic(db, n=3)                      # no kept facts → no H ids shown
        gate = lambda u: json.dumps({"sources": {"1": "covered", "2": "covered", "3": "covered"},
                                     "handwritten_restated": ["none", ""], "rationale": "ok"})
        plan, _ = await run_distill_dream(semantic, _llm(gate=gate))
        assert plan.results[0].status == STATUS_APPROVED

    async def test_untrustworthy_gate_output_defers_never_approves(self, db, semantic):
        await _topic(db, n=3)
        plan, _ = await run_distill_dream(semantic, _llm(gate=lambda u: "not json"))
        assert plan.results[0].status == STATUS_DEFERRED

    async def test_gate_must_cover_every_source(self, db, semantic):
        await _topic(db, n=3)
        gate = lambda u: json.dumps({"sources": {"1": "covered"}, "handwritten_restated": [],
                                     "rationale": "partial"})
        plan, _ = await run_distill_dream(semantic, _llm(gate=gate))
        assert plan.results[0].status == STATUS_DEFERRED


# ── review, failures, read-only, metadata contract ────────────────────────

class TestPass:
    async def test_self_review_veto_is_respected(self, db, semantic):
        await _topic(db)
        plan, _ = await run_distill_dream(semantic, _llm(review="skip"))
        assert plan.results[0].status == STATUS_REJECTED

    async def test_distill_failure_isolated_to_its_neighbourhood(self, db, semantic):
        await _topic(db, "a", 0)
        await _topic(db, "b", 180)
        plan, _ = await run_distill_dream(semantic, _llm(fail_distill_for='key="b'))
        statuses = sorted(r.status for r in plan.results)
        assert statuses == sorted([STATUS_APPROVED, STATUS_FAILED])

    async def test_pass_is_read_only(self, db, semantic):
        await _topic(db)
        await _row(db, "hand", 5, source="manual")
        before = await _snapshot(db)
        await run_distill_dream(semantic, _llm())
        assert await _snapshot(db) == before

    async def test_report_lists_the_distillation(self, db, semantic):
        await _topic(db)
        plan, report = await run_distill_dream(semantic, _llm())
        assert plan.pass_id in report
        assert "精煉後的一條" in report
        assert "read-only" in report

    def test_produced_by_metadata_contract(self):
        meta = planned_survivor_metadata(now=NOW)
        assert meta == {"produced_by": "distill_dream", "distilled_at": NOW.isoformat()}
        assert PRODUCED_BY == "distill_dream"


def _facts(user: str) -> list[str]:
    return [line for line in user.splitlines() if line.startswith("[")]


class TestTool:
    async def test_tool_writes_report_and_stays_read_only(self, db, semantic, tmp_path):
        from loom.core.memory.maintenance import make_distill_dream_tool

        await _topic(db)
        before = await _snapshot(db)
        tool = make_distill_dream_tool(semantic, _llm(), dreams_dir=tmp_path / "dreams")
        res = await tool.executor(_call({}))
        assert res.success, res.error
        assert "approved=1" in res.output
        (report,) = list((tmp_path / "dreams").glob("*.md"))
        text = report.read_text()
        assert "· 記憶蒸餾" in text and "精煉後的一條" in text
        assert await _snapshot(db) == before

    async def test_tool_rejects_bad_input(self, semantic, tmp_path):
        from loom.core.memory.maintenance import make_distill_dream_tool

        tool = make_distill_dream_tool(semantic, _llm(), dreams_dir=tmp_path)
        bad_domain = await tool.executor(_call({"domain": "nope"}))
        bad_cap = await tool.executor(_call({"max_neighbourhoods": 0}))
        bad_sim = await tool.executor(_call({"min_similarity": 1.5}))
        assert not bad_domain.success and not bad_cap.success and not bad_sim.success


def _call(args: dict):
    from loom.core.harness.middleware import ToolCall
    from loom.core.harness.permissions import TrustLevel
    return ToolCall(tool_name="distill_dream", args=args,
                    trust_level=TrustLevel.SAFE, session_id="t")


# ── cross-pass suppression of rejected neighbourhoods ─────────────────────

class TestSuppression:
    """A rejected neighbourhood isn't re-planned while it is unchanged, so
    repeated executing passes (the big dream, the 12 h loop) move on to the
    next one. Read-only passes remember nothing."""

    async def test_rejected_neighbourhood_is_not_planned_again(self, db, semantic):
        await _topic(db, "a", 0)
        plan1, _ = await run_distill_dream(semantic, _llm(review="skip"), max_neighbourhoods=1, execute=True)
        (r,) = plan1.results
        assert r.status == STATUS_REJECTED
        plan2, _ = await run_distill_dream(semantic, _llm(), max_neighbourhoods=1)
        assert plan2.neighbourhoods == []
        assert any("suppressed" in n for n in plan2.notes)

    async def test_the_next_neighbourhood_gets_its_turn(self, db, semantic):
        await _topic(db, "a", 0)
        await _topic(db, "b", 180, n=3)       # smaller → planned second
        plan1, _ = await run_distill_dream(semantic, _llm(review="skip"), max_neighbourhoods=1, execute=True)
        seed1 = plan1.neighbourhoods[0].seed_key
        plan2, _ = await run_distill_dream(semantic, _llm(), max_neighbourhoods=1)
        assert plan2.neighbourhoods[0].seed_key[0] != seed1[0]

    async def test_an_edited_member_lifts_the_suppression(self, db, semantic):
        await _topic(db, "a", 0)
        await run_distill_dream(semantic, _llm(review="skip"), max_neighbourhoods=1, execute=True)
        await db.execute("UPDATE semantic_entries SET value='changed', "
                         "updated_at='2099-01-01T00:00:00+00:00' WHERE key='a2'")
        await db.commit()
        plan, _ = await run_distill_dream(semantic, _llm(), max_neighbourhoods=1)
        assert len(plan.neighbourhoods) == 1

    @pytest.mark.parametrize("review", ["defer"])
    async def test_deferred_is_not_suppressed(self, db, semantic, review):
        await _topic(db, "a", 0)
        await run_distill_dream(semantic, _llm(review=review), max_neighbourhoods=1, execute=True)
        plan, _ = await run_distill_dream(semantic, _llm(), max_neighbourhoods=1)
        assert len(plan.neighbourhoods) == 1

    async def test_failed_distill_is_not_suppressed(self, db, semantic):
        await _topic(db, "a", 0)
        await run_distill_dream(semantic, _llm(fail_distill_for='key="a'), max_neighbourhoods=1, execute=True)
        plan, _ = await run_distill_dream(semantic, _llm(), max_neighbourhoods=1)
        assert len(plan.neighbourhoods) == 1

    async def test_convergent_suppression_is_separate(self, db, semantic):
        from loom.core.cognition.consolidation import load_suppressed_signatures
        await _topic(db, "a", 0)
        await run_distill_dream(semantic, _llm(review="skip"), max_neighbourhoods=1, execute=True)
        assert await load_suppressed_signatures(db) == set()

    async def test_read_only_pass_remembers_nothing(self, db, semantic):
        await _topic(db, "a", 0)
        await run_distill_dream(semantic, _llm(review="skip"), max_neighbourhoods=1)
        plan, _ = await run_distill_dream(semantic, _llm(), max_neighbourhoods=1)
        assert len(plan.neighbourhoods) == 1
        cur = await db.execute("SELECT count(*) FROM memory_meta WHERE key LIKE 'distill_dream.%'")
        assert (await cur.fetchone())[0] == 0

    async def test_neighbours_of_a_rejected_set_are_not_regrouped(self, db, semantic):
        # one dense topic: after its densest set is rejected, a neighbouring
        # seed must not rebuild nearly the same set and get re-reviewed
        for i in range(16):                      # denser than one 12-member set
            await _row(db, f"d{i:02d}", i * 1.5)
        first, _ = await run_distill_dream(semantic, _llm(review="skip"),
                                           max_neighbourhoods=1, execute=True)
        rejected = {m.key for m in first.neighbourhoods[0].members}
        assert len(rejected) == 12
        plan, _ = await run_distill_dream(semantic, _llm(), max_neighbourhoods=5)
        for nb in plan.neighbourhoods:
            assert not rejected & {m.key for m in nb.members}
