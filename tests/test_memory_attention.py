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
