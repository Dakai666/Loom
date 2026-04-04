"""
Autonomy Daemon — loads triggers from loom.toml, runs the evaluator
in a background loop, and executes PlannedActions.

This is the bridge between the declarative config and the live runtime.

Usage (from CLI):
    loom autonomy start   # foreground
    loom autonomy status  # show registered triggers
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from loom.autonomy.evaluator import TriggerEvaluator
from loom.autonomy.history import TriggerHistory
from loom.autonomy.planner import ActionPlanner, PlannedAction, ActionDecision
from loom.autonomy.triggers import CronTrigger, EventTrigger, ConditionTrigger
from loom.notify.confirm import ConfirmFlow
from loom.notify.router import NotificationRouter
from loom.notify.types import Notification, NotificationType


class AutonomyDaemon:
    """
    Manages the full autonomy lifecycle:
      - Loads triggers from config
      - Runs TriggerEvaluator in the background
      - Routes PlannedActions → Confirm / Execute
    """

    def __init__(
        self,
        notify_router: NotificationRouter,
        confirm_flow: ConfirmFlow,
        loom_session=None,   # LoomSession for executing prompts
        db=None,            # open aiosqlite.Connection for trigger_history persistence
    ) -> None:
        self._notify = notify_router
        self._confirm = confirm_flow
        self._session = loom_session

        history = TriggerHistory(db) if db is not None else None
        self._planner = ActionPlanner(
            semantic_memory=getattr(loom_session, "_semantic", None) if loom_session else None,
        )
        self._evaluator = TriggerEvaluator(on_fire=self._planner.handle, history=history)
        # Intercept planned actions
        self._planner_handle_orig = self._planner.handle
        self._evaluator._on_fire = self._on_trigger_fire

    async def _on_trigger_fire(self, trigger, context):
        plan = await self._planner.handle(trigger, context)
        await self._execute_plan(plan)

    async def _execute_plan(self, plan: PlannedAction) -> None:
        if plan.decision == ActionDecision.SKIP:
            return

        if plan.decision == ActionDecision.EXECUTE:
            await self._run_agent(plan)
            return

        # NOTIFY or HOLD — send confirmation request
        notif = Notification(
            type=NotificationType.CONFIRM,
            title=f"Loom autonomy: {plan.trigger_name}",
            body=plan.intent,
            trigger_name=plan.trigger_name,
            timeout_seconds=60 if plan.decision == ActionDecision.NOTIFY else 300,
            thread_id=plan.context.get("notify_thread_id", 0),
        )

        result = await self._confirm.ask(notif)

        from loom.notify.types import ConfirmResult
        if result == ConfirmResult.APPROVED:
            await self._run_agent(plan)
        elif result == ConfirmResult.TIMEOUT and plan.decision == ActionDecision.NOTIFY:
            # Guarded timeout → skip (do not downgrade to execute automatically)
            await self._notify.send(Notification(
                type=NotificationType.INFO,
                title=f"Autonomy: {plan.trigger_name} skipped",
                body="No response within timeout — action was skipped.",
                trigger_name=plan.trigger_name,
            ))

    async def _run_agent(self, plan: PlannedAction) -> None:
        if self._session is None:
            return

        # Collect text output from stream_turn (the session's interactive loop).
        # We run it as an async generator and pull TurnDone to confirm completion.
        try:
            # stream_turn is an async generator — consume it fully so the session
            # processes the prompt and executes all tool calls.
            output_chunks: list[str] = []
            async for event in self._session.stream_turn(plan.prompt):
                # Collect streaming text without importing platform event types
                if hasattr(event, "text") and isinstance(event.text, str):
                    output_chunks.append(event.text)

            thread_id = plan.context.get("notify_thread_id", 0)
            response = "".join(output_chunks).strip()
            if response:
                await self._notify.send(Notification(
                    type=NotificationType.REPORT,
                    title=f"Autonomy result: {plan.trigger_name}",
                    body=response[:1000],
                    trigger_name=plan.trigger_name,
                    thread_id=thread_id,
                ))
        except Exception as exc:
            await self._notify.send(Notification(
                type=NotificationType.ALERT,
                title=f"Autonomy error: {plan.trigger_name}",
                body=str(exc),
                trigger_name=plan.trigger_name,
                thread_id=plan.context.get("notify_thread_id", 0),
            ))

    # ------------------------------------------------------------------
    # Config loading
    # ------------------------------------------------------------------

    def load_config(self, config_path: str | Path) -> int:
        """
        Load triggers from a loom.toml file.
        Returns the number of triggers registered.
        """
        import tomllib

        path = Path(config_path)
        if not path.exists():
            return 0

        with open(path, "rb") as f:
            config = tomllib.load(f)

        autonomy_cfg = config.get("autonomy", {})
        if not autonomy_cfg.get("enabled", False):
            return 0

        count = 0
        for sched in autonomy_cfg.get("schedules", []):
            trigger = CronTrigger(
                name=sched["name"],
                intent=sched["intent"],
                cron=sched.get("cron", "0 9 * * 1-5"),
                timezone=sched.get("timezone", "UTC"),
                trust_level=sched.get("trust_level", "guarded"),
                notify=sched.get("notify", True),
                notify_thread_id=sched.get("notify_thread", 0),
            )
            self._evaluator.register(trigger)
            count += 1

        for evt in autonomy_cfg.get("triggers", []):
            trigger = EventTrigger(
                name=evt["name"],
                intent=evt["intent"],
                event_name=evt.get("event", evt["name"]),
                trust_level=evt.get("trust_level", "guarded"),
                notify=evt.get("notify", True),
                notify_thread_id=evt.get("notify_thread", 0),
            )
            self._evaluator.register(trigger)
            count += 1

        return count

    # ------------------------------------------------------------------
    # Runtime
    # ------------------------------------------------------------------

    async def start(self, poll_interval: float = 60.0) -> None:
        """Run the evaluator loop (blocking)."""
        await self._evaluator.run_forever(poll_interval=poll_interval)

    @property
    def evaluator(self) -> TriggerEvaluator:
        return self._evaluator

    def registered_triggers(self) -> list[dict]:
        return [
            {
                "name": t.name,
                "kind": t.kind.value,
                "intent": t.intent,
                "trust_level": t.trust_level,
                "enabled": t.enabled,
            }
            for t in self._evaluator.list()
        ]
