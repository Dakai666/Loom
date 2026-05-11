# Daily Safety-Net Compaction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a daily Discord safety-net compaction path that promotes idle episodic memory to semantic memory even when turn-driven thresholds do not fire.

**Architecture:** `LoomSession` exposes `force_compact()` and moves shared compression work into one private helper guarded by `_compaction_lock`. `LoomDiscordBot` schedules one 05:00 local-time daily pass over in-memory sessions and runs a best-effort compaction pass after lazy session resume.

**Tech Stack:** Python async, `discord.ext.tasks`, pytest async tests, existing Loom memory compaction helpers.

---

## File Structure

- Modify `loom/core/session.py`
  - Add `_compact_memory(force: bool)` as the shared compaction body.
  - Update `_run_compaction_check()` to lock and call `_compact_memory(force=False)`.
  - Add public `force_compact()` that locks and calls `_compact_memory(force=True)`.
  - Update `_compaction_subscriber_loop()` to rely on `_run_compaction_check()` for locking.
- Modify `loom/platform/discord/bot.py`
  - Import `datetime`, `time` as needed, and `discord.ext.tasks`.
  - Add daily compaction loop initialization in `LoomDiscordBot.__init__()`.
  - Start the loop in `on_ready()` if it is not running.
  - Add `_run_daily_compaction()` and `_force_compact_active_sessions(reason: str)`.
  - Add a best-effort resume-time compaction call in `_start_session()`.
  - Add `close()` to cancel the loop and close active sessions/client.
- Modify `tests/test_memory_compaction_subscriber.py`
  - Bind `force_compact()` onto the stub.
  - Add tests for below-threshold forced compaction and forced/threshold serialization.
- Modify or create `tests/test_discord_daily_compaction.py`
  - Test active-session daily pass calls each session's `force_compact()`.
  - Test failures in one session do not stop the rest.
  - Test `_start_session()` runs the resume-time pass on the newly created session.

## Tasks

### Task 1: Core Forced Compaction

**Files:**
- Modify: `tests/test_memory_compaction_subscriber.py`
- Modify: `loom/core/session.py`

- [ ] **Step 1: Write failing tests**

Add two tests:

```python
async def test_force_compact_bypasses_threshold_and_buffers_done(monkeypatch) -> None:
    s = _make_stub(ep_count=5, threshold=30, fact_count=6)
    monkeypatch.setattr(s._session_module, "compress_session", s._fake_compress)
    await s.force_compact()
    assert s.compress_called == 1
    assert s.refresh_called == 1
    assert len(s._pending_compactions) == 1
    assert isinstance(s._pending_compactions[0], CompressDone)
    assert s._pending_compactions[0].fact_count == 6


async def test_force_and_threshold_checks_share_compaction_lock(monkeypatch) -> None:
    s = _make_stub(ep_count=30, threshold=30, fact_count=2)
    in_flight = 0
    max_in_flight = 0

    async def _slow_compress(*_a, **_kw):
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0.05)
        in_flight -= 1
        return 2

    monkeypatch.setattr(s._session_module, "compress_session", _slow_compress)
    await asyncio.gather(s.force_compact(), s._run_compaction_check())
    assert max_in_flight == 1
```

Update `_make_stub()` to bind `s.force_compact = LoomSession.force_compact.__get__(s)`.

- [ ] **Step 2: Run tests to verify red**

Run:

```bash
pytest -q tests/test_memory_compaction_subscriber.py::test_force_compact_bypasses_threshold_and_buffers_done tests/test_memory_compaction_subscriber.py::test_force_and_threshold_checks_share_compaction_lock
```

Expected: fails because `LoomSession.force_compact` does not exist.

- [ ] **Step 3: Implement minimal core behavior**

In `loom/core/session.py`, add:

```python
async def _compact_memory(self, *, force: bool = False) -> None:
    ep_count = await self._memory.episodic.count_session(
        self.session_id, uncompressed_only=True,
    )
    if not force and ep_count < self._episodic_compress_threshold:
        return
    fact_count = await compress_session(...)
    if fact_count:
        self._pending_compactions.append(CompressDone(fact_count=fact_count))
    await self._refresh_memory_index()
```

Then wrap `_run_compaction_check()` and `force_compact()` in:

```python
try:
    async with self._compaction_lock:
        await self._compact_memory(force=False)
except Exception:
    logger.exception(...)
```

and:

```python
async def force_compact(self) -> None:
    try:
        async with self._compaction_lock:
            await self._compact_memory(force=True)
    except Exception:
        logger.exception(...)
```

Update `_compaction_subscriber_loop()` so the event body calls `await self._run_compaction_check()` directly.

- [ ] **Step 4: Verify green**

Run:

```bash
pytest -q tests/test_memory_compaction_subscriber.py
```

Expected: all tests in the file pass.

### Task 2: Discord Daily Compaction

**Files:**
- Create or modify: `tests/test_discord_daily_compaction.py`
- Modify: `loom/platform/discord/bot.py`

- [ ] **Step 1: Write failing tests**

Create tests with fake sessions:

