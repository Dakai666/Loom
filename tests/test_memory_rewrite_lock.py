"""One memory-rewriting dream pass at a time per event loop (#624 follow-up).

The daemon's convergent / distill loops share the session's DB connection; a
commit from one inside the other's multi-statement write would land half of
it. (The distill_dream agent tool that also took the lock was retired, #639.)
"""

from __future__ import annotations

import asyncio

from loom.core.cognition.consolidation import memory_rewrite_lock


async def test_same_loop_shares_one_lock():
    assert memory_rewrite_lock() is memory_rewrite_lock()


def test_each_event_loop_gets_its_own_lock():
    async def grab():
        lock = memory_rewrite_lock()
        async with lock:             # contended use binds an asyncio.Lock to its loop
            await asyncio.sleep(0)
        return lock
    a = asyncio.run(grab())
    b = asyncio.run(grab())          # a fresh loop must not trip over a's binding
    assert a is not b


async def test_daemon_and_another_holder_use_the_same_lock(tmp_path):
    import types

    from loom.autonomy.daemon import AutonomyDaemon
    daemon = AutonomyDaemon(notify_router=None, confirm_flow=None,
                            loom_session=types.SimpleNamespace())
    order = []

    async def inner(tag):
        order.append(f"{tag}+")
        await asyncio.sleep(0.02)
        order.append(f"{tag}-")
    wrapped = daemon._one_dream_at_a_time(lambda: inner("daemon"))

    async def tool_side():
        async with memory_rewrite_lock():
            await inner("tool")
    await asyncio.gather(wrapped(), tool_side())
    assert order in (["daemon+", "daemon-", "tool+", "tool-"],
                     ["tool+", "tool-", "daemon+", "daemon-"])


async def test_nested_acquire_raises_instead_of_deadlocking():
    import pytest

    lock = memory_rewrite_lock()

    async def nested():
        async with lock:
            async with memory_rewrite_lock():
                pass
    with pytest.raises(RuntimeError, match="re-entrant"):
        await asyncio.wait_for(nested(), timeout=2)   # a deadlock fails, not hangs
    async with lock:            # released cleanly after the refusal
        pass
