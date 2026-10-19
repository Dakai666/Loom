"""One memory-rewriting dream pass at a time per event loop (#624 follow-up).

The daemon's convergent / distill loops and a session's distill_dream tool
share the session's DB connection; a commit from one inside the other's
multi-statement write would land half of it.
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


async def test_daemon_and_tool_use_the_same_lock(tmp_path):
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


async def test_distill_tool_execute_waits_for_a_running_dream(tmp_path):
    from loom.core.memory.maintenance import make_distill_dream_tool
    from loom.core.memory.semantic import SemanticMemory
    from loom.core.memory.store import SQLiteStore
    from tests.test_distill_dream import _call, _llm, _topic

    store = SQLiteStore(str(tmp_path / "lock.db"))
    await store.initialize()
    async with store.connect() as db:
        await _topic(db)
        tool = make_distill_dream_tool(SemanticMemory(db), _llm(), dreams_dir=tmp_path,
                                       allow_execute=True)
        lock = memory_rewrite_lock()
        await lock.acquire()                 # a daemon pass is mid-write
        task = asyncio.create_task(tool.executor(_call({"execute": True})))
        await asyncio.sleep(0.1)
        assert not task.done()
        lock.release()
        res = await asyncio.wait_for(task, timeout=5)
        assert res.success, res.error
