"""
Tests for scheduled chime delivery (issue #369).

Covers:
- ``ChimeRequest`` formatting and the ``<system_chime>`` wrapper
- ``CronTrigger`` mode/target validation
- ``SessionRegistry`` register / lookup / unregister
- ``AutonomyDaemon._execute_plan`` mode="chime" routing + fallback behaviour
- ``daemon.load_config`` parsing of chime schedules (incl. invalid targets)
- ``LoomDiscordBot.deliver_chime`` dedupe + dispatcher draining

The Discord-bot tests use a lightweight stand-in for the heavy
``discord.Client`` to avoid the cost (and IO) of constructing a real
bot. The flow we exercise — enqueue → dispatcher pop → invoke
``_run_chime_turn`` — is independent of Discord network code.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest


# ===========================================================================
# format_chime_content / ChimeRequest
# ===========================================================================


class TestChimeFormatting:
    def test_wrapper_includes_schedule_and_fired_at(self):
        from loom.autonomy.chime import ChimeRequest, format_chime_content

        req = ChimeRequest(
            schedule_name="morning_greeting",
            intent="向 Dakai 道早安",
            fired_at=datetime(2026, 5, 14, 1, 0, tzinfo=timezone.utc),
        )
        out = format_chime_content(req)

        assert out.startswith('<system_chime schedule="morning_greeting"')
        assert 'fired_at="2026-05-14T01:00:00+00:00"' in out
        assert "向 Dakai 道早安" in out
        assert out.endswith("</system_chime>")

    def test_intent_is_stripped(self):
        from loom.autonomy.chime import ChimeRequest, format_chime_content

        req = ChimeRequest(
            schedule_name="x",
            intent="\n\n  hello  \n\n",
            fired_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        assert "hello" in format_chime_content(req)
        # No leading/trailing whitespace bleeding into the body
        assert ">\nhello\n<" in format_chime_content(req)


# ===========================================================================
# CronTrigger — mode + target validation
# ===========================================================================


class TestCronTriggerModes:
    def test_default_mode_is_independent(self):
        from loom.autonomy.triggers import CronTrigger

        t = CronTrigger(name="x", intent="y", cron="0 9 * * *")
        assert t.mode == "independent"
        assert t.target == {}

    def test_chime_mode_keeps_target(self):
        from loom.autonomy.triggers import CronTrigger

        t = CronTrigger(
            name="x",
            intent="y",
            cron="0 9 * * *",
            mode="chime",
            target={"type": "discord_thread", "id": "123"},
        )
        assert t.mode == "chime"
        assert t.target["id"] == "123"

    def test_invalid_mode_raises(self):
        from loom.autonomy.triggers import CronTrigger

        with pytest.raises(ValueError):
            CronTrigger(name="x", intent="y", cron="0 9 * * *", mode="bogus")


# ===========================================================================
# SessionRegistry
# ===========================================================================


class TestSessionRegistry:
    def test_register_lookup_by_label(self):
        from loom.platform.session_registry import SessionRegistry

        reg = SessionRegistry()
        sess_a = object()
        sess_b = object()
        reg.register("a", sess_a, labels={"discord_thread": "111"})  # type: ignore[arg-type]
        reg.register("b", sess_b, labels={"discord_thread": "222"})  # type: ignore[arg-type]

        assert reg.find_by_label("discord_thread", "111") == [sess_a]
        assert reg.find_by_label("discord_thread", "222") == [sess_b]
        assert reg.find_by_label("discord_thread", "999") == []

    def test_unregister_removes_session(self):
        from loom.platform.session_registry import SessionRegistry

        reg = SessionRegistry()
        sess = object()
        reg.register("a", sess, labels={"discord_thread": "111"})  # type: ignore[arg-type]
        reg.unregister("a")
        assert reg.get("a") is None
        assert reg.find_by_label("discord_thread", "111") == []

    def test_re_register_overwrites_labels(self):
        from loom.platform.session_registry import SessionRegistry

        reg = SessionRegistry()
        sess = object()
        reg.register("a", sess, labels={"discord_thread": "111"})  # type: ignore[arg-type]
        reg.register("a", sess, labels={"discord_thread": "222"})  # type: ignore[arg-type]
        assert reg.find_by_label("discord_thread", "111") == []
        assert reg.find_by_label("discord_thread", "222") == [sess]


# ===========================================================================
# AutonomyDaemon._execute_plan — chime routing
# ===========================================================================


def _make_daemon(*, chime_delivery=None, session=None):
    from loom.autonomy.daemon import AutonomyDaemon
    from loom.notify.confirm import ConfirmFlow
    from loom.notify.router import NotificationRouter

    router = NotificationRouter()
    flow = ConfirmFlow(send_fn=lambda n: None)
    return AutonomyDaemon(
        notify_router=router,
        confirm_flow=flow,
        loom_session=session,
        chime_delivery=chime_delivery,
    )


def _chime_plan(*, target=None, fallback=None):
    from loom.autonomy.planner import ActionDecision, PlannedAction
    from loom.core.harness.permissions import TrustLevel

    tgt = dict(target or {"type": "discord_thread", "id": "111"})
    if fallback is not None:
        tgt["fallback"] = fallback
    return PlannedAction(
        trigger_name="morning_greeting",
        intent="say hi",
        prompt="say hi",
        decision=ActionDecision.EXECUTE,
        trust_level=TrustLevel.SAFE,
        context={
            "mode": "chime",
            "target": tgt,
            "trigger_name": "morning_greeting",
            "intent": "say hi",
            "allowed_tools": [],
            "scope_grants": [],
            "attach_outputs": [],
            "notify_thread_id": 0,
            "trust_level": "safe",
        },
    )


class TestChimeExecution:
    @pytest.mark.asyncio
    async def test_chime_routes_to_delivery_callback(self):
        delivered: list = []

        async def deliver(req):
            delivered.append(req)
            return True

        daemon = _make_daemon(chime_delivery=deliver)
        await daemon._execute_plan(_chime_plan())

        assert len(delivered) == 1
        assert delivered[0].schedule_name == "morning_greeting"
        assert delivered[0].target["id"] == "111"

    @pytest.mark.asyncio
    async def test_chime_with_no_callback_skips(self):
        # No delivery callback wired + no session ⇒ nothing happens (no crash).
        daemon = _make_daemon(chime_delivery=None, session=None)
        await daemon._execute_plan(_chime_plan(fallback="skip"))
        # If we reach here without exception, the skip branch worked.

    @pytest.mark.asyncio
    async def test_chime_miss_falls_back_to_independent(self):
        async def deliver(req):
            return False  # target session not found

        ran_independent = asyncio.Event()

        async def fake_stream(prompt, **_):
            ran_independent.set()
            if False:
                yield  # pragma: no cover — generator marker

        session = MagicMock()
        session.stream_turn = fake_stream
        session.perm.session_authorized = set()
        session.perm.authorize = MagicMock()
        session.perm.revoke = MagicMock()
        session.perm.revoke_matching = MagicMock()
        session.workspace = "/tmp"

        daemon = _make_daemon(chime_delivery=deliver, session=session)
        await daemon._execute_plan(_chime_plan(fallback="independent"))

        assert ran_independent.is_set()

    @pytest.mark.asyncio
    async def test_chime_miss_skips_when_fallback_is_skip(self):
        async def deliver(req):
            return False

        called = False

        async def fake_stream(prompt, **_):
            nonlocal called
            called = True
            if False:
                yield

        session = MagicMock()
        session.stream_turn = fake_stream

        daemon = _make_daemon(chime_delivery=deliver, session=session)
        await daemon._execute_plan(_chime_plan(fallback="skip"))

        assert called is False


# ===========================================================================
# daemon.load_config — chime schedule parsing
# ===========================================================================


class TestChimeConfigLoading:
    def test_loads_chime_schedule(self, tmp_path):
        toml_content = """
