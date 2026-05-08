"""TriggerEvaluator → ledger env_observation emit (#322 commit 4 / doc/53 §3.1, §4.2).

env_observation is one of the three event types that does NOT inherit
the parent correlation_id (see §4.2). Each trigger fire opens a fresh
correlation chain; downstream emits inside the on_fire reaction inherit
the new correlation via async_correlation_scope.
"""

from __future__ import annotations

from datetime import datetime, UTC
from pathlib import Path

import pytest
import pytest_asyncio

from loom.autonomy.evaluator import TriggerEvaluator
from loom.autonomy.triggers import (
    ConditionTrigger,
    CronTrigger,
    EventTrigger,
)
from loom.core.ledger import (
    LedgerEmitter,
    LedgerStore,
    ModelEventPayload,
    async_correlation_scope,
    current_correlation,
)


@pytest_asyncio.fixture
async def ledger(tmp_path: Path) -> LedgerStore:
    s = LedgerStore(
        db_path=tmp_path / "ledger.db",
        blob_dir=tmp_path / "blobs",
    )
    await s.open()
    try:
        yield s
    finally:
        await s.close()


@pytest_asyncio.fixture
def emitter(ledger: LedgerStore) -> LedgerEmitter:
    return LedgerEmitter(ledger, session_id="sess_daemon")


async def _all_env_obs(ledger: LedgerStore) -> list:
    rows = await ledger.fetch_by_turn("system:autonomy")
    return [r for r in rows if r.event_type == "env_observation"]


# ---------------------------------------------------------------------------
# Cron / Event / Condition trigger kinds → correct observation_type
# ---------------------------------------------------------------------------


async def test_cron_fire_emits_timer_observation(
    ledger: LedgerStore, emitter: LedgerEmitter
) -> None:
    evaluator = TriggerEvaluator(ledger_emitter=emitter)
    # cron "* * * * *" matches every minute
    evaluator.register(CronTrigger(name="every_minute", intent="x", cron="* * * * *"))

    # Pin a specific minute so dedup is deterministic
    dt = datetime(2026, 5, 8, 12, 0, 0, tzinfo=UTC)
    await evaluator.evaluate_cron(dt)

    events = await _all_env_obs(ledger)
    assert len(events) == 1
    p = events[0].payload
    assert p["observation_type"] == "timer"
    assert p["source"] == "autonomy_daemon"
    assert p["detail"]["trigger_name"] == "every_minute"
    assert p["detail"]["trigger_kind"] == "cron"


async def test_event_fire_emits_external_observation(
    ledger: LedgerStore, emitter: LedgerEmitter
) -> None:
    evaluator = TriggerEvaluator(ledger_emitter=emitter)
    evaluator.register(
        EventTrigger(name="on_push", intent="x", event_name="git_push")
    )
    await evaluator.emit("git_push", {"repo": "Loom"})

    events = await _all_env_obs(ledger)
    assert len(events) == 1
    p = events[0].payload
    assert p["observation_type"] == "external"
    assert p["detail"]["context"]["repo"] == "Loom"


async def test_condition_fire_emits_anomaly_observation(
    ledger: LedgerStore, emitter: LedgerEmitter
) -> None:
    evaluator = TriggerEvaluator(ledger_emitter=emitter)
    evaluator.register(
        ConditionTrigger(
            name="memory_full", intent="x", condition_fn=lambda: True
        )
    )
    await evaluator.poll_conditions()

    events = await _all_env_obs(ledger)
    assert len(events) == 1
    assert events[0].payload["observation_type"] == "anomaly"


# ---------------------------------------------------------------------------
# Each fire opens a NEW correlation_id (does not inherit parent)
# ---------------------------------------------------------------------------


async def test_each_fire_opens_fresh_correlation(
    ledger: LedgerStore, emitter: LedgerEmitter
) -> None:
    evaluator = TriggerEvaluator(ledger_emitter=emitter)
    evaluator.register(
        EventTrigger(name="t", intent="x", event_name="ping")
    )

    # Even when an outer correlation_id is active, env_observation must
    # mint a fresh one (§4.2 三類例外).
    async with async_correlation_scope("outer_corr"):
        await evaluator.emit("ping", {})
        await evaluator.emit("ping", {})

    events = await _all_env_obs(ledger)
    assert len(events) == 2
    corrs = {e.correlation_id for e in events}
    assert "outer_corr" not in corrs
    assert all(c.startswith("env_") for c in corrs)
    assert len(corrs) == 2  # two distinct fresh ids


# ---------------------------------------------------------------------------
# Reaction chain (on_fire) inherits the freshly-minted correlation
# ---------------------------------------------------------------------------


async def test_reaction_chain_inherits_new_correlation(
    ledger: LedgerStore, emitter: LedgerEmitter
) -> None:
    captured: list[str | None] = []

    async def on_fire(trigger, ctx):
        # Inside the reaction, current_correlation() is the freshly
        # minted env correlation. Downstream emits inherit it.
        captured.append(current_correlation())
        await emitter.emit_model_event(
            turn_id="system:autonomy",
            payload=ModelEventPayload(
                model="x", tier=1, token_usage={"prompt": 1, "completion": 1}
            ),
        )

    evaluator = TriggerEvaluator(on_fire=on_fire, ledger_emitter=emitter)
    evaluator.register(EventTrigger(name="t", intent="x", event_name="ping"))
    await evaluator.emit("ping", {})

    rows = await ledger.fetch_by_turn("system:autonomy")
    env_evt = next(r for r in rows if r.event_type == "env_observation")
    model_evt = next(r for r in rows if r.event_type == "model_event")
    assert env_evt.correlation_id.startswith("env_")
    assert model_evt.correlation_id == env_evt.correlation_id
    assert captured == [env_evt.correlation_id]


# ---------------------------------------------------------------------------
# Failure isolation + no-emitter
# ---------------------------------------------------------------------------


async def test_emit_failure_does_not_block_on_fire(
    ledger: LedgerStore, emitter: LedgerEmitter
) -> None:
    fired: list[str] = []

    async def on_fire(trigger, ctx):
        fired.append(trigger.name)

    evaluator = TriggerEvaluator(on_fire=on_fire, ledger_emitter=emitter)
    evaluator.register(EventTrigger(name="t", intent="x", event_name="ping"))

    # Close ledger to force emit failure
    await ledger.close()
    await evaluator.emit("ping", {})
    assert fired == ["t"]


async def test_no_emitter_skips_env_observation(ledger: LedgerStore) -> None:
    fired: list[str] = []

    async def on_fire(trigger, ctx):
        fired.append(trigger.name)

    evaluator = TriggerEvaluator(on_fire=on_fire)  # no ledger_emitter
    evaluator.register(EventTrigger(name="t", intent="x", event_name="ping"))
    await evaluator.emit("ping", {})

    events = await _all_env_obs(ledger)
    assert events == []
    assert fired == ["t"]
