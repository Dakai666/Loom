"""
Action Planner — the decision pipeline that runs after a trigger fires.

Decision flow
-------------
Trigger fires
  → Context Assembly   (load relevant memory + reflection summary)
  → Intent Assessment  (what should be done, in what trust tier?)
  → Authorization Gate (safe → execute; guarded → notify+confirm; critical → must confirm)
  → Action / Notify fork

The planner does NOT execute tools directly — it produces a PlannedAction
that the runtime (CLI session or autonomy daemon) executes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from loom.core.harness.permissions import TrustLevel
from .triggers import TriggerDefinition


class ActionDecision(Enum):
    EXECUTE   = "execute"    # safe: run immediately
    NOTIFY    = "notify"     # guarded: notify user, wait for confirm
    HOLD      = "hold"       # critical: hard block, must confirm
    SKIP      = "skip"       # disabled or no meaningful action


@dataclass
class PlannedAction:
    """
    The output of the Action Planner for a single trigger fire.

    `intent`       — natural-language description of what to do
    `decision`     — how to handle authorization
    `trust_level`  — the evaluated trust level of the planned action
    `context`      — assembled context (memory excerpts, trigger metadata)
    `prompt`       — the prompt to send to the LLM when executing
    """
    trigger_name: str
    intent: str
    decision: ActionDecision
    trust_level: TrustLevel
    context: dict[str, Any] = field(default_factory=dict)
    prompt: str = ""

    @property
    def requires_confirmation(self) -> bool:
        return self.decision in (ActionDecision.NOTIFY, ActionDecision.HOLD)


class ActionPlanner:
    """
    Converts a fired trigger into a PlannedAction.

    Optionally receives memory/reflection references for context assembly;
    if absent, produces a minimal plan based on the trigger's own fields.
    """

    def __init__(
        self,
        reflection=None,       # ReflectionAPI | None
        semantic_memory=None,  # SemanticMemory | None
    ) -> None:
        self._reflection = reflection
        self._semantic = semantic_memory

    async def handle(
        self,
        trigger: TriggerDefinition,
        fire_context: dict[str, Any],
    ) -> PlannedAction:
        """
        Called by TriggerEvaluator when a trigger fires.
        Returns a PlannedAction ready for the runtime to act on.
        """
        # 1. Determine trust level
        trust_level = _parse_trust(trigger.trust_level)

        # 2. Assemble context
        context = dict(fire_context)
        context["trigger_name"] = trigger.name
        context["intent"] = trigger.intent
        context["notify_thread_id"] = getattr(trigger, "notify_thread_id", 0)

        if self._semantic is not None:
            try:
                recent_facts = await self._semantic.list_recent(limit=5)
                context["recent_facts"] = [
                    {"key": f.key, "value": f.value} for f in recent_facts
                ]
            except Exception:
                pass

        # 3. Map trust level → decision
        if not trigger.enabled:
            decision = ActionDecision.SKIP
        elif trust_level == TrustLevel.SAFE:
            decision = ActionDecision.EXECUTE
        elif trust_level == TrustLevel.GUARDED:
            decision = ActionDecision.NOTIFY if trigger.notify else ActionDecision.EXECUTE
        else:  # CRITICAL
            decision = ActionDecision.HOLD

        # 4. Build execution prompt
        prompt = _build_prompt(trigger, context)

        return PlannedAction(
            trigger_name=trigger.name,
            intent=trigger.intent,
            decision=decision,
            trust_level=trust_level,
            context=context,
            prompt=prompt,
        )


def _parse_trust(level: str) -> TrustLevel:
    mapping = {
        "safe":     TrustLevel.SAFE,
        "guarded":  TrustLevel.GUARDED,
        "critical": TrustLevel.CRITICAL,
    }
    return mapping.get(level.lower(), TrustLevel.GUARDED)


def _build_prompt(
    trigger: TriggerDefinition, context: dict[str, Any]
) -> str:
    facts_section = ""
    if context.get("recent_facts"):
        lines = "\n".join(
            f"  - {f['value']}" for f in context["recent_facts"]
        )
        facts_section = f"\nRecent memory context:\n{lines}\n"

    return (
        f"[Autonomous trigger: {trigger.name}]\n"
        f"Intent: {trigger.intent}\n"
        f"{facts_section}"
        f"Please carry out the above intent. "
        f"Think step-by-step and use tools as needed."
    )
