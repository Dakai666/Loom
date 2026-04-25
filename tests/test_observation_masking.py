"""
Tests for observation masking (Issue #197 Phase 2).

Masking folds stale tool observations into scratchpad references when:
  - The entry is older than ``_mask_age_turns`` turns
  - The same tool has been called more recently (supersession)
  - The entry isn't already a JIT placeholder
  - The entry isn't already masked

Tests use a duck-typed stand-in for LoomSession so the method's logic is
exercised without bringing up a full session (DB, memory, router, etc.).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from loom.core.jobs.scratchpad import Scratchpad
from loom.core.session import LoomSession


def _mock_session(
    messages: list[dict[str, Any]],
    *,
    turn_index: int,
    mask_age_turns: int = 20,
    scratchpad: Scratchpad | None = None,
) -> SimpleNamespace:
    """Build a minimal LoomSession stand-in with just the attrs the method reads."""
    return SimpleNamespace(
        messages=messages,
        _turn_index=turn_index,
        _scratchpad=scratchpad if scratchpad is not None else Scratchpad(),
        _mask_age_turns=mask_age_turns,
    )


def _run_mask(session: SimpleNamespace) -> None:
    """Invoke the unbound method against the mock session."""
    LoomSession._apply_observation_masking(session)


def _tool_msg(
    tool_call_id: str, content: str, tool_name: str, emit_turn: int,
    *, masked: bool = False,
) -> dict[str, Any]:
    msg: dict[str, Any] = {
        "role": "tool",
        "tool_call_id": tool_call_id,
        "content": content,
        "_emit_turn": emit_turn,
        "_tool_name": tool_name,
    }
    if masked:
        msg["_masked"] = True
    return msg


class TestMaskingTriggers:
    """Both age AND supersession must hold for masking to fire."""

    async def test_old_and_superseded_gets_folded(self) -> None:
        """Tool called at turn 0 and again at turn 25 → first call is
        old (age=30 > threshold 20) AND superseded → fold."""
        scratchpad = Scratchpad()
        messages = [
            {"role": "user", "content": "search for X"},
            _tool_msg("c1", "old fetch_url body — 5000 chars of HTML",
                      "fetch_url", emit_turn=0),
            {"role": "user", "content": "search for Y"},
            _tool_msg("c2", "newer fetch_url body — 3000 chars",
                      "fetch_url", emit_turn=25),
        ]
        session = _mock_session(messages, turn_index=30, scratchpad=scratchpad)

        _run_mask(session)

        # First fetch_url is masked (age=30, superseded)
        first = messages[1]
        assert first.get("_masked") is True
        assert "observation folded" in first["content"]
        assert "fetch_url from 30 turns ago" in first["content"]

        # Second fetch_url survives — most recent call
        second = messages[3]
        assert "_masked" not in second
        assert second["content"] == "newer fetch_url body — 3000 chars"

        # Original content lives in scratchpad
        refs = scratchpad.list_refs()
        assert len(refs) == 1
        assert refs[0].startswith("masked_fetch_url_")
        assert "old fetch_url body" in scratchpad.read(refs[0])

    async def test_old_but_not_superseded_preserved(self) -> None:
        """A tool called only once stays inline regardless of age — no
        supersession means the agent likely still needs it."""
        scratchpad = Scratchpad()
        messages = [
            {"role": "user", "content": "do a thing"},
            _tool_msg("c1", "the only fetch_url body", "fetch_url", emit_turn=0),
            {"role": "user", "content": "follow up"},
            _tool_msg("c2", "different tool", "list_dir", emit_turn=10),
        ]
        session = _mock_session(messages, turn_index=30, scratchpad=scratchpad)

        _run_mask(session)

        # fetch_url only called once → preserved
        assert messages[1]["content"] == "the only fetch_url body"
        assert "_masked" not in messages[1]
        assert scratchpad.list_refs() == []

    async def test_recent_calls_preserved_regardless_of_supersession(self) -> None:
        """Both calls within threshold → both inline, even though one is
        superseded by the other."""
        scratchpad = Scratchpad()
        messages = [
            _tool_msg("c1", "first call", "fetch_url", emit_turn=18),
            _tool_msg("c2", "second call", "fetch_url", emit_turn=19),
        ]
        session = _mock_session(messages, turn_index=20, scratchpad=scratchpad)

        _run_mask(session)

        assert "_masked" not in messages[0]
        assert "_masked" not in messages[1]
        assert scratchpad.list_refs() == []


class TestMaskingSkipPaths:
    """Cases where masking is the wrong move — must skip cleanly."""

    async def test_disabled_when_threshold_zero(self) -> None:
        scratchpad = Scratchpad()
        messages = [
            _tool_msg("c1", "ancient call", "fetch_url", emit_turn=0),
            _tool_msg("c2", "newer call", "fetch_url", emit_turn=99),
        ]
        session = _mock_session(
            messages, turn_index=100, mask_age_turns=0, scratchpad=scratchpad,
        )

        _run_mask(session)

        assert "_masked" not in messages[0]
        assert "_masked" not in messages[1]

    async def test_jit_placeholder_skipped(self) -> None:
        """Already-spilled JIT entries are minimal already — re-folding
        them just wastes a scratchpad ref."""
        scratchpad = Scratchpad()
        jit_placeholder = (
            "[tool output spilled to scratchpad — 12000 chars]\n"
            "  tool: fetch_url\n"
            "  ref:  scratchpad:auto_fetch_url_abc123\n"
            "  ..."
        )
        messages = [
            _tool_msg("c1", jit_placeholder, "fetch_url", emit_turn=0),
            _tool_msg("c2", "newer call", "fetch_url", emit_turn=25),
        ]
        session = _mock_session(messages, turn_index=30, scratchpad=scratchpad)

        _run_mask(session)

        # JIT placeholder kept as-is, not re-spilled
        assert "_masked" not in messages[0]
        assert messages[0]["content"] == jit_placeholder
        assert scratchpad.list_refs() == []

    async def test_untagged_messages_skipped(self) -> None:
        """Messages without _emit_turn / _tool_name (e.g. from older
        sessions on disk) must not break masking — just skip."""
        scratchpad = Scratchpad()
        messages = [
            # Untagged — no _emit_turn, no _tool_name
            {"role": "tool", "tool_call_id": "c1", "content": "untagged"},
            _tool_msg("c2", "tagged but only call", "fetch_url", emit_turn=0),
        ]
        session = _mock_session(messages, turn_index=30, scratchpad=scratchpad)

        _run_mask(session)  # must not raise

        assert messages[0]["content"] == "untagged"
        # tagged but only call → preserved (not superseded)
        assert "_masked" not in messages[1]

    async def test_already_masked_idempotent(self) -> None:
        """Running masking twice produces the same result; no double-spill."""
        scratchpad = Scratchpad()
        messages = [
            _tool_msg("c1", "old", "fetch_url", emit_turn=0),
            _tool_msg("c2", "newer", "fetch_url", emit_turn=25),
        ]
        session = _mock_session(messages, turn_index=30, scratchpad=scratchpad)

        _run_mask(session)
        first_pass_refs = list(scratchpad.list_refs())
        first_pass_content = messages[0]["content"]

        _run_mask(session)
        assert list(scratchpad.list_refs()) == first_pass_refs
        assert messages[0]["content"] == first_pass_content


class TestMaskingPlaceholderShape:
    """The placeholder is the agent's signal — its shape matters."""

    async def test_placeholder_includes_tool_name_age_and_ref(self) -> None:
        scratchpad = Scratchpad()
        messages = [
            _tool_msg("c1", "old body", "fetch_url", emit_turn=0),
            _tool_msg("c2", "newer body", "fetch_url", emit_turn=25),
        ]
        session = _mock_session(messages, turn_index=30, scratchpad=scratchpad)

        _run_mask(session)

        placeholder = messages[0]["content"]
        assert "fetch_url" in placeholder
        assert "30 turns ago" in placeholder
        assert "scratchpad_read" in placeholder
        # Ref appears in placeholder and matches what's in scratchpad
        ref = scratchpad.list_refs()[0]
        assert ref in placeholder


