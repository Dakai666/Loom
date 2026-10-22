"""#635 B+C: the agent's own attention list and the Hook G source swap.

Contracts (docs/designs/58 §4):
- one list, replaced whole, ≤5 items, empty legal, atomic on any invalid input
- prepared/shelved never touch the fact's value, confidence, domain or source
- keep = recent → milestone; let_go only undoes a keep this flow made
- Hook G with the list on: prepared + still-valid only, silent when empty,
  never falls back to the old milestone top-3; Hook A stays independent
- the attend tool's own trace never becomes compression input
"""

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

from loom.core.harness.middleware import ToolCall
from loom.core.harness.permissions import TrustLevel
from loom.core.memory.attention import (
    MAX_ITEMS,
    AttentionError,
    AttentionList,
)
from loom.core.memory.contradiction import ConflictType, Contradiction, Resolution
from loom.core.memory.pulse import (
    PULSE_TYPE_CONTRADICTION,
    PULSE_TYPE_SESSION_BRIEF,
    MemoryPulse,
)
from loom.core.memory.semantic import SemanticEntry, SemanticMemory
from loom.core.memory.store import SQLiteStore
from loom.core.session import records_episodic_trace
from loom.platform.cli.tools import make_memory_attend_tool


@pytest_asyncio.fixture
async def mem(tmp_path):
    store = SQLiteStore(str(tmp_path / "attention.db"))
    await store.initialize()
    async with store.connect() as db:
        semantic = SemanticMemory(db)
        for key, value, temporal in (
            ("taste", "Taste forms through repeated small choices", "recent"),
            ("cdc", "The CDC conjecture still lacks a counterexample", "recent"),
            ("old_anchor", "DK chose SQLite WAL for memory", "milestone"),
            ("other", "An unrelated project fact", "recent"),
        ):
            await semantic.upsert(SemanticEntry(
                key=key, value=value, confidence=0.8, source="session:s0",
                domain="project", temporal=temporal,
            ))
        yield db, semantic, AttentionList(db, semantic)


def _item(key, stance="prepared", reason="still thinking about it", **kw):
    return {"key": key, "stance": stance, "reason": reason, **kw}


async def _row(db, key):
    cur = await db.execute(
        "SELECT value, confidence, domain, source, temporal, updated_at "
        "FROM semantic_entries WHERE key = ?", (key,),
    )
    return await cur.fetchone()


# ── list write contract ────────────────────────────────────────────────────


async def test_empty_by_default_and_full_replace(mem):
    _, _, att = mem
    assert await att.read() == []
    await att.write(items=[_item("taste"), _item("cdc", stance="shelved")])
    assert [(i.key, i.stance) for i in await att.read()] == [
        ("taste", "prepared"), ("cdc", "shelved"),
    ]
    await att.write(items=[_item("cdc")])
    assert [(i.key, i.stance) for i in await att.read()] == [("cdc", "prepared")]
    await att.write(items=[])
    assert await att.read() == []


@pytest.mark.parametrize("bad", [
    [_item("taste")] * 2,                                  # duplicate key
    [_item("missing")],                                    # unknown fact
    [_item("taste", stance="loved")],                      # bad stance
    [_item("taste", reason="  ")],                         # reason required
    [_item(f"k{i}") for i in range(MAX_ITEMS + 1)],        # over cap
])
async def test_invalid_write_is_atomic(mem, bad):
    db, _, att = mem
    await att.write(items=[_item("cdc")], keep=[])
    before = await _row(db, "taste")
    with pytest.raises(AttentionError):
        await att.write(items=bad, keep=["taste"])
    assert [i.key for i in await att.read()] == ["cdc"]
    assert await _row(db, "taste") == before                # keep not applied


async def test_list_never_touches_fact_fields(mem):
    db, _, att = mem
    before = {k: await _row(db, k) for k in ("taste", "cdc")}
    await att.write(items=[_item("taste"), _item("cdc", stance="shelved")])
    await att.write(items=[])
    assert {k: await _row(db, k) for k in ("taste", "cdc")} == before


async def test_redirect_stub_cannot_be_listed(mem):
    _, semantic, att = mem
    entries = [await semantic.get("taste"), await semantic.get("other")]
    await semantic.consolidate(
        entries, survivor_key="taste", refined_value="merged taste",
        sacrificed_content="x", merge_rationale="test",
    )
    with pytest.raises(AttentionError):
        await att.write(items=[_item("other")])


