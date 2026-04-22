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
import hashlib
import json as _json
import logging
from datetime import datetime, UTC
from pathlib import Path

logger = logging.getLogger(__name__)

from loom.autonomy.evaluator import TriggerEvaluator
from loom.autonomy.history import TriggerHistory
from loom.autonomy.planner import ActionPlanner, PlannedAction, ActionDecision
from loom.autonomy.triggers import CronTrigger, EventTrigger, ConditionTrigger
from loom.core.infra import AbortController, wait_aborted
from loom.notify.confirm import ConfirmFlow
from loom.notify.router import NotificationRouter
from loom.notify.types import Notification, NotificationType


# ---------------------------------------------------------------------------
# Issue #91: autonomy config tamper detection
# ---------------------------------------------------------------------------

_VALID_TRUST_LEVELS = {"safe", "guarded", "critical"}
_CONFIG_HASH_PATH = Path.home() / ".loom" / "autonomy_config.hash"


def _hash_autonomy_section(autonomy_cfg: dict) -> str:
    """Deterministic SHA-256 of the autonomy config section."""
    canonical = _json.dumps(autonomy_cfg, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _validate_trust_level(value: str, trigger_name: str) -> str:
    """Validate and return trust_level, defaulting to 'guarded' on invalid."""
    if value not in _VALID_TRUST_LEVELS:
        logger.warning(
            "[autonomy] trigger %r has invalid trust_level=%r, defaulting to 'guarded'",
            trigger_name, value,
        )
        return "guarded"
    return value


def _resolve_attachments(
    workspace: Path,
    patterns: list[str],
    since: datetime,
) -> list[Path]:
    """Expand ``attach_outputs`` globs relative to *workspace* and return
    regular files whose mtime is at or after *since*.

    The mtime filter is intentional: it excludes stale files from previous
    runs that happen to sit in the matched paths.  Patterns that escape
    the workspace (absolute paths or ``..``) are ignored so a malformed
    config can't attach arbitrary files from disk.
    """
    if not patterns:
        return []

    cutoff = since.timestamp()
    results: list[Path] = []
    seen: set[Path] = set()

    for pat in patterns:
        if not pat or Path(pat).is_absolute() or ".." in Path(pat).parts:
            logger.debug("[autonomy] attach_outputs: rejected unsafe pattern %r", pat)
            continue
        try:
            matches = list(workspace.glob(pat))
        except (OSError, ValueError) as exc:
            logger.debug("[autonomy] attach_outputs: glob %r failed: %s", pat, exc)
            continue
        if not matches:
            logger.debug("[autonomy] attach_outputs: pattern %r matched nothing", pat)
            continue
        for p in matches:
            try:
                if not p.is_file():
                    logger.debug("[autonomy] attach_outputs: skipped non-file %s", p)
                    continue
                if p.stat().st_mtime < cutoff:
                    logger.debug("[autonomy] attach_outputs: skipped stale %s", p)
                    continue
            except OSError as exc:
                logger.debug("[autonomy] attach_outputs: stat failed for %s: %s", p, exc)
                continue
            resolved = p.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            results.append(p)

    return results


def _check_config_integrity(autonomy_cfg: dict) -> bool:
    """
    Compare current config hash against stored hash.
    Returns True if first load or match; False if mismatch.
    """
    current_hash = _hash_autonomy_section(autonomy_cfg)
    try:
        if _CONFIG_HASH_PATH.exists():
            stored = _CONFIG_HASH_PATH.read_text().strip()
            if stored != current_hash:
                logger.warning(
                    "[autonomy] CONFIG CHANGE DETECTED — autonomy section hash "
                    "mismatch. Stored=%s, Current=%s. Review loom.toml for tampering.",
                    stored[:12], current_hash[:12],
                )
                # Update hash after warning — the log entry is the audit trail.
                # Without this, every restart repeats the same warning forever.
                _CONFIG_HASH_PATH.write_text(current_hash)
                return False
        # First load or match — record/update
        _CONFIG_HASH_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CONFIG_HASH_PATH.write_text(current_hash)
        return True
    except Exception as exc:
        logger.warning("[autonomy] config integrity check failed: %s", exc)
        return True  # fail-open


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
        self._abort = AbortController()

        history = TriggerHistory(db) if db is not None else None
        # Issue #147 Phase C.2: pull semantic via the facade. ``_memory``
        # may be unset on test doubles or pre-start sessions — fall back
        # gracefully so autonomy continues to plan without context.
        _mem = getattr(loom_session, "_memory", None) if loom_session else None
        self._planner = ActionPlanner(
            semantic_memory=_mem.semantic if _mem is not None else None,
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

        logger.info("[autonomy] trigger=%s — starting agent run", plan.trigger_name)

        # Pre-authorize tools and scope grants declared in the schedule.
        # These are revoked after the turn completes so triggers don't
        # accumulate cross-schedule permissions.
        from loom.core.harness.scope import ScopeGrant
        _added_tools: list[str] = []
        for tool_name in plan.context.get("allowed_tools", []):
            if tool_name not in self._session.perm.session_authorized:
                self._session.perm.authorize(tool_name)
                _added_tools.append(tool_name)
        _added_grant_count = 0
        for g in plan.context.get("scope_grants", []):
            self._session.perm.grant(ScopeGrant(
                resource=g["resource"],
                action=g["action"],
                selector=g.get("selector", "*"),
                constraints=g.get("constraints", {}),
                source=f"autonomy:{plan.trigger_name}",
            ))
            _added_grant_count += 1

        # Record turn start before stream_turn so attachment resolution
        # can filter by mtime — files written during this turn only.
        turn_start = datetime.now(UTC)

        # Collect text output from stream_turn with origin="autonomy".
        # BlastRadiusMiddleware will auto-deny any tool call that isn't
        # covered by existing scope grants (no human to prompt).
        try:
            output_chunks: list[str] = []
            async for event in self._session.stream_turn(
                plan.prompt,
                abort_signal=self._abort.signal,
                origin="autonomy",
            ):
                # Collect streaming text without importing platform event types
                if hasattr(event, "text") and isinstance(event.text, str):
                    output_chunks.append(event.text)
                # ActionStateChange / ActionRolledBack events are silently
                # consumed — daemon doesn't need lifecycle visualization.

            thread_id = plan.context.get("notify_thread_id", 0)
            response = "".join(output_chunks).strip()
            logger.info("[autonomy] trigger=%s — completed (%d chars)", plan.trigger_name, len(response))

            attachments = _resolve_attachments(
                self._session.workspace,
                plan.context.get("attach_outputs", []),
                turn_start,
            )
            if attachments:
                logger.info(
                    "[autonomy] trigger=%s — attaching %d file(s): %s",
                    plan.trigger_name,
                    len(attachments),
                    [p.name for p in attachments],
                )

            if response or attachments:
                if response:
                    body = response[:1000]
                elif attachments:
                    body = f"📎 {len(attachments)} attachment(s)"
                else:
                    body = ""
                await self._notify.send(Notification(
                    type=NotificationType.REPORT,
                    title=f"Autonomy result: {plan.trigger_name}",
                    body=body,
                    trigger_name=plan.trigger_name,
                    thread_id=thread_id,
                    attachments=attachments,
                ))
        except Exception as exc:
            logger.error("[autonomy] trigger=%s — error: %s", plan.trigger_name, exc, exc_info=True)
            await self._notify.send(Notification(
                type=NotificationType.ALERT,
                title=f"Autonomy error: {plan.trigger_name}",
                body=str(exc),
                trigger_name=plan.trigger_name,
                thread_id=plan.context.get("notify_thread_id", 0),
            ))
        finally:
            # Revoke temporary grants so schedules don't leak permissions.
            for tool_name in _added_tools:
                self._session.perm.revoke(tool_name)
            if _added_grant_count:
                _src = f"autonomy:{plan.trigger_name}"
                self._session.perm.revoke_matching(lambda g: g.source == _src)

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

        # Issue #91: tamper detection (fail-open by design — logs warning only)
        if not _check_config_integrity(autonomy_cfg):
            logger.warning(
                "[autonomy] Proceeding despite config change. "
                "Review loom.toml and restart to update the stored hash."
            )

        count = 0
        for sched in autonomy_cfg.get("schedules", []):
            trigger = CronTrigger(
                name=sched["name"],
                intent=sched["intent"],
                cron=sched.get("cron", "0 9 * * 1-5"),
                timezone=sched.get("timezone", "UTC"),
                trust_level=_validate_trust_level(
                    sched.get("trust_level", "guarded"), sched["name"],
                ),
                notify=sched.get("notify", True),
                notify_thread_id=sched.get("notify_thread", 0),
                allowed_tools=sched.get("allowed_tools", []),
                scope_grants=sched.get("scope_grants", []),
                attach_outputs=sched.get("attach_outputs", []),
            )
            self._evaluator.register(trigger)
            count += 1

        for evt in autonomy_cfg.get("triggers", []):
            trigger = EventTrigger(
                name=evt["name"],
                intent=evt["intent"],
                event_name=evt.get("event", evt["name"]),
                trust_level=_validate_trust_level(
                    evt.get("trust_level", "guarded"), evt["name"],
                ),
                notify=evt.get("notify", True),
                notify_thread_id=evt.get("notify_thread", 0),
                allowed_tools=evt.get("allowed_tools", []),
                scope_grants=evt.get("scope_grants", []),
                attach_outputs=evt.get("attach_outputs", []),
            )
            self._evaluator.register(trigger)
            count += 1

        return count

    # ------------------------------------------------------------------
    # Runtime
    # ------------------------------------------------------------------

    async def start(self, poll_interval: float = 60.0) -> None:
        """Run the evaluator loop (blocking). Returns when stop() is called."""
        run_task = asyncio.ensure_future(
            self._evaluator.run_forever(poll_interval=poll_interval)
        )
        abort_task = asyncio.ensure_future(wait_aborted(self._abort.signal))
        done, pending = await asyncio.wait(
            [run_task, abort_task], return_when=asyncio.FIRST_COMPLETED
        )
        for t in pending:
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

    def stop(self) -> None:
        """Signal the daemon to stop — aborts any in-flight stream_turn() and exits the loop."""
        self._abort.abort()

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
