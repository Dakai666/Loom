"""Turn-local skill activation inputs for model-tier routing (#675).

The historical class name remains, but this tracker evaluates no outcomes and
owns no memory connection. Skill metrics are historical snapshots.
"""

from __future__ import annotations


class SkillOutcomeTracker:
    """Track successful skill activations until the turn boundary."""

    def __init__(self) -> None:
        self._activated: dict[str, int] = {}

    def record_activation(self, skill_name: str, turn_index: int) -> None:
        """Make a loaded skill available to the next model-call tier decision."""
        self._activated[skill_name] = turn_index

    @property
    def activated_skills(self) -> list[str]:
        """Names activated during the current turn."""
        return list(self._activated)

    def clear_through(self, turn_index: int) -> None:
        """Clear completed-turn inputs without changing the sticky model tier."""
        for name, activated_at in list(self._activated.items()):
            if activated_at <= turn_index:
                del self._activated[name]
