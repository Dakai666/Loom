"""
Tests for ``loom.core.infra.telemetry`` — Issue #142.

Covers:
- Each dimension's record/snapshot/render contract
- Anomaly detection thresholds (no false positives below the sample floor)
- AgentTelemetryTracker persistence round-trip
- Graceful degradation for unknown dimension names
"""

import json
import pytest
import pytest_asyncio

from loom.core.infra.telemetry import (
    AgentTelemetryTracker,
    ContextLayoutDimension,
    DEFAULT_DIMENSIONS,
    MemoryCompressionDimension,
    ToolCallDimension,
    _build_dimension,
)
from loom.core.memory.store import SQLiteStore


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def store(tmp_path):
    s = SQLiteStore(str(tmp_path / "telemetry.db"))
    await s.initialize()
    return s


# ---------------------------------------------------------------------------
# ToolCallDimension
# ---------------------------------------------------------------------------

def test_tool_call_records_success_and_failure():
    dim = ToolCallDimension()
    dim.record("recall", success=True, duration_ms=12.0)
    dim.record("recall", success=False, duration_ms=30.0, error_msg="boom")

    snap = dim.snapshot()
    assert snap["tools"]["recall"]["success"] == 1
    assert snap["tools"]["recall"]["failure"] == 1
    assert snap["tools"]["recall"]["last_failure_msg"] == "boom"
    assert snap["tools"]["recall"]["avg_latency_ms"] == 21.0  # (12+30)/2


def test_tool_call_summary_zero_state():
    dim = ToolCallDimension()
    assert "no calls" in dim.render_summary()


def test_tool_call_anomaly_requires_min_samples():
    """Single failure on 2 calls — shouldn't fire."""
    dim = ToolCallDimension()
    dim.record("recall", success=False, duration_ms=1.0, error_msg="x")
    dim.record("recall", success=False, duration_ms=1.0, error_msg="x")
    assert dim.has_anomaly() is False
    assert dim.describe_anomaly() is None


def test_tool_call_anomaly_fires_above_threshold():
    """5 calls, 4 failures → 80% rate > 30% threshold → anomaly fires."""
    dim = ToolCallDimension()
    dim.record("recall", success=True, duration_ms=1.0)
    for _ in range(4):
        dim.record("recall", success=False, duration_ms=1.0, error_msg="db-down")
    assert dim.has_anomaly() is True
    desc = dim.describe_anomaly()
    assert desc is not None
    assert "recall" in desc


# ---------------------------------------------------------------------------
# MemoryCompressionDimension
# ---------------------------------------------------------------------------

def test_memory_compression_yield_math():
    dim = MemoryCompressionDimension()
    dim.record(entries=10, facts=4)
    dim.record(entries=20, facts=6)

    snap = dim.snapshot()
    assert snap["runs"] == 2
    assert snap["entries_total"] == 30
    assert snap["facts_total"] == 10
    # overall = 10/30 = 0.333; recent = (0.4 + 0.3) / 2 = 0.35
    assert abs(snap["overall_yield"] - 0.333) < 0.01
    assert abs(dim.recent_yield - 0.35) < 0.01


def test_memory_compression_anomaly_needs_min_runs():
    """Even with 0% yield, first 2 runs should not fire anomaly."""
    dim = MemoryCompressionDimension()
    dim.record(entries=10, facts=0)
    dim.record(entries=10, facts=0)
    assert dim.has_anomaly() is False


def test_memory_compression_anomaly_fires_low_yield():
    dim = MemoryCompressionDimension()
    for _ in range(3):
        dim.record(entries=20, facts=1)  # 5% yield
    assert dim.has_anomaly() is True
    assert "compression yield" in dim.describe_anomaly()


def test_memory_compression_rolling_window_bounded():
    dim = MemoryCompressionDimension()
    for _ in range(15):
        dim.record(entries=10, facts=5)
    # window capped at 10
    assert len(dim._recent_yields) == 10


# ---------------------------------------------------------------------------
# ContextLayoutDimension
# ---------------------------------------------------------------------------

class _StubLayer:
    def __init__(self, name: str, content: str) -> None:
        self.name = name
        self.content = content


class _StubStack:
    def __init__(self, layers):
        self._layers = layers


