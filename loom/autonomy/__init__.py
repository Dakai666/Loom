from .triggers import TriggerKind, TriggerDefinition, CronTrigger, EventTrigger, ConditionTrigger
from .evaluator import TriggerEvaluator
from .planner import ActionPlanner, PlannedAction, ActionDecision

# Backward-compat re-export: ``run_self_reflection`` lived under
# ``loom.autonomy`` until audit-B / #399 moved it to its proper home in
# ``loom.core.cognition.self_reflection`` (it's a TaskReflector helper,
# not autonomy daemon state). The shim keeps any external
# ``from loom.autonomy import run_self_reflection`` site working —
# direction is autonomy → cognition, the *allowed* direction.
from loom.core.cognition.self_reflection import run_self_reflection

__all__ = [
    "TriggerKind", "TriggerDefinition", "CronTrigger", "EventTrigger", "ConditionTrigger",
    "TriggerEvaluator",
    "ActionPlanner", "PlannedAction", "ActionDecision",
    "run_self_reflection",
]