# ── keep / let_go = milestone promotion owned by this flow ─────────────────


async def test_keep_promotes_without_changing_content(mem):
    db, _, att = mem
    value, conf, domain, source, temporal, updated = await _row(db, "taste")
    assert temporal == "recent"
    await att.write(keep=["taste"])
    assert await _row(db, "taste") == (value, conf, domain, source, "milestone", updated)


async def test_let_go_only_undoes_own_keep(mem):
    db, _, att = mem
    await att.write(keep=["taste", "old_anchor"])   # old_anchor already milestone
    await att.write(let_go=["taste", "old_anchor"])
    assert (await _row(db, "taste"))[4] == "recent"
    assert (await _row(db, "old_anchor"))[4] == "milestone"


async def test_removing_from_list_does_not_demote(mem):
    db, _, att = mem
    await att.write(items=[_item("taste")], keep=["taste"])
    await att.write(items=[])
    assert (await _row(db, "taste"))[4] == "milestone"


async def test_keep_and_let_go_same_key_rejected(mem):
    _, _, att = mem
    with pytest.raises(AttentionError):
        await att.write(keep=["taste"], let_go=["taste"])


# ── Hook G source swap ─────────────────────────────────────────────────────


def _pulse(db, semantic, att, buffer):
    return MemoryPulse(
        db=db, semantic=semantic, session_id="s-new",
        session_started_at=datetime.now(UTC), pending_buffer=buffer,
        attention=att,
    )


async def _seed_prior_session(db):
    await db.execute(
        "INSERT INTO sessions (session_id, model, started_at, last_active) "
        "VALUES ('s-old', 'm', ?, ?)",
        ((datetime.now(UTC) - timedelta(days=1)).isoformat(),
         datetime.now(UTC).isoformat()),
    )
    await db.commit()


async def test_brief_uses_only_valid_prepared_items(mem):
    db, semantic, att = mem
    await _seed_prior_session(db)
    await att.write(items=[
        _item("taste", reason="my own thread"),
        _item("cdc", stance="shelved"),
        _item("other", expires_days=1),
    ])
    # Two days later "other" has expired; nothing else in the list leaks.
    later = AttentionList(
        db, semantic, clock=lambda: datetime.now(UTC) + timedelta(days=2),
    )
    buffer = []
    await _pulse(db, semantic, later, buffer).session_brief()
    assert len(buffer) == 1
    record = buffer[0]
    assert record.pulse_type == PULSE_TYPE_SESSION_BRIEF
    assert "taste" in record.text and "my own thread" in record.text
    assert "cdc" not in record.text and "other" not in record.text
    assert "old_anchor" not in record.text          # old top-3 never used


async def test_brief_skips_changed_or_redirected_source(mem):
    db, semantic, att = mem
    await att.write(items=[_item("taste"), _item("cdc")])
    await semantic.upsert(SemanticEntry(
        key="cdc", value="CDC conjecture rewritten", confidence=0.8,
        source="session:s0", domain="project",
    ))
    await semantic.consolidate(
        [await semantic.get("other"), await semantic.get("taste")],
        survivor_key="other", refined_value="merged", sacrificed_content="x",
        merge_rationale="test",
    )
    buffer = []
    await _pulse(db, semantic, att, buffer).session_brief()
    assert buffer == []


async def test_brief_silent_when_list_empty_even_with_milestones(mem):
    db, semantic, att = mem
    await _seed_prior_session(db)
    buffer = []
    await _pulse(db, semantic, att, buffer).session_brief()
    assert buffer == []


async def test_list_written_mid_session_lands_next_session(mem):
    db, semantic, att = mem
    buffer = []
    pulse = _pulse(db, semantic, att, buffer)
    await pulse.session_brief()
    await att.write(items=[_item("taste")])
    assert buffer == []                              # one brief per session, at start
    nxt = []
    await _pulse(db, semantic, att, nxt).session_brief()
    assert len(nxt) == 1


