"""
Tests for the Autonomy Engine and Notification Layer:

  Triggers
    - CronTrigger: cron validation, should_fire matching (exact, range, list, step, *)
    - EventTrigger / ConditionTrigger: kind, evaluate

  TriggerEvaluator
    - cron deduplication within same minute
    - event emission fires matching triggers only
    - condition polling fires when condition returns True
    - on_fire callback called with correct args

  ActionPlanner
    - trust level → decision mapping (safe→EXECUTE, guarded+notify→NOTIFY, critical→HOLD)
    - disabled trigger → SKIP
    - prompt contains intent

  NotificationTypes / Notification dataclass
  NotificationRouter
    - fan-out to multiple notifiers concurrently
    - per-channel send
    - errors in one channel don't block others

  ConfirmFlow
    - approved / denied paths
    - no wait_fn → auto-approved
    - timeout returns TIMEOUT

  CLINotifier (smoke)
  WebhookNotifier: serialize, push_reply unblocks wait_reply

  AutonomyDaemon
    - load_config registers correct triggers
    - load_config with disabled autonomy registers nothing
    - execute_plan routes to correct path
"""

import asyncio
import json
import pytest
import pytest_asyncio
from datetime import datetime, UTC
from unittest.mock import MagicMock, AsyncMock

from loom.autonomy.triggers import (
    CronTrigger, EventTrigger, ConditionTrigger,
    TriggerKind, _cron_matches,
)
from loom.autonomy.evaluator import TriggerEvaluator
from loom.autonomy.planner import ActionPlanner, ActionDecision, PlannedAction
from loom.notify.types import Notification, NotificationType, ConfirmResult
from loom.notify.router import NotificationRouter, BaseNotifier
from loom.notify.confirm import ConfirmFlow
from loom.notify.adapters.cli import CLINotifier
from loom.notify.adapters.webhook import WebhookNotifier


# ===========================================================================
# Helpers
# ===========================================================================

def dt(minute=0, hour=9, day=1, month=4, weekday=1):
    """Build a datetime for testing cron matching. weekday: 0=Mon."""
    # Map weekday to a real date — 2026-04-01 is Wednesday (weekday=2)
    # Base: 2026-03-30 is Monday (weekday=0)
    from datetime import date
    base = date(2026, 3, 30)  # Monday
    import calendar
    # Find a date in April 2026 with the right weekday
    target = date(2026, month, day)
    return datetime(
        target.year, target.month, target.day,
        hour, minute, 0, tzinfo=UTC,
    )


def make_confirm_notif(timeout=5):
    return Notification(
        type=NotificationType.CONFIRM,
        title="Test",
        body="Allow action?",
        timeout_seconds=timeout,
    )


# ===========================================================================
# CronTrigger — field matching
# ===========================================================================

class TestCronMatching:
    def test_wildcard_matches_any(self):
        assert _cron_matches("*", 0) is True
        assert _cron_matches("*", 59) is True

    def test_exact_value(self):
        assert _cron_matches("9", 9) is True
        assert _cron_matches("9", 10) is False

    def test_range(self):
        assert _cron_matches("1-5", 3) is True
        assert _cron_matches("1-5", 1) is True
        assert _cron_matches("1-5", 5) is True
        assert _cron_matches("1-5", 6) is False

    def test_list(self):
        assert _cron_matches("1,3,5", 3) is True
        assert _cron_matches("1,3,5", 4) is False

    def test_step(self):
        assert _cron_matches("*/5", 0) is True
        assert _cron_matches("*/5", 5) is True
        assert _cron_matches("*/5", 15) is True
        assert _cron_matches("*/5", 3) is False

    def test_combined_list_and_range(self):
        assert _cron_matches("1-3,7", 2) is True
        assert _cron_matches("1-3,7", 7) is True
        assert _cron_matches("1-3,7", 5) is False


