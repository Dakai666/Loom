"""
Reflection API — lets the agent observe its own execution trace.

This is the "self-awareness" layer: the agent can query what tools it
called, how often they succeeded, and get a plain-language summary of
the current session without reading raw DB rows.

Phase 2: query interface over episodic + procedural memory.
Phase 3: the Autonomy Engine will call this to decide what to do next.
"""

from loom.core.memory.episodic import EpisodicMemory
from loom.core.memory.procedural import ProceduralMemory


class ReflectionAPI:
    """
    Read-only window into the agent's own history and skill health.

    Injected with memory instances at session start.
    """

    def __init__(
        self,
        episodic: EpisodicMemory,
        procedural: ProceduralMemory,
    ) -> None:
        self._episodic = episodic
        self._procedural = procedural

    # ------------------------------------------------------------------
    # Session trace queries
    # ------------------------------------------------------------------

    async def recent_tool_calls(
        self, session_id: str, n: int = 10
    ) -> list[dict]:
        """
        Return the last `n` tool_result entries for a session,
        ordered most-recent first.
        """
        entries = await self._episodic.read_session(session_id)
        tool_entries = [
            {
                "content": e.content,
                "metadata": e.metadata,
                "timestamp": e.created_at.isoformat(),
            }
            for e in entries
            if e.event_type == "tool_result"
        ]
        return tool_entries[-n:][::-1]

    async def session_summary(self, session_id: str) -> str:
        """
        One-paragraph plain-text summary of what happened in the session.
        Suitable for inserting into the LLM context as a system message.
        """
        entries = await self._episodic.read_session(session_id)
        if not entries:
            return "No activity recorded yet in this session."

        tool_results = [e for e in entries if e.event_type == "tool_result"]
        messages = [e for e in entries if e.event_type == "message"]
        ok = sum(1 for e in tool_results if e.metadata.get("success"))
        fail = len(tool_results) - ok

        tool_names: list[str] = []
        for e in tool_results:
            name = e.metadata.get("tool_name")
            if name and name not in tool_names:
                tool_names.append(name)

        parts = [
            f"Session so far: {len(messages)} user message(s), "
            f"{len(tool_results)} tool call(s) ({ok} ok, {fail} failed).",
        ]
        if tool_names:
            parts.append(f"Tools used: {', '.join(tool_names)}.")

        return " ".join(parts)

    # ------------------------------------------------------------------
    # Skill health queries
    # ------------------------------------------------------------------

    async def skill_health_report(self) -> list[dict]:
        """
        Return confidence and usage stats for all active skills,
        sorted by confidence descending.
        """
        skills = await self._procedural.list_active()
        return [
            {
                "name": s.name,
                "confidence": round(s.confidence, 3),
                "usage_count": s.usage_count,
                "success_rate": round(s.success_rate, 3),
                "tags": s.tags,
            }
            for s in skills
        ]

    async def failure_summary(self, session_id: str) -> dict[str, int]:
        """
        Return failure counts grouped by failure_type for the session.
        Example: {"permission_denied": 2, "timeout": 1, "execution_error": 3}
        Entries without a failure_type are grouped under "unclassified".
        """
        entries = await self._episodic.read_session(session_id)
        counts: dict[str, int] = {}
        for e in entries:
            if e.event_type != "tool_result":
                continue
            if e.metadata.get("success"):
                continue
            ftype = e.metadata.get("failure_type") or "unclassified"
            counts[ftype] = counts.get(ftype, 0) + 1
        return counts

    async def tool_success_rate(self, session_id: str) -> dict[str, float]:
        """
        Per-tool success rates for the current session.
        Returns {tool_name: success_rate_0_to_1}.
        """
        entries = await self._episodic.read_session(session_id)
        counts: dict[str, list[int]] = {}   # {name: [ok, total]}
        for e in entries:
            if e.event_type != "tool_result":
                continue
            name = e.metadata.get("tool_name")
            if not name:
                continue
            if name not in counts:
                counts[name] = [0, 0]
            counts[name][1] += 1
            if e.metadata.get("success"):
                counts[name][0] += 1
        return {
            name: v[0] / v[1] if v[1] else 0.0
            for name, v in counts.items()
        }