def test_context_layout_attributes_by_char_share():
    stack = _StubStack([
        _StubLayer("soul", "a" * 1000),
        _StubLayer("agent", "a" * 500),
    ])
    messages = [{"role": "user", "content": "a" * 500}]
    dim = ContextLayoutDimension(
        stack=stack, messages_ref=messages, max_window=200_000,
    )
    dim.update_total(input_tokens=20_000)

    snap = dim.snapshot()
    # 2000 chars total; soul is half → ~10k tokens
    assert snap["total_tokens"] == 20_000
    assert 9_500 <= snap["layers"]["soul"] <= 10_500
    assert snap["layers"]["agent"] == snap["layers"]["messages"]


def test_context_layout_zero_state():
    dim = ContextLayoutDimension()
    assert "no llm calls" in dim.render_summary()
    assert dim.has_anomaly() is False


# ---------------------------------------------------------------------------
# Unknown dimension factory
# ---------------------------------------------------------------------------

def test_unknown_dimension_degrades_gracefully():
    assert _build_dimension("nonexistent") is None


# ---------------------------------------------------------------------------
# AgentTelemetryTracker lifecycle
# ---------------------------------------------------------------------------

async def test_tracker_persists_and_reads_back(store):
    async with store.connect() as db:
        tracker = AgentTelemetryTracker(
            db, "sess-a", persist_interval=1,
        )
        await tracker.ensure_table()

        tracker.get("tool_call").record(
            "recall", success=True, duration_ms=12.0,
        )
        tracker.mark_dirty()
        await tracker.flush()

        rows = await (await db.execute(
            "SELECT dimension, payload FROM agent_telemetry "
            "WHERE session_id = ? ORDER BY dimension",
            ("sess-a",),
        )).fetchall()
        dims = {r[0]: json.loads(r[1]) for r in rows}
        assert set(dims.keys()) == set(DEFAULT_DIMENSIONS)
        assert dims["tool_call"]["tools"]["recall"]["success"] == 1


async def test_tracker_maybe_flush_respects_interval(store):
    async with store.connect() as db:
        tracker = AgentTelemetryTracker(db, "sess-b", persist_interval=5)
        await tracker.ensure_table()

        tracker.mark_dirty()  # 1 event, below threshold
        await tracker.maybe_flush()

        # Nothing should be written yet.
        rows = await (await db.execute(
            "SELECT COUNT(*) FROM agent_telemetry WHERE session_id = ?",
            ("sess-b",),
        )).fetchone()
        assert rows[0] == 0

        for _ in range(4):
            tracker.mark_dirty()  # reaches threshold
        await tracker.maybe_flush()

        rows = await (await db.execute(
            "SELECT COUNT(*) FROM agent_telemetry WHERE session_id = ?",
            ("sess-b",),
        )).fetchone()
        assert rows[0] == len(DEFAULT_DIMENSIONS)


async def test_tracker_anomaly_report_silent_when_healthy(store):
    async with store.connect() as db:
        tracker = AgentTelemetryTracker(db, "sess-c")
        # No events recorded — nothing can be anomalous.
        assert tracker.anomaly_report() is None


async def test_tracker_anomaly_report_fires(store):
    async with store.connect() as db:
        tracker = AgentTelemetryTracker(db, "sess-d")
        tool_dim = tracker.get("tool_call")
        tool_dim.record("recall", success=True, duration_ms=1.0)
        for _ in range(4):
            tool_dim.record("recall", success=False, duration_ms=1.0, error_msg="oops")

        alert = tracker.anomaly_report()
        assert alert is not None
        assert "tool_call" in alert
        assert "AGENT TELEMETRY ALERT" in alert


async def test_tracker_reports(store):
    async with store.connect() as db:
        tracker = AgentTelemetryTracker(db, "sess-e")
        tracker.get("tool_call").record("recall", success=True, duration_ms=1.0)
        minimal = tracker.report_minimal()
        assert "tool:" in minimal

        detail_all = tracker.report_detail()
        assert "tool_call" in detail_all
        assert "memory_compression" in detail_all

        per_dim = tracker.report_detail("memory_compression")
        assert "no runs" in per_dim


async def test_tracker_unknown_dimension_in_detail(store):
    async with store.connect() as db:
        tracker = AgentTelemetryTracker(db, "sess-f")
        out = tracker.report_detail("does_not_exist")
        assert "Unknown dimension" in out


# ---------------------------------------------------------------------------
# Integration with main SCHEMA
# ---------------------------------------------------------------------------

async def test_agent_telemetry_table_created_by_initialize(store):
    """SCHEMA at initialize() time should include agent_telemetry — no
    separate ensure_table() required when using the standard store init.
    """
    async with store.connect() as db:
        row = await (await db.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='agent_telemetry'"
        )).fetchone()
        assert row is not None