class TestCronTrigger:
    def test_valid_expression_accepted(self):
        t = CronTrigger(name="t", intent="x", cron="0 9 * * 1-5")
        assert t.kind == TriggerKind.CRON

    def test_invalid_expression_raises(self):
        with pytest.raises(ValueError, match="5 fields"):
            CronTrigger(name="t", intent="x", cron="0 9 * *")

    def test_should_fire_exact_match(self):
        t = CronTrigger(name="t", intent="x", cron="30 14 * * *")
        assert t.should_fire(datetime(2026, 4, 2, 14, 30, tzinfo=UTC)) is True
        assert t.should_fire(datetime(2026, 4, 2, 14, 31, tzinfo=UTC)) is False

    def test_should_fire_weekday_range(self):
        # "0 9 * * 1-5" → weekdays Mon–Fri; Python weekday() 0=Mon … 4=Fri
        t = CronTrigger(name="t", intent="x", cron="0 9 * * 1-5")
        mon = datetime(2026, 3, 30, 9, 0, tzinfo=UTC)   # Monday
        sat = datetime(2026, 4, 4, 9, 0, tzinfo=UTC)    # Saturday (weekday=5)
        assert t.should_fire(mon) is True
        assert t.should_fire(sat) is False

    def test_should_fire_wildcard_all_match(self):
        t = CronTrigger(name="t", intent="x", cron="* * * * *")
        assert t.should_fire(datetime.now(UTC)) is True


class TestEventTrigger:
    def test_kind(self):
        t = EventTrigger(name="t", intent="x", event_name="deploy_done")
        assert t.kind == TriggerKind.EVENT

    def test_attributes(self):
        t = EventTrigger(name="on_deploy", intent="run tests", event_name="deploy")
        assert t.event_name == "deploy"


class TestConditionTrigger:
    def test_kind(self):
        t = ConditionTrigger(name="t", intent="x", condition_fn=lambda: True)
        assert t.kind == TriggerKind.CONDITION

    def test_evaluate_true(self):
        t = ConditionTrigger(name="t", intent="x", condition_fn=lambda: True)
        assert t.evaluate() is True

    def test_evaluate_false(self):
        t = ConditionTrigger(name="t", intent="x", condition_fn=lambda: False)
        assert t.evaluate() is False

    def test_evaluate_exception_returns_false(self):
        def boom(): raise RuntimeError("fail")
        t = ConditionTrigger(name="t", intent="x", condition_fn=boom)
        assert t.evaluate() is False

    def test_evaluate_no_fn_returns_false(self):
        t = ConditionTrigger(name="t", intent="x")
        assert t.evaluate() is False


# ===========================================================================
# TriggerEvaluator
# ===========================================================================

