"""The agent-loop measurement scores only what the model was actually sent,
and compares ref identities exactly (#650 S1; #662 review)."""
from loom.core.harness.middleware import ToolCall
from loom.core.harness.permissions import TrustLevel
from loom.core.memory.procedural import ProceduralMemory
from loom.core.memory.search import MemorySearch
from loom.core.memory.semantic import SemanticEntry, SemanticMemory
from loom.core.memory.store import SQLiteStore
from loom.platform.cli.tools import make_recall_tool
from scripts.memory_s1_agent_loop import _cited, _Facade, _visible_hit

EXPANSION = (
    "log:s0-build:1 — turn 1 with the turn before and the turn after it\n"
    "── turn 0 [2026-01-01 08:00] no ledger link recorded\nuser: 石橋 測試\n"
    "── turn 1 (this ref) [2026-01-01 08:05] no ledger link recorded\nassistant: 還在查\n"
    "── turn 2 [2026-01-01 08:10] no ledger link recorded\nassistant: 原因是 timezone=UTC"
)


def _gold(ref, *contains):
    return [{"ref": ref, "contains": list(contains)}]


def test_neighbour_turn_evidence_in_an_expansion_counts():
    assert _visible_hit(_gold("log:s0-build:2", "timezone=UTC"), [EXPANSION]) == "ref read"


def test_expansion_evidence_must_be_in_the_right_turn_and_session():
    assert _visible_hit(_gold("log:s0-build:1", "timezone=UTC"), [EXPANSION]) is None
    assert _visible_hit(_gold("log:other:2", "timezone=UTC"), [EXPANSION]) is None
    child = EXPANSION.replace("log:s0-build:1 —", "log:s0-build:child:1 —", 1)
    assert _visible_hit(_gold("log:s0-build:2", "timezone=UTC"), [child]) is None
    assert _visible_hit(_gold("log:s0-build:child:2", "timezone=UTC"), [child]) == "ref read"


def test_turn_numbers_and_refs_compare_exactly():
    shown = "[conversation] [2026-01-01] log:s0-design:20\n  夜間全量驗證 內容雜湊"
    assert _visible_hit(_gold("log:s0-design:2", "夜間全量驗證"), [shown]) is None
    assert _visible_hit(_gold("log:s0-design:20", "夜間全量驗證"), [shown]) == "search"
    assert not _cited("參考 log:s0-design:20", _gold("log:s0-design:2"))
    assert _cited("參考 log:s0-design:2。", _gold("log:s0-design:2"))
    assert _cited("見 mem:s0:batch:limit@1a2b3c4d", _gold("mem:s0:batch:limit"))


async def test_evidence_the_tool_did_not_show_is_not_a_hit(tmp_path):
    """Codex repro: the evidence sits past the 300-char display cut."""
    store = SQLiteStore(str(tmp_path / "m.db"))
    await store.initialize()
    async with store.connect() as db:
        semantic = SemanticMemory(db)
        await semantic.upsert(SemanticEntry(
            key="tail", value="cedar gate " + "x" * 400 + " hidden-evidence"))
        tool = make_recall_tool(_Facade(MemorySearch(semantic, ProceduralMemory(db))))  # type: ignore[arg-type]
        res = await tool.executor(ToolCall(
            tool_name="recall", args={"query": "cedar gate"},
            trust_level=TrustLevel.SAFE, session_id="t"))
        expanded = await tool.executor(ToolCall(
            tool_name="recall", args={"ref": "mem:tail"},
            trust_level=TrustLevel.SAFE, session_id="t"))
    assert res.success and "mem:tail" in res.output and "hidden-evidence" not in res.output
    assert _visible_hit(_gold("mem:tail", "hidden-evidence"), [res.output]) is None
    assert _visible_hit(_gold("mem:tail", "cedar gate"), [res.output]) == "search"
    # Read by ref, the full entry reaches the model: now it counts.
    assert "hidden-evidence" in expanded.output
    assert _visible_hit(_gold("mem:tail", "hidden-evidence"),
                        [res.output, expanded.output]) == "ref read"