```python
class _FakeSession:
    def __init__(self, name: str, fail: bool = False):
        self.name = name
        self.fail = fail
        self.calls = 0

    async def force_compact(self) -> None:
        self.calls += 1
        if self.fail:
            raise RuntimeError(f"{self.name} failed")


async def test_daily_compaction_runs_for_all_active_sessions() -> None:
    bot = LoomDiscordBot(model="test-model", db_path="/tmp/loom-test.db")
    s1 = _FakeSession("one")
    s2 = _FakeSession("two")
    bot._sessions = {1: s1, 2: s2}
    await bot._force_compact_active_sessions(reason="test")
    assert s1.calls == 1
    assert s2.calls == 1


async def test_daily_compaction_continues_after_session_failure() -> None:
    bot = LoomDiscordBot(model="test-model", db_path="/tmp/loom-test.db")
    s1 = _FakeSession("one", fail=True)
    s2 = _FakeSession("two")
    bot._sessions = {1: s1, 2: s2}
    await bot._force_compact_active_sessions(reason="test")
    assert s1.calls == 1
    assert s2.calls == 1
```

- [ ] **Step 2: Run tests to verify red**

Run:

```bash
pytest -q tests/test_discord_daily_compaction.py::test_daily_compaction_runs_for_all_active_sessions tests/test_discord_daily_compaction.py::test_daily_compaction_continues_after_session_failure
```

Expected: fails because `_force_compact_active_sessions` does not exist.

- [ ] **Step 3: Implement Discord pass**

In `loom/platform/discord/bot.py`:

```python
from datetime import datetime, time as datetime_time
from discord.ext import tasks
```

In `__init__()`:

```python
local_tz = datetime.now().astimezone().tzinfo
self._daily_compaction_loop = tasks.loop(
    time=datetime_time(hour=5, minute=0, tzinfo=local_tz)
)(self._run_daily_compaction)
```

Add:

```python
async def _run_daily_compaction(self) -> None:
    await self._force_compact_active_sessions(reason="daily")

async def _force_compact_active_sessions(self, *, reason: str) -> None:
    for thread_id, session in list(self._sessions.items()):
        try:
            await session.force_compact()
        except Exception:
            logger.exception(...)
```

Start the loop from `on_ready()` only if it is not running.

- [ ] **Step 4: Verify green**

Run:

```bash
pytest -q tests/test_discord_daily_compaction.py
```

Expected: tests pass.

### Task 3: Resume-Time Pass and Shutdown

**Files:**
- Modify: `tests/test_discord_daily_compaction.py`
- Modify: `loom/platform/discord/bot.py`

- [ ] **Step 1: Write failing resume test**

Patch `loom.platform.discord.bot.LoomSession`, middleware setup, and tool registration as needed, then assert `_start_session()` calls `force_compact()` once on the new session.

- [ ] **Step 2: Run test to verify red**

Run:

```bash
pytest -q tests/test_discord_daily_compaction.py::test_start_session_runs_resume_compaction_pass
```

Expected: fails because `_start_session()` does not call the helper.

- [ ] **Step 3: Implement resume pass and close**

After `self._sessions[thread_id] = session` in `_start_session()`:

```python
await self._force_compact_active_sessions(reason="resume")
```

or call a single-session helper if implemented. Add `close()` that cancels
`_daily_compaction_loop`, closes all active sessions, and closes the Discord
client.

- [ ] **Step 4: Verify green**

Run:

```bash
pytest -q tests/test_discord_daily_compaction.py tests/test_discord_slash_commands.py tests/test_memory_compaction_subscriber.py
```

Expected: all pass.

### Task 4: Final Verification and PR

**Files:**
- All touched files.

- [ ] **Step 1: Run focused suite**

```bash
pytest -q tests/test_memory_compaction_subscriber.py tests/test_discord_daily_compaction.py tests/test_discord_slash_commands.py tests/test_memory.py tests/test_memory_search.py tests/test_memory_facade.py tests/test_session.py
```

Expected: pass.

- [ ] **Step 2: Static cleanup**

```bash
git diff --check
npx gitnexus detect-changes
```

Expected: no whitespace errors; GitNexus affected scope matches memory compaction and Discord bot.

- [ ] **Step 3: Commit and open PR**

```bash
git status --short
git add docs/superpowers/specs/2026-05-11-daily-safety-net-compaction-design.md docs/superpowers/plans/2026-05-11-daily-safety-net-compaction.md loom/core/session.py loom/platform/discord/bot.py tests/test_memory_compaction_subscriber.py tests/test_discord_daily_compaction.py
git commit -m "feat(memory): add Discord safety-net compaction"
git push -u origin codex/daily-safety-net-compaction
gh pr create --title "feat(memory): add Discord safety-net compaction" --body "..."
```

Expected: PR links issue #356 and lists verification output.

## Self-Review

- Spec coverage: The plan covers forced compaction below threshold, daily Discord scheduling, resume-time safety pass, lock serialization, and existing-suite verification.
- Placeholder scan: No task relies on an undefined "TODO"; code examples name the exact methods and files.
- Type consistency: The plan uses `force_compact()`, `_force_compact_active_sessions(reason=...)`, and `_run_daily_compaction()` consistently across tests and implementation.