class TestTriggerEvaluator:
    @pytest.mark.asyncio
    async def test_cron_fires_on_match(self):
        fired = []
        async def on_fire(trigger, ctx):
            fired.append(trigger.name)

        ev = TriggerEvaluator(on_fire=on_fire)
        ev.register(CronTrigger(name="check", intent="do it", cron="0 9 * * *"))
        await ev.evaluate_cron(datetime(2026, 4, 2, 9, 0, tzinfo=UTC))
        assert "check" in fired

    @pytest.mark.asyncio
    async def test_cron_does_not_fire_on_mismatch(self):
        fired = []
        async def on_fire(trigger, ctx):
            fired.append(trigger.name)

        ev = TriggerEvaluator(on_fire=on_fire)
        ev.register(CronTrigger(name="check", intent="do it", cron="0 9 * * *"))
        await ev.evaluate_cron(datetime(2026, 4, 2, 10, 0, tzinfo=UTC))
        assert fired == []

    @pytest.mark.asyncio
    async def test_cron_dedup_same_minute(self):
        fired = []
        async def on_fire(trigger, ctx):
            fired.append(trigger.name)

        ev = TriggerEvaluator(on_fire=on_fire)
        ev.register(CronTrigger(name="dup", intent="x", cron="0 9 * * *"))
        t = datetime(2026, 4, 2, 9, 0, tzinfo=UTC)
        await ev.evaluate_cron(t)
        await ev.evaluate_cron(t)  # same minute — should not re-fire
        assert fired.count("dup") == 1

    @pytest.mark.asyncio
    async def test_cron_fires_again_next_minute(self):
        fired = []
        async def on_fire(trigger, ctx):
            fired.append(trigger.name)

        ev = TriggerEvaluator(on_fire=on_fire)
        ev.register(CronTrigger(name="dup", intent="x", cron="* * * * *"))
        await ev.evaluate_cron(datetime(2026, 4, 2, 9, 0, tzinfo=UTC))
        await ev.evaluate_cron(datetime(2026, 4, 2, 9, 1, tzinfo=UTC))
        assert fired.count("dup") == 2

    @pytest.mark.asyncio
    async def test_event_fires_matching_only(self):
        fired = []
        async def on_fire(trigger, ctx):
            fired.append(trigger.name)

        ev = TriggerEvaluator(on_fire=on_fire)
        ev.register(EventTrigger(name="on_deploy", intent="x", event_name="deploy"))
        ev.register(EventTrigger(name="on_rollback", intent="x", event_name="rollback"))
        await ev.emit("deploy")
        assert "on_deploy" in fired
        assert "on_rollback" not in fired

    @pytest.mark.asyncio
    async def test_condition_fires_when_true(self):
        fired = []
        async def on_fire(trigger, ctx):
            fired.append(trigger.name)

        state = {"val": True}
        ev = TriggerEvaluator(on_fire=on_fire)
        ev.register(ConditionTrigger(name="cpu_high", intent="x",
                                     condition_fn=lambda: state["val"]))
        await ev.poll_conditions()
        assert "cpu_high" in fired

    @pytest.mark.asyncio
    async def test_condition_does_not_fire_when_false(self):
        fired = []
        async def on_fire(trigger, ctx):
            fired.append(trigger.name)

        ev = TriggerEvaluator(on_fire=on_fire)
        ev.register(ConditionTrigger(name="c", intent="x", condition_fn=lambda: False))
        await ev.poll_conditions()
        assert fired == []

    @pytest.mark.asyncio
    async def test_disabled_trigger_does_not_fire(self):
        fired = []
        async def on_fire(trigger, ctx):
            fired.append(trigger.name)

        ev = TriggerEvaluator(on_fire=on_fire)
        ev.register(CronTrigger(name="off", intent="x",
                                cron="* * * * *", enabled=False))
        await ev.evaluate_cron(datetime.now(UTC))
        assert fired == []

    @pytest.mark.asyncio
    async def test_no_on_fire_does_not_crash(self):
        ev = TriggerEvaluator()  # no callback
        ev.register(CronTrigger(name="t", intent="x", cron="* * * * *"))
        await ev.evaluate_cron(datetime.now(UTC))  # should not raise

    @pytest.mark.asyncio
    async def test_emit_returns_fired_names(self):
        ev = TriggerEvaluator(on_fire=AsyncMock())
        ev.register(EventTrigger(name="t1", intent="x", event_name="go"))
        ev.register(EventTrigger(name="t2", intent="x", event_name="stop"))
        fired = await ev.emit("go")
        assert fired == ["t1"]

    def test_unregister_removes_trigger(self):
        ev = TriggerEvaluator()
        ev.register(CronTrigger(name="t", intent="x", cron="* * * * *"))
        ev.unregister("t")
        assert ev.list() == []


# ===========================================================================
# ActionPlanner
# ===========================================================================

