"""Tests for the weekly skill review worker (doc/54 §5 P0-6)."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
import pytest_asyncio

from loom.core.ledger import (
    LedgerEmitter,
    LedgerStore,
    MemoryOpPayload,
    ToolLifecyclePayload,
    TurnEndPayload,
    async_correlation_scope,
    async_turn_scope,
)
from loom.core.skill_review import generate_weekly_report
from loom.core.skill_review.weekly import (
    ATTENTION_ABNORMAL,
    ATTENTION_MUFFLED,
    ATTENTION_STALE,
    ATTENTION_UNDIGESTED,
    ATTENTION_UNUSED,
)


@pytest_asyncio.fixture
async def ledger(tmp_path: Path) -> LedgerStore:
    s = LedgerStore(db_path=tmp_path / "ledger.db", blob_dir=tmp_path / "blobs")
    await s.open()
    try:
        yield s
    finally:
        await s.close()


async def _emit_load_end(
    emitter: LedgerEmitter, *, skill: str, suffix: str
) -> None:
    await emitter.emit_tool_lifecycle(
        payload=ToolLifecyclePayload(
            phase="END",
            tool_name="load_skill",
            tool_call_id=f"call_{suffix}",
            args_digest=f"sha256:{suffix}",
            skill_id=skill,
        ),
        event_id=f"evt_load_end_{suffix}",
    )


async def test_empty_ledger_renders_clean_report(
    ledger: LedgerStore, tmp_path: Path
) -> None:
    output_dir = tmp_path / "out"
    report = await generate_weekly_report(
        ledger, skills_roots=[], output_dir=output_dir, window_days=7,
    )
    assert report.skills_seen == []
    assert report.attention == []
    assert "no skill activity in window" in report.markdown
    assert report.output_path is not None
    assert report.output_path.read_text(encoding="utf-8") == report.markdown


async def test_report_lists_skills_with_loads(
    ledger: LedgerStore, tmp_path: Path
) -> None:
    e = LedgerEmitter(ledger, session_id="sess_a")
    async with async_turn_scope("turn_A"), async_correlation_scope("c_A"):
        await _emit_load_end(e, skill="code_weaver", suffix="1")
    async with async_turn_scope("turn_B"), async_correlation_scope("c_B"):
        await _emit_load_end(e, skill="news_aggregator", suffix="2")
        await _emit_load_end(e, skill="news_aggregator", suffix="3")

    report = await generate_weekly_report(
        ledger, skills_roots=[], output_dir=None, write_to_disk=False,
    )
    assert "code_weaver" in report.skills_seen
    assert "news_aggregator" in report.skills_seen
    assert report.digests["news_aggregator"].load_count == 2
    assert "| code_weaver | 1 |" in report.markdown
    assert "| news_aggregator | 2 |" in report.markdown


async def test_attention_muffled_run(
    ledger: LedgerStore, tmp_path: Path
) -> None:
    """Loaded ≥3 times with zero memory_op feedback → muffled."""
    e = LedgerEmitter(ledger, session_id="sess_a")
    for i in range(4):
        async with (
            async_turn_scope(f"turn_{i}"),
            async_correlation_scope(f"c_{i}"),
        ):
            await _emit_load_end(e, skill="silent_skill", suffix=str(i))

    report = await generate_weekly_report(
        ledger, skills_roots=[], output_dir=None, write_to_disk=False,
    )
    codes_by_skill = {a.skill_id: a.reasons for a in report.attention}
    assert ATTENTION_MUFFLED in codes_by_skill.get("silent_skill", [])


async def test_attention_abnormal_outcome(
    ledger: LedgerStore, tmp_path: Path
) -> None:
    """>30% of outcome-known turns ending abandoned/error → abnormal."""
    e = LedgerEmitter(ledger, session_id="sess_a")
    # 1 clean, 2 error → 2/3 abnormal
    for i, outcome in enumerate(["clean", "error", "error"]):
        async with (
            async_turn_scope(f"turn_{i}"),
            async_correlation_scope(f"c_{i}"),
        ):
            await _emit_load_end(e, skill="flaky", suffix=str(i))
            await e.emit_turn_end(
                payload=TurnEndPayload(
                    outcome=outcome, duration_ms=1, token_usage={},
                ),
            )

    report = await generate_weekly_report(
        ledger, skills_roots=[], output_dir=None, write_to_disk=False,
    )
    codes_by_skill = {a.skill_id: a.reasons for a in report.attention}
    assert ATTENTION_ABNORMAL in codes_by_skill.get("flaky", [])


async def test_attention_exists_but_unused(
    ledger: LedgerStore, tmp_path: Path
) -> None:
    """Skill on disk but no load event → exists_but_unused."""
    skills_root = tmp_path / "skills"
    unused = skills_root / "ghost_skill"
    unused.mkdir(parents=True)
    (unused / "SKILL.md").write_text("# ghost\n")

    report = await generate_weekly_report(
        ledger, skills_roots=[skills_root], output_dir=None, write_to_disk=False,
    )
    codes_by_skill = {a.skill_id: a.reasons for a in report.attention}
    assert ATTENTION_UNUSED in codes_by_skill.get("ghost_skill", [])
    assert "ghost_skill" in report.markdown


async def test_attention_undigested_feedback(
    ledger: LedgerStore, tmp_path: Path
) -> None:
    """Feedback exists but SKILL.md mtime predates first feedback → undigested."""
    skills_root = tmp_path / "skills"
    sd = skills_root / "stale_skill"
    sd.mkdir(parents=True)
    skill_md = sd / "SKILL.md"
    skill_md.write_text("# stale\n")
    # Force mtime to be old
    old_ts = time.time() - 86400 * 10
    import os
    os.utime(skill_md, (old_ts, old_ts))

    e = LedgerEmitter(ledger, session_id="sess_a")
    async with async_turn_scope("turn_A"), async_correlation_scope("c_A"):
        await _emit_load_end(e, skill="stale_skill", suffix="1")
        await e.emit_memory_op(
            payload=MemoryOpPayload(
                operation="write", memory_id="mem1", trust_tier="user_explicit",
            ),
        )

    report = await generate_weekly_report(
        ledger, skills_roots=[skills_root], output_dir=None, write_to_disk=False,
    )
    codes_by_skill = {a.skill_id: a.reasons for a in report.attention}
    assert ATTENTION_UNDIGESTED in codes_by_skill.get("stale_skill", [])


async def test_no_attention_when_skill_is_healthy(
    ledger: LedgerStore, tmp_path: Path
) -> None:
    """One load + one feedback + clean outcome → nothing fires."""
    e = LedgerEmitter(ledger, session_id="sess_a")
    async with async_turn_scope("turn_A"), async_correlation_scope("c_A"):
        await _emit_load_end(e, skill="healthy", suffix="1")
        await e.emit_memory_op(
            payload=MemoryOpPayload(
                operation="write", memory_id="mem1", trust_tier="user_explicit",
            ),
        )
        await e.emit_turn_end(
            payload=TurnEndPayload(outcome="clean", duration_ms=1, token_usage={}),
        )

    report = await generate_weekly_report(
        ledger, skills_roots=[], output_dir=None, write_to_disk=False,
    )
    assert all(a.skill_id != "healthy" for a in report.attention)


async def test_window_filters_correctly(
    ledger: LedgerStore, tmp_path: Path
) -> None:
    """Events older than the window must not appear."""
    e = LedgerEmitter(ledger, session_id="sess_a")
    async with async_turn_scope("turn_A"), async_correlation_scope("c_A"):
        await _emit_load_end(e, skill="old_event", suffix="1")

    # Use now_ts far in the future to push everything out of window.
    future = time.time() + 100 * 86400
    report = await generate_weekly_report(
        ledger,
        skills_roots=[],
        output_dir=None,
        write_to_disk=False,
        now_ts=future,
        window_days=7,
    )
    assert report.skills_seen == []


async def test_markdown_has_expected_sections(
    ledger: LedgerStore, tmp_path: Path
) -> None:
    e = LedgerEmitter(ledger, session_id="sess_a")
    async with async_turn_scope("turn_A"), async_correlation_scope("c_A"):
        await _emit_load_end(e, skill="x", suffix="1")

    report = await generate_weekly_report(
        ledger, skills_roots=[], output_dir=None, write_to_disk=False,
    )
    assert "# Skill Weekly Review" in report.markdown
    assert "## 該關注清單" in report.markdown
    assert "## 技能活動 — 本週載入次數" in report.markdown