class TestMaskingFailureBehavior:
    """Scratchpad write failures must not break the session."""

    async def test_scratchpad_failure_keeps_inline(self) -> None:
        class _FailingScratchpad:
            def write(self, ref, content):
                raise RuntimeError("disk full")

            def read(self, ref):
                raise KeyError(ref)

            def list_refs(self):
                return []

        messages = [
            _tool_msg("c1", "old body", "fetch_url", emit_turn=0),
            _tool_msg("c2", "newer body", "fetch_url", emit_turn=25),
        ]
        session = _mock_session(
            messages, turn_index=30, scratchpad=_FailingScratchpad(),
        )

        _run_mask(session)  # must not raise

        # First call kept inline — masking degraded gracefully
        assert messages[0]["content"] == "old body"
        assert "_masked" not in messages[0]


class TestMaskingMixedScenario:
    """A realistic research session: many fetches, only some folded."""

    async def test_research_session_folds_correctly(self) -> None:
        """Simulates 5 fetch_url calls + 3 web_search calls across many
        turns. Old superseded calls fold; latest of each tool stays."""
        scratchpad = Scratchpad()
        messages = [
            # Turn 0–5: data collection burst
            _tool_msg("c1", "fetch A body", "fetch_url", emit_turn=0),
            _tool_msg("c2", "search for foo", "web_search", emit_turn=1),
            _tool_msg("c3", "fetch B body", "fetch_url", emit_turn=2),
            _tool_msg("c4", "search for bar", "web_search", emit_turn=3),
            _tool_msg("c5", "fetch C body", "fetch_url", emit_turn=4),
            # Turn 25+: synthesis phase, latest fetch
            _tool_msg("c6", "search for baz", "web_search", emit_turn=25),
            _tool_msg("c7", "fetch D body", "fetch_url", emit_turn=26),
        ]
        session = _mock_session(messages, turn_index=30, scratchpad=scratchpad)

        _run_mask(session)

        # First three fetch_url calls (old + superseded) → masked
        for idx in [0, 2, 4]:
            assert messages[idx].get("_masked") is True, f"index {idx}"

        # First two web_search calls (old + superseded by c6) → masked
        for idx in [1, 3]:
            assert messages[idx].get("_masked") is True, f"index {idx}"

        # Latest web_search and fetch_url → preserved
        assert "_masked" not in messages[5]
        assert "_masked" not in messages[6]

        # Five entries spilled to scratchpad
        assert len(scratchpad.list_refs()) == 5
