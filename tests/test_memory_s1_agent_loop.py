"""The agent-loop measurement must credit evidence shown via ref expansion,
and only for the right turn of the right session (#650 S1)."""
from scripts.memory_s1_agent_loop import _expansion_shows

EXPANSION = (
    "log:s0-build:1 — turn 1 with the turn before and the turn after it\n"
    "── turn 0 [2026-01-01 08:00] no ledger link recorded\nuser: 石橋 測試\n"
    "── turn 1 (this ref) [2026-01-01 08:05] no ledger link recorded\nassistant: 還在查\n"
    "── turn 2 [2026-01-01 08:10] no ledger link recorded\nassistant: 原因是 timezone=UTC"
)


def test_neighbour_turn_evidence_counts():
    assert _expansion_shows({"ref": "log:s0-build:2", "contains": ["timezone=UTC"]}, [EXPANSION])


def test_evidence_in_another_turn_or_session_does_not_count():
    assert not _expansion_shows({"ref": "log:s0-build:1", "contains": ["timezone=UTC"]}, [EXPANSION])
    assert not _expansion_shows({"ref": "log:other:2", "contains": ["timezone=UTC"]}, [EXPANSION])
    assert not _expansion_shows({"ref": "mem:x", "contains": ["UTC"]}, [EXPANSION])