async def test_hook_a_independent_of_attention_brief(mem):
    db, semantic, att = mem
    await att.write(items=[_item("taste")])
    buffer = []
    pulse = _pulse(db, semantic, att, buffer)
    await pulse.session_brief()
    existing = await semantic.get("cdc")
    await pulse.contradiction_inject(Contradiction(
        existing=existing,
        proposed=SemanticEntry(key="cdc", value="new", confidence=0.8),
        conflict_type=ConflictType.KEY_MATCH,
        resolution=Resolution.SUPERSEDE,
    ))
    assert [r.pulse_type for r in buffer] == [
        PULSE_TYPE_SESSION_BRIEF, PULSE_TYPE_CONTRADICTION,
    ]


async def test_separate_db_has_separate_list(mem, tmp_path):
    _, _, att = mem
    await att.write(items=[_item("taste")])
    store = SQLiteStore(str(tmp_path / "other-agent.db"))
    await store.initialize()
    async with store.connect() as db2:
        assert await AttentionList(db2, SemanticMemory(db2)).read() == []


# ── tool path + compression exclusion ──────────────────────────────────────


async def test_tool_writes_and_shows_list_with_diff(mem):
    db, _, att = mem
    tool = make_memory_attend_tool(att)
    assert tool.trust_level == TrustLevel.SAFE

    def call(args):
        return tool.executor(ToolCall(
            id="c", tool_name="memory_attend", trust_level=TrustLevel.SAFE,
            session_id="s", args=args,
        ))

    shown = await call({})
    assert shown.success and "empty" in shown.output.lower()
    wrote = await call({"items": [_item("taste")], "keep": ["taste"]})
    assert wrote.success
    assert "+ taste" in wrote.output and "kept: taste" in wrote.output
    bad = await call({"items": [_item("missing")]})
    assert not bad.success
    assert [i.key for i in await att.read()] == ["taste"]


def test_attend_trace_is_not_compression_input():
    assert records_episodic_trace("memory_attend") is False
    assert records_episodic_trace("recall") is True


# ── serialization with dream rewrites (Codex review on #637) ───────────────


async def test_write_waits_for_running_dream_rewrite_then_sees_the_merge(mem):
    """A dream holding the rewrite lock merges the fact first; the queued
    keep then validates against the stub and refuses — nothing changes."""
    import asyncio

    from loom.core.cognition.consolidation import memory_rewrite_lock

    db, semantic, att = mem
    holding, release = asyncio.Event(), asyncio.Event()

    async def dream():
        async with memory_rewrite_lock():
            holding.set()
            await release.wait()
            await semantic.consolidate(
                [await semantic.get("cdc"), await semantic.get("taste")],
                survivor_key="cdc", refined_value="merged", sacrificed_content="x",
                merge_rationale="dream",
            )

    dream_task = asyncio.create_task(dream())
    await holding.wait()
    attend_task = asyncio.create_task(att.write(keep=["taste"]))
    await asyncio.sleep(0.01)
    assert not attend_task.done()                    # blocked behind the dream
    release.set()
    await dream_task
    with pytest.raises(AttentionError):
        await attend_task
    taste = await semantic.get("taste")
    assert taste.temporal == "recent" and "kept_by" not in taste.metadata
    assert (await semantic.get("cdc")).temporal == "recent"


async def test_keep_rejects_whole_write_if_fact_merged_after_validation(mem, monkeypatch):
    """Belt and braces for a writer outside the lock: a real merge landing
    between validation and the UPDATE must not promote the stub."""
    db, semantic, att = mem
    await att.write(items=[_item("cdc")])
    original = att._live_fact

    async def validate_then_merge(key, what):
        entry = await original(key, what)
        if key == "taste":
            await semantic.consolidate(
                [await semantic.get("other"), await semantic.get("taste")],
                survivor_key="other", refined_value="merged",
                sacrificed_content="x", merge_rationale="race",
            )
        return entry

    monkeypatch.setattr(att, "_live_fact", validate_then_merge)
    with pytest.raises(AttentionError):
        # taste is validated once (keep), after the list — the merge lands
        # between that last check and the UPDATE.
        await att.write(items=[_item("cdc", stance="shelved")], keep=["taste"])
    stub = await semantic.get("taste")
    assert stub.metadata.get("redirected_to") == "other"
    assert stub.temporal == "recent" and "kept_by" not in stub.metadata
    assert [(i.key, i.stance) for i in await att.read()] == [("cdc", "prepared")]  # list untouched too