class TestActionPlanner:
    @pytest.mark.asyncio
    async def test_safe_trigger_gives_execute(self):
        planner = ActionPlanner()
        t = CronTrigger(name="t", intent="do x", cron="* * * * *",
                        trust_level="safe")
        plan = await planner.handle(t, {})
        assert plan.decision == ActionDecision.EXECUTE

    @pytest.mark.asyncio
    async def test_guarded_notify_true_gives_notify(self):
        planner = ActionPlanner()
        t = CronTrigger(name="t", intent="do x", cron="* * * * *",
                        trust_level="guarded", notify=True)
        plan = await planner.handle(t, {})
        assert plan.decision == ActionDecision.NOTIFY

    @pytest.mark.asyncio
    async def test_guarded_notify_false_gives_execute(self):
        planner = ActionPlanner()
        t = CronTrigger(name="t", intent="do x", cron="* * * * *",
                        trust_level="guarded", notify=False)
        plan = await planner.handle(t, {})
        assert plan.decision == ActionDecision.EXECUTE

    @pytest.mark.asyncio
    async def test_critical_gives_hold(self):
        planner = ActionPlanner()
        t = CronTrigger(name="t", intent="nuke", cron="* * * * *",
                        trust_level="critical")
        plan = await planner.handle(t, {})
        assert plan.decision == ActionDecision.HOLD

    @pytest.mark.asyncio
    async def test_disabled_gives_skip(self):
        planner = ActionPlanner()
        t = CronTrigger(name="t", intent="x", cron="* * * * *",
                        trust_level="safe", enabled=False)
        plan = await planner.handle(t, {})
        assert plan.decision == ActionDecision.SKIP

    @pytest.mark.asyncio
    async def test_prompt_contains_intent(self):
        planner = ActionPlanner()
        t = CronTrigger(name="t", intent="Review yesterday's progress",
                        cron="* * * * *", trust_level="safe")
        plan = await planner.handle(t, {})
        assert "Review yesterday's progress" in plan.prompt

    @pytest.mark.asyncio
    async def test_requires_confirmation_for_notify(self):
        planner = ActionPlanner()
        t = CronTrigger(name="t", intent="x", cron="* * * * *",
                        trust_level="guarded", notify=True)
        plan = await planner.handle(t, {})
        assert plan.requires_confirmation is True

    @pytest.mark.asyncio
    async def test_no_confirmation_for_execute(self):
        planner = ActionPlanner()
        t = CronTrigger(name="t", intent="x", cron="* * * * *",
                        trust_level="safe")
        plan = await planner.handle(t, {})
        assert plan.requires_confirmation is False

    @pytest.mark.asyncio
    async def test_context_assembled(self):
        planner = ActionPlanner()
        t = CronTrigger(name="my_trigger", intent="x",
                        cron="* * * * *", trust_level="safe")
        fire_ctx = {"triggered_at": "2026-04-02T09:00:00", "source": "cron"}
        plan = await planner.handle(t, fire_ctx)
        assert plan.context["trigger_name"] == "my_trigger"
        assert plan.context["source"] == "cron"


# ===========================================================================
# NotificationRouter
# ===========================================================================

class TestNotificationRouter:
    def _make_notifier(self, channel: str, raises: bool = False):
        class N(BaseNotifier):
            pass
        n = N()
        n.channel = channel
        n.sent = []
        async def send(notif):
            if raises:
                raise RuntimeError("boom")
            n.sent.append(notif)
        n.send = send
        return n

    @pytest.mark.asyncio
    async def test_send_reaches_all_notifiers(self):
        router = NotificationRouter()
        a = self._make_notifier("a")
        b = self._make_notifier("b")
        router.register(a).register(b)

        notif = Notification(type=NotificationType.INFO,
                             title="T", body="B")
        results = await router.send(notif)
        assert results["a"] is True
        assert results["b"] is True
        assert len(a.sent) == 1
        assert len(b.sent) == 1

    @pytest.mark.asyncio
    async def test_error_in_one_channel_does_not_block_others(self):
        router = NotificationRouter()
        good = self._make_notifier("good")
        bad  = self._make_notifier("bad", raises=True)
        router.register(good).register(bad)

        notif = Notification(type=NotificationType.ALERT,
                             title="T", body="B")
        results = await router.send(notif)
        assert results["good"] is True
        assert results["bad"] is False
        assert len(good.sent) == 1

    @pytest.mark.asyncio
    async def test_send_to_specific_channel(self):
        router = NotificationRouter()
        a = self._make_notifier("a")
        b = self._make_notifier("b")
        router.register(a).register(b)

        notif = Notification(type=NotificationType.INFO,
                             title="T", body="B")
        ok = await router.send_to("a", notif)
        assert ok is True
        assert len(a.sent) == 1
        assert len(b.sent) == 0

    @pytest.mark.asyncio
    async def test_send_to_missing_channel_returns_false(self):
        router = NotificationRouter()
        notif = Notification(type=NotificationType.INFO,
                             title="T", body="B")
        ok = await router.send_to("nonexistent", notif)
        assert ok is False

    def test_channels_lists_registered(self):
        router = NotificationRouter()
        router.register(self._make_notifier("x"))
        router.register(self._make_notifier("y"))
        assert set(router.channels) == {"x", "y"}


# ===========================================================================
# ConfirmFlow
# ===========================================================================

