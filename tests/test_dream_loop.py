"""Tests for DreamLoop (Issue #376) — autonomy-driven dream cadence."""

from __future__ import annotations

import asyncio
import logging

import pytest

from loom.autonomy.maintenance import DreamLoop
from loom.core.infra import AbortController


@pytest.fixture(autouse=True)
def _fast_first_sweep(monkeypatch):
    """Shorten the first-sweep delay so tests don't wait 5 minutes."""
    monkeypatch.setattr(DreamLoop, "_FIRST_SWEEP_DELAY_SECONDS", 0.05)


async def test_dream_loop_calls_dream_fn_then_aborts():
    calls: list[dict] = []

    async def dream_fn():
        result = {
            "domain": "self",
            "facts_sampled": 5,
            "triples_found": 3,
            "triples_written": 2,
            "errors": [],
        }
        calls.append(result)
        return result

    abort = AbortController()
    loop = DreamLoop(dream_fn=dream_fn, abort=abort, interval_hours=24.0)

    task = asyncio.create_task(loop.run_forever())
    # Wait long enough for the first sweep to fire, but well under the
    # 24h interval so the second one cannot.
    await asyncio.sleep(0.2)
    abort.abort()
    await asyncio.wait_for(task, timeout=1.0)

    assert len(calls) == 1


async def test_dream_loop_returns_immediately_when_pre_aborted():
    async def dream_fn():
        raise AssertionError("should not run when pre-aborted")

    abort = AbortController()
    abort.abort()
    loop = DreamLoop(dream_fn=dream_fn, abort=abort, interval_hours=24.0)

    await asyncio.wait_for(loop.run_forever(), timeout=0.5)


async def test_dream_loop_swallows_dream_fn_failure(caplog):
    calls = 0

    async def dream_fn():
        nonlocal calls
        calls += 1
        raise RuntimeError("boom")

    abort = AbortController()
    loop = DreamLoop(dream_fn=dream_fn, abort=abort, interval_hours=24.0)

    with caplog.at_level(logging.WARNING, logger="loom.autonomy.maintenance"):
        task = asyncio.create_task(loop.run_forever())
        await asyncio.sleep(0.2)
        abort.abort()
        await asyncio.wait_for(task, timeout=1.0)

    assert calls == 1
    assert any("dream] cycle failed" in r.message for r in caplog.records)


async def test_dream_loop_min_interval_clamp():
    """interval_hours that would be < 60s gets clamped, so the loop is
    never set to a degenerate zero-sleep busy-poll."""
    loop = DreamLoop(
        dream_fn=lambda: asyncio.sleep(0),  # never invoked
        abort=AbortController(),
        interval_hours=0.0,
    )
    assert loop._interval_seconds >= 60.0


# ---------------------------------------------------------------------------
# Daemon wiring (_maybe_start_dream_loop) — exercises config gating + the
# closure construction. Stops short of running an actual dream cycle.
# ---------------------------------------------------------------------------


class _StubMemory:
    def __init__(self):
        self.semantic = object()
        self.relational = object()


class _StubSession:
    def __init__(self, *, with_memory=True, with_db=True):
        self._memory = _StubMemory() if with_memory else None
        self._db = object() if with_db else None
        self.router = object()
        self.model = "stub-model"


def _make_daemon(session):
    from loom.autonomy.daemon import AutonomyDaemon

    return AutonomyDaemon(
        notify_router=None,
        confirm_flow=None,
        loom_session=session,
    )


def test_maybe_start_dream_loop_skipped_when_disabled(monkeypatch):
    monkeypatch.setattr(
        "loom.core.session._load_loom_config",
        lambda: {"memory": {"dream": {"enabled": False}}},
    )
    daemon = _make_daemon(_StubSession())
    assert daemon._maybe_start_dream_loop() is None


def test_maybe_start_dream_loop_skipped_without_session(monkeypatch):
    daemon = _make_daemon(None)
    assert daemon._maybe_start_dream_loop() is None


def test_maybe_start_dream_loop_skipped_without_db(monkeypatch):
    monkeypatch.setattr(
        "loom.core.session._load_loom_config",
        lambda: {"memory": {"dream": {"enabled": True}}},
    )
    daemon = _make_daemon(_StubSession(with_db=False))
    assert daemon._maybe_start_dream_loop() is None


async def test_maybe_start_dream_loop_launches_and_cancels(monkeypatch):
    monkeypatch.setattr(DreamLoop, "_FIRST_SWEEP_DELAY_SECONDS", 60.0)
    monkeypatch.setattr(
        "loom.core.session._load_loom_config",
        lambda: {"memory": {"dream": {"enabled": True, "interval_hours": 12}}},
    )
    daemon = _make_daemon(_StubSession())
    task = daemon._maybe_start_dream_loop()
    assert task is not None
    daemon._abort.abort()
    await asyncio.wait_for(task, timeout=1.0)