[autonomy]
enabled = true

[[autonomy.schedules]]
name = "morning_greeting"
cron = "0 1 * * *"
intent = "say hi"
trust_level = "safe"
mode = "chime"

  [autonomy.schedules.target]
  type = "discord_thread"
  id = "12345"
"""
        cfg = tmp_path / "loom.toml"
        cfg.write_text(toml_content, encoding="utf-8")

        daemon = _make_daemon()
        n = daemon.load_config(cfg)
        assert n == 1

        triggers = list(daemon._evaluator._triggers.values())
        assert triggers[0].mode == "chime"
        assert triggers[0].target == {
            "type": "discord_thread",
            "id": "12345",
            "fallback": "skip",
        }

    def test_chime_without_target_id_is_skipped(self, tmp_path):
        toml_content = """
[autonomy]
enabled = true

[[autonomy.schedules]]
name = "broken"
cron = "0 1 * * *"
intent = "x"
mode = "chime"

  [autonomy.schedules.target]
  type = "discord_thread"
"""
        cfg = tmp_path / "loom.toml"
        cfg.write_text(toml_content, encoding="utf-8")

        daemon = _make_daemon()
        # Missing id ⇒ schedule is dropped with a warning, not a crash.
        n = daemon.load_config(cfg)
        assert n == 0

    def test_chime_wrong_target_type_is_skipped(self, tmp_path):
        toml_content = """