class TestConfirmFlow:
    @pytest.mark.asyncio
    async def test_approved_path(self):
        sent = []
        async def send(n): sent.append(n)
        async def wait(n): return ConfirmResult.APPROVED

        flow = ConfirmFlow(send_fn=send, wait_fn=wait)
        result = await flow.ask(make_confirm_notif())
        assert result == ConfirmResult.APPROVED
        assert len(sent) == 1

    @pytest.mark.asyncio
    async def test_denied_path(self):
        async def send(n): pass
        async def wait(n): return ConfirmResult.DENIED

        flow = ConfirmFlow(send_fn=send, wait_fn=wait)
        result = await flow.ask(make_confirm_notif())
        assert result == ConfirmResult.DENIED

    @pytest.mark.asyncio
    async def test_timeout_returns_timeout(self):
        async def send(n): pass
        async def wait(n):
            await asyncio.sleep(100)   # never resolves in time
            return ConfirmResult.APPROVED

        flow = ConfirmFlow(send_fn=send, wait_fn=wait,
                          default_on_timeout=ConfirmResult.TIMEOUT)
        notif = make_confirm_notif(timeout=1)
        result = await flow.ask(notif)
        assert result == ConfirmResult.TIMEOUT

    @pytest.mark.asyncio
    async def test_no_wait_fn_auto_approves(self):
        sent = []
        async def send(n): sent.append(n)

        flow = ConfirmFlow(send_fn=send, wait_fn=None)
        result = await flow.ask(make_confirm_notif())
        assert result == ConfirmResult.APPROVED

    @pytest.mark.asyncio
    async def test_wrong_notification_type_raises(self):
        async def send(n): pass
        flow = ConfirmFlow(send_fn=send)
        info_notif = Notification(type=NotificationType.INFO,
                                  title="T", body="B")
        with pytest.raises(AssertionError):
            await flow.ask(info_notif)


# ===========================================================================
# CLINotifier (smoke test)
# ===========================================================================

class TestCLINotifier:
    @pytest.mark.asyncio
    async def test_send_does_not_crash(self):
        console = MagicMock()
        notifier = CLINotifier(console=console)
        notif = Notification(type=NotificationType.INFO,
                             title="Hello", body="World")
        await notifier.send(notif)
        assert console.print.called

    @pytest.mark.asyncio
    async def test_send_confirm_type(self):
        console = MagicMock()
        notifier = CLINotifier(console=console)
        notif = Notification(type=NotificationType.CONFIRM,
                             title="Confirm", body="ok?")
        await notifier.send(notif)
        assert console.print.called


# ===========================================================================
# WebhookNotifier — serialize + push_reply
# ===========================================================================

class TestWebhookNotifier:
    def test_serialize_produces_valid_json(self):
        n = WebhookNotifier(url="http://example.com/hook")
        notif = Notification(type=NotificationType.CONFIRM,
                             title="T", body="B",
                             trigger_name="deploy",
                             timeout_seconds=30)
        data = WebhookNotifier._serialize(notif)
        parsed = json.loads(data)
        assert parsed["type"] == "confirm"
        assert parsed["title"] == "T"
        assert parsed["trigger_name"] == "deploy"
        assert parsed["timeout_seconds"] == 30
        assert "created_at" in parsed

    @pytest.mark.asyncio
    async def test_push_reply_unblocks_wait_reply(self):
        n = WebhookNotifier(url="http://localhost/dummy")
        notif = Notification(type=NotificationType.CONFIRM,
                             title="T", body="B")
        # Pre-seed queue (skipping actual HTTP)
        n._reply_queues[notif.id] = asyncio.Queue(maxsize=1)

        async def push_later():
            await asyncio.sleep(0.05)
            n.push_reply(notif.id, ConfirmResult.APPROVED)

        asyncio.create_task(push_later())
        result = await asyncio.wait_for(n.wait_reply(notif), timeout=2.0)
        assert result == ConfirmResult.APPROVED

    def test_push_reply_on_unknown_id_is_noop(self):
        n = WebhookNotifier(url="http://example.com")
        n.push_reply("nonexistent-id", ConfirmResult.DENIED)  # should not raise


# ===========================================================================
# AutonomyDaemon — config loading
# ===========================================================================