async def test_let_go_rejects_if_ownership_changed_after_validation(mem, monkeypatch):
    db, semantic, att = mem
    await att.write(keep=["taste"])
    original = att._live_fact

    async def validate_then_take_over(key, what):
        entry = await original(key, what)
        await db.execute(
            "UPDATE semantic_entries SET metadata = json_remove(metadata, '$.kept_by') "
            "WHERE key = ?", (key,),
        )
        return entry

    monkeypatch.setattr(att, "_live_fact", validate_then_take_over)
    with pytest.raises(AttentionError):
        await att.write(let_go=["taste"])
    assert (await semantic.get("taste")).temporal == "milestone"


# ── long-unvisited sample (the way in for memories with no cue) ────────────


@pytest_asyncio.fixture
async def aged(mem):
    """Back-date the fixture facts and add a few that must never be sampled."""
    db, semantic, att = mem
    for key, value, source in (
        ("dream_triple", "A dreaming hypothesis", "dreaming"),
        ("fresh", "Written yesterday, never read", "memorize"),
        ("read_lately", "Old but read last week", "memorize"),
    ):
        await semantic.upsert(SemanticEntry(key=key, value=value, source=source))
    old = (datetime.now(UTC) - timedelta(days=90)).isoformat()
    await db.execute(
        "UPDATE semantic_entries SET created_at = ?, last_accessed_at = NULL "
        "WHERE key != 'fresh'", (old,),
    )
    await db.execute(
        "UPDATE semantic_entries SET last_accessed_at = ? WHERE key = 'read_lately'",
        ((datetime.now(UTC) - timedelta(days=7)).isoformat(),),
    )
    await db.commit()
    await semantic.consolidate(                       # "other" becomes a stub
        [await semantic.get("cdc"), await semantic.get("other")],
        survivor_key="cdc", refined_value="merged cdc", sacrificed_content="x",
        merge_rationale="test",
    )
    await db.execute("UPDATE semantic_entries SET created_at = ?", (old,))
    await db.execute(
        "UPDATE semantic_entries SET created_at = ? WHERE key = 'fresh'",
        ((datetime.now(UTC) - timedelta(days=1)).isoformat(),),
    )
    await db.commit()
    return db, semantic, att


async def test_unvisited_sample_only_long_unseen_live_non_dream(aged):
    _, _, att = aged
    seen = set()
    for _ in range(20):
        seen |= {e.key for e in await att.unvisited_sample(n=10)}
    assert seen == {"taste", "cdc", "old_anchor"}


async def test_unvisited_sample_skips_listed_and_caps_n(aged):
    _, _, att = aged
    await att.write(items=[_item("taste")])
    for _ in range(10):
        sample = await att.unvisited_sample(n=1)
        assert len(sample) == 1 and sample[0].key != "taste"


async def test_unvisited_sample_does_not_mark_accessed(aged):
    db, semantic, att = aged
    await att.unvisited_sample(n=10)
    cur = await db.execute(
        "SELECT COUNT(*) FROM semantic_entries WHERE last_accessed_at IS NOT NULL",
    )
    assert (await cur.fetchone())[0] == 1             # only read_lately


async def test_tool_view_shows_unvisited_sample(aged):
    _, _, att = aged
    tool = make_memory_attend_tool(att)
    shown = await tool.executor(ToolCall(
        id="v", tool_name="memory_attend", trust_level=TrustLevel.SAFE,
        session_id="s", args={},
    ))
    assert "Not looked at in a long while" in shown.output
    assert "(session)" in shown.output               # origin shown: hand vs machine
    assert "dream_triple" not in shown.output and "fresh" not in shown.output


# ── Loom Agent's first-use feedback (2026-10-22) ───────────────────────────


@pytest_asyncio.fixture
async def mixed_pool(mem):
    """Many old machine facts, a few old hand-written ones."""
    db, semantic, att = mem
    for i in range(40):
        await semantic.upsert(SemanticEntry(
            key=f"machine:{i}", value=f"extracted {i}", source=f"session:x{i}",
        ))
    for i in range(4):
        await semantic.upsert(SemanticEntry(
            key=f"hand:{i}", value=f"my own judgement {i}", source="memorize",
        ))
    old = (datetime.now(UTC) - timedelta(days=90)).isoformat()
    await db.execute(
        "UPDATE semantic_entries SET created_at = ?, last_accessed_at = NULL", (old,),
    )
    await db.commit()
    return db, semantic, att