[autonomy]
enabled = true

[[autonomy.schedules]]
name = "broken"
cron = "0 1 * * *"
intent = "x"
mode = "chime"

  [autonomy.schedules.target]
  type = "cli"
  id = "whatever"
"""
        cfg = tmp_path / "loom.toml"
        cfg.write_text(toml_content, encoding="utf-8")

        daemon = _make_daemon()
        n = daemon.load_config(cfg)
        assert n == 0

    def test_integer_target_id_is_coerced_to_string(self, tmp_path):
        toml_content = """
[autonomy]
enabled = true

[[autonomy.schedules]]
name = "g"
cron = "0 1 * * *"
intent = "x"
mode = "chime"

  [autonomy.schedules.target]
  type = "discord_thread"
  id = 99999
"""
        cfg = tmp_path / "loom.toml"
        cfg.write_text(toml_content, encoding="utf-8")

        daemon = _make_daemon()
        daemon.load_config(cfg)
        trig = next(iter(daemon._evaluator._triggers.values()))
        # Label lookup compares to str(thread_id) so the parsed value must be a str.
        assert trig.target["id"] == "99999"


# ===========================================================================
# LoomDiscordBot.deliver_chime — dedupe + dispatch
#
# We bypass __init__ to avoid constructing a real discord.Client. The chime
# dispatch path only touches: _sessions, _running_turns, _chime_pending,
# _chime_dispatcher, _client.get_channel, and session.stream_turn.
# ===========================================================================


def _make_bot_stub():
    """Build a minimal LoomDiscordBot without going through __init__."""
    from loom.platform.discord.bot import LoomDiscordBot

    bot = LoomDiscordBot.__new__(LoomDiscordBot)
    bot._sessions = {}
    bot._running_turns = {}
    bot._chime_pending = {}
    bot._chime_dispatcher = {}
    bot._client = MagicMock()
    bot._client.get_channel = MagicMock(return_value=None)
    return bot


class TestDeliverChime:
    @pytest.mark.asyncio
    async def test_rejects_non_discord_thread_target(self):
        from loom.autonomy.chime import ChimeRequest

        bot = _make_bot_stub()
        req = ChimeRequest(
            schedule_name="x",
            intent="y",
            fired_at=datetime.now(timezone.utc),
            target={"type": "cli", "id": "1"},
        )
        assert await bot.deliver_chime(req) is False

    @pytest.mark.asyncio
    async def test_rejects_when_session_not_loaded(self):
        from loom.autonomy.chime import ChimeRequest

        bot = _make_bot_stub()
        req = ChimeRequest(
            schedule_name="x",
            intent="y",
            fired_at=datetime.now(timezone.utc),
            target={"type": "discord_thread", "id": "404"},
        )
        assert await bot.deliver_chime(req) is False

    @pytest.mark.asyncio
    async def test_latest_wins_dedupe(self):
        from loom.autonomy.chime import ChimeRequest

        bot = _make_bot_stub()
        # Pretend a session exists for this thread but stop the dispatcher
        # from actually executing by replacing the runner.
        bot._sessions[111] = MagicMock()
        bot._run_chime_turn = AsyncMock(return_value=None)

        async def deliver(intent):
            return await bot.deliver_chime(
                ChimeRequest(
                    schedule_name="morning",
                    intent=intent,
                    fired_at=datetime.now(timezone.utc),
                    target={"type": "discord_thread", "id": "111"},
                )
            )

        # Fire three chimes for the same schedule before the dispatcher
        # has a chance to drain (no yields between them).
        results = await asyncio.gather(deliver("v1"), deliver("v2"), deliver("v3"))
        assert all(results)

        # Wait for dispatcher to drain.
        await asyncio.sleep(0)
        for _ in range(10):
            if not bot._chime_dispatcher and not bot._chime_pending:
                break
            await asyncio.sleep(0)

        # All three enqueues collapsed onto a single dispatch — last intent wins.
        assert bot._run_chime_turn.await_count == 1
        delivered_req = bot._run_chime_turn.await_args.args[1]
        assert delivered_req.intent == "v3"


# ===========================================================================
# Dispatcher edge cases — regressions from PR #373 review
# ===========================================================================


class TestChimeDispatcherEdgeCases:
    @pytest.mark.asyncio
    async def test_chime_survives_user_turn_cancellation(self):
        """A cancelled in-flight user turn must not silently drop a queued chime.

        Repro of PR #373 review finding #2: ``except Exception`` did not
        catch ``asyncio.CancelledError`` (Py3.8+ inherits BaseException),
        so the dispatcher exited after popping the request.
        """
        from loom.autonomy.chime import ChimeRequest

        bot = _make_bot_stub()
        bot._sessions[111] = MagicMock()
        bot._run_chime_turn = AsyncMock(return_value=None)

        # Stand in for an in-flight user turn that we'll cancel before
        # the chime dispatcher gets to await it.
        async def fake_user_turn():
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                raise
        user_task = asyncio.ensure_future(fake_user_turn())
        bot._running_turns[111] = user_task

        # Deliver the chime — dispatcher picks the name, then awaits user_task.
        assert await bot.deliver_chime(
            ChimeRequest(
                schedule_name="morning",
                intent="hello",
                fired_at=datetime.now(timezone.utc),
                target={"type": "discord_thread", "id": "111"},
            )
        )

        # Give the dispatcher a turn to reach the await.
        await asyncio.sleep(0)
        # Now cancel the user turn — this is what `_handle_message`'s
        # cancel-and-replace path does when a new user message arrives.
        user_task.cancel()
        try:
            await user_task
        except asyncio.CancelledError:
            pass

        # Dispatcher should still drain.
        for _ in range(20):
            if bot._run_chime_turn.await_count >= 1:
                break
            await asyncio.sleep(0)

        assert bot._run_chime_turn.await_count == 1, (
            "chime was silently dropped when the user turn was cancelled"
        )
        assert bot._chime_pending.get(111) is None

    @pytest.mark.asyncio
    async def test_latest_wins_through_user_turn_wait(self):
        """A same-name chime arriving while the dispatcher is waiting on
        an in-flight user turn must still collapse onto the original slot.

        Repro of PR #373 review finding #3: the original code popped the
        request before waiting, so a late same-name chime created a fresh
        entry that ran on the next loop iteration — both delivered.
        """
        from loom.autonomy.chime import ChimeRequest

        bot = _make_bot_stub()
        bot._sessions[111] = MagicMock()
        bot._run_chime_turn = AsyncMock(return_value=None)

        # Long in-flight user turn we control.
        user_done = asyncio.Event()

        async def fake_user_turn():
            await user_done.wait()
        user_task = asyncio.ensure_future(fake_user_turn())
        bot._running_turns[111] = user_task

        # First chime ("old") — gets queued, dispatcher reaches the wait.
        assert await bot.deliver_chime(
            ChimeRequest(
                schedule_name="morning",
                intent="old",
                fired_at=datetime.now(timezone.utc),
                target={"type": "discord_thread", "id": "111"},
            )
        )
        await asyncio.sleep(0)  # let dispatcher reach `await existing`

        # Second chime ("new") for the same schedule — must overwrite.
        assert await bot.deliver_chime(
            ChimeRequest(
                schedule_name="morning",
                intent="new",
                fired_at=datetime.now(timezone.utc),
                target={"type": "discord_thread", "id": "111"},
            )
        )

        # Release the user turn so the dispatcher proceeds.
        user_done.set()
        await user_task
        for _ in range(20):
            if bot._run_chime_turn.await_count >= 1 and not bot._chime_pending:
                break
            await asyncio.sleep(0)

        assert bot._run_chime_turn.await_count == 1, (
            "same-name chime arriving during user-turn wait was not deduped"
        )
        delivered_req = bot._run_chime_turn.await_args.args[1]
        assert delivered_req.intent == "new"


# ===========================================================================
# Chime permission scoping (PR #373 review finding #1)
# ===========================================================================


class TestChimePermissions:
    @pytest.mark.asyncio
    async def test_chime_applies_and_revokes_schedule_grants(self):
        """allowed_tools / scope_grants on the schedule must be pre-authorized
        before the chime turn and revoked after — same invariant as
        ``AutonomyDaemon._run_agent`` for independent runs."""
        from loom.autonomy.chime import ChimeRequest

        bot = _make_bot_stub()

        # Fake session.perm capturing all permission calls.
        session = MagicMock()
        session.perm.session_authorized = set()
        authorized: list[str] = []
        revoked: list[str] = []
        granted: list = []
        revoke_matching_calls: list = []

        def _authorize(name):
            authorized.append(name)
            session.perm.session_authorized.add(name)
        def _revoke(name):
            revoked.append(name)
        def _grant(g):
            granted.append(g)
        def _revoke_matching(pred):
            revoke_matching_calls.append(pred)

        session.perm.authorize = MagicMock(side_effect=_authorize)
        session.perm.revoke = MagicMock(side_effect=_revoke)
        session.perm.grant = MagicMock(side_effect=_grant)
        session.perm.revoke_matching = MagicMock(side_effect=_revoke_matching)
        bot._sessions[111] = session

        applied_during_turn: dict[str, list[str]] = {}

        async def fake_turn(thread_id, req):
            # Snapshot the state *during* the turn — should reflect grants.
            applied_during_turn["authorized"] = list(authorized)
            applied_during_turn["granted_count"] = len(granted)
        bot._run_chime_turn = AsyncMock(side_effect=fake_turn)

        req = ChimeRequest(
            schedule_name="morning",
            intent="hi",
            fired_at=datetime.now(timezone.utc),
            target={"type": "discord_thread", "id": "111"},
            allowed_tools=("write_file", "memorize"),
            scope_grants=(
                {"resource": "path", "action": "write", "selector": "news/*"},
            ),
        )
        assert await bot.deliver_chime(req)

        # Wait for dispatcher to fully complete — including the finally
        # block where revoke happens. The done_callback that clears
        # _chime_dispatcher fires after the dispatcher coroutine fully
        # returns, so it's a safe completion signal.
        for _ in range(50):
            if not bot._chime_dispatcher and not bot._chime_pending:
                break
            await asyncio.sleep(0)

        # Authorized during the turn, revoked after.
        assert applied_during_turn["authorized"] == ["write_file", "memorize"]
        assert applied_during_turn["granted_count"] == 1
        assert sorted(revoked) == ["memorize", "write_file"]
        assert len(revoke_matching_calls) == 1

        # The scope_grants ScopeGrant should carry an autonomy:<schedule> source
        # so revoke_matching's predicate can target it.
        assert granted[0].source == "autonomy:morning"
        assert revoke_matching_calls[0](granted[0]) is True