class TestAutonomyDaemonConfig:
    def _make_daemon(self):
        from loom.autonomy.daemon import AutonomyDaemon
        from loom.notify.router import NotificationRouter
        from loom.notify.confirm import ConfirmFlow

        sent = []
        async def send(n): sent.append(n)
        router = NotificationRouter()
        flow = ConfirmFlow(send_fn=send)
        return AutonomyDaemon(notify_router=router, confirm_flow=flow)

    def test_load_config_enabled(self, tmp_path):
        toml_content = """
[autonomy]
enabled = true

[[autonomy.schedules]]
name = "daily_review"
cron = "0 9 * * 1-5"
intent = "Review progress"
trust_level = "guarded"
notify = true

[[autonomy.schedules]]
name = "weekly_prune"
cron = "0 2 * * 0"
intent = "Prune old memories"
trust_level = "safe"
notify = false
"""
        config_file = tmp_path / "loom.toml"
        config_file.write_text(toml_content, encoding="utf-8")

        daemon = self._make_daemon()
        count = daemon.load_config(config_file)
        assert count == 2

        triggers = daemon.registered_triggers()
        names = {t["name"] for t in triggers}
        assert names == {"daily_review", "weekly_prune"}

    def test_load_config_disabled_returns_zero(self, tmp_path):
        toml_content = """
[autonomy]
enabled = false

[[autonomy.schedules]]
name = "never_loaded"
cron = "* * * * *"
intent = "x"
"""
        config_file = tmp_path / "loom.toml"
        config_file.write_text(toml_content, encoding="utf-8")

        daemon = self._make_daemon()
        count = daemon.load_config(config_file)
        assert count == 0
        assert daemon.registered_triggers() == []

    def test_load_config_missing_file_returns_zero(self, tmp_path):
        daemon = self._make_daemon()
        count = daemon.load_config(tmp_path / "nonexistent.toml")
        assert count == 0

    def test_load_config_with_event_triggers(self, tmp_path):
        toml_content = """
[autonomy]
enabled = true

[[autonomy.triggers]]
name = "on_error_spike"
event = "error_rate_threshold"
intent = "Analyse errors"
trust_level = "guarded"
notify = true
"""
        config_file = tmp_path / "loom.toml"
        config_file.write_text(toml_content, encoding="utf-8")

        daemon = self._make_daemon()
        count = daemon.load_config(config_file)
        assert count == 1
        assert daemon.registered_triggers()[0]["kind"] == "event"

    @pytest.mark.asyncio
    async def test_execute_plan_safe_calls_agent(self):
        from loom.autonomy.daemon import AutonomyDaemon
        from loom.autonomy.planner import PlannedAction, ActionDecision
        from loom.core.harness.permissions import TrustLevel
        from loom.notify.router import NotificationRouter
        from loom.notify.confirm import ConfirmFlow

        session = MagicMock()
        session.run_turn = AsyncMock(return_value="Done.")
        session._semantic = None

        sent = []
        async def send(n): sent.append(n)

        router = NotificationRouter()
        flow = ConfirmFlow(send_fn=send)
        daemon = AutonomyDaemon(
            notify_router=router, confirm_flow=flow, loom_session=session
        )

        plan = PlannedAction(
            trigger_name="test",
            intent="do something",
            decision=ActionDecision.EXECUTE,
            trust_level=TrustLevel.SAFE,
            prompt="do something",
        )
        await daemon._execute_plan(plan)
        session.run_turn.assert_called_once_with("do something")

    @pytest.mark.asyncio
    async def test_execute_plan_skip_does_not_call_agent(self):
        from loom.autonomy.daemon import AutonomyDaemon
        from loom.autonomy.planner import PlannedAction, ActionDecision
        from loom.core.harness.permissions import TrustLevel
        from loom.notify.router import NotificationRouter
        from loom.notify.confirm import ConfirmFlow

        session = MagicMock()
        session.run_turn = AsyncMock(return_value="")
        session._semantic = None

        async def send(n): pass
        router = NotificationRouter()
        flow = ConfirmFlow(send_fn=send)
        daemon = AutonomyDaemon(
            notify_router=router, confirm_flow=flow, loom_session=session
        )

        plan = PlannedAction(
            trigger_name="t", intent="x",
            decision=ActionDecision.SKIP,
            trust_level=TrustLevel.SAFE,
        )
        await daemon._execute_plan(plan)
        session.run_turn.assert_not_called()