async def test_unvisited_sample_always_includes_hand_written_when_available(mixed_pool):
    _, _, att = mixed_pool
    for _ in range(10):
        sample = await att.unvisited_sample(n=5)
        hand = [e for e in sample if e.key.startswith("hand:")]
        assert len(sample) == 5
        assert len(hand) == 3                         # 3 hand + 2 other


async def test_unvisited_sample_fills_from_machine_when_hand_runs_short(aged):
    _, _, att = aged                                  # no hand-written in pool
    sample = await att.unvisited_sample(n=5)
    assert len(sample) == 3                           # whole eligible pool


async def test_relisting_keeps_hash_and_expiry_so_a_rewrite_stays_visible(mem):
    _, semantic, att = mem
    await att.write(items=[_item("taste", expires_days=3)])
    first = (await att.read())[0]
    await semantic.upsert(SemanticEntry(
        key="taste", value="Taste rewritten by someone", source="session:s0",
    ))
    # Rewriting the list (new reason, another item) must not launder it.
    await att.write(items=[_item("taste", reason="still mine"), _item("cdc")])
    carried = [i for i in await att.read() if i.key == "taste"][0]
    assert carried.reason == "still mine"
    assert (carried.value_hash, carried.expires_at, carried.set_at) == (
        first.value_hash, first.expires_at, first.set_at,
    )
    assert (await att.status(carried))[0] == "changed"


async def test_explicit_expires_days_renews_and_rehashes(mem):
    _, semantic, att = mem
    later = AttentionList(
        att._db, semantic, clock=lambda: datetime.now(UTC) + timedelta(days=10),
    )
    await att.write(items=[_item("taste", expires_days=1)])
    assert (await later.status((await later.read())[0]))[0] == "expired"
    await later.write(items=[_item("taste", expires_days=7)])
    assert (await later.status((await later.read())[0]))[0] == "live"


async def test_tool_view_explains_item_state_in_plain_words(mem):
    _, semantic, att = mem
    tool = make_memory_attend_tool(att)

    async def view():
        return (await tool.executor(ToolCall(
            id="v", tool_name="memory_attend", trust_level=TrustLevel.SAFE,
            session_id="s", args={},
        ))).output

    await att.write(items=[_item("taste")])
    assert "fact unchanged" in await view()
    assert "source live" not in await view()
    await semantic.upsert(SemanticEntry(key="taste", value="rewritten", source="x"))
    assert "fact rewritten since you listed it" in await view()


async def test_stale_carried_item_does_not_block_rewriting_the_list(mem):
    """A listed fact merged away later must not make every rewrite fail;
    it rides along as 'merged' until the agent drops or replaces it."""
    _, semantic, att = mem
    await att.write(items=[_item("taste")])
    await semantic.consolidate(
        [await semantic.get("cdc"), await semantic.get("taste")],
        survivor_key="cdc", refined_value="merged", sacrificed_content="x",
        merge_rationale="test",
    )
    await att.write(items=[_item("taste"), _item("old_anchor")])
    items = {i.key: i for i in await att.read()}
    assert (await att.status(items["taste"]))[0] == "merged"
    with pytest.raises(AttentionError):               # renewing a stub is refused
        await att.write(items=[_item("taste", expires_days=7)])


async def test_subagent_and_case_variant_sources_count_as_hand_written(mem):
    db, semantic, att = mem
    await db.execute("DELETE FROM semantic_entries")
    for key, source in (("sub", "agent:abc123"), ("caps", "Memorize"),
                        ("m1", "session:a"), ("m2", "session:b")):
        await semantic.upsert(SemanticEntry(key=key, value=key, source=source))
    old = (datetime.now(UTC) - timedelta(days=90)).isoformat()
    await db.execute("UPDATE semantic_entries SET created_at = ?", (old,))
    await db.commit()
    for _ in range(10):
        sample = await att.unvisited_sample(n=3)
        assert {"sub", "caps"} <= {e.key for e in sample}
