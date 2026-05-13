"""Issue #356 — Discord daily safety-net memory compaction."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from loom.platform.discord.bot import LoomDiscordBot


class _FakeSession:
    def __init__(self, name: str, *, fail: bool = False):
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


async def test_start_session_runs_resume_compaction_pass(monkeypatch) -> None:
    import loom.core.session as session_module

    bot = LoomDiscordBot(model="test-model", db_path="/tmp/loom-test.db")
    bot._thread_map = {"123": "sess-existing"}
    bot._save_thread_map = MagicMock()
    bot._client.get_channel = MagicMock(return_value=None)
    bot._force_compact_session = AsyncMock()

    session = MagicMock()
    session.session_id = "sess-existing"
    session.workspace = MagicMock()
    session.registry.register = MagicMock()
    session.perm.authorize = MagicMock()
    session._pipeline._middlewares = []
    session._pipeline.use = MagicMock()
    session._loom_config = {"task_write": {"discord_reminder": False}}
    session.subscribe_diagnostic = MagicMock()
    session.start = AsyncMock()

    monkeypatch.setattr(session_module, "LoomSession", MagicMock(return_value=session))

    started = await bot._start_session(123)

    assert started is session
    bot._force_compact_session.assert_awaited_once_with(
        123, session, reason="resume"
    )


async def test_start_session_resume_compacts_only_new_session(monkeypatch) -> None:
    import loom.core.session as session_module

    bot = LoomDiscordBot(model="test-model", db_path="/tmp/loom-test.db")
    bot._sessions[999] = _FakeSession("already-active")
    bot._thread_map = {"123": "sess-existing"}
    bot._save_thread_map = MagicMock()
    bot._client.get_channel = MagicMock(return_value=None)

    session = MagicMock()
    session.session_id = "sess-existing"
    session.workspace = MagicMock()
    session.registry.register = MagicMock()
    session.perm.authorize = MagicMock()
    session._pipeline._middlewares = []
    session._pipeline.use = MagicMock()
    session._loom_config = {"task_write": {"discord_reminder": False}}
    session.subscribe_diagnostic = MagicMock()
    session.start = AsyncMock()
    session.force_compact = AsyncMock()

    monkeypatch.setattr(session_module, "LoomSession", MagicMock(return_value=session))

    await bot._start_session(123)

    assert bot._sessions[999].calls == 0
    session.force_compact.assert_awaited_once()
