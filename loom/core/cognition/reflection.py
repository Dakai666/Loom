"""Read-only reflection over session_log and historical skill snapshots.

Legacy episodic is available only on explicit request when the log has no
known tool outcomes. Sources are selected as a whole, never merged by time.
"""
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from loom.core.memory.session_log import _redact_in_place, _redact_secrets
from loom.core.timezone import user_zone

if TYPE_CHECKING:
    from loom.core.memory.facade import MemoryFacade


class ReflectionAPI:
    """Read trace outcomes without turning missing observations into failures."""

    def __init__(self, memory: "MemoryFacade") -> None:
        self._memory = memory
        self._procedural = memory.procedural

    async def _trace(
        self, session_id: str, *, legacy_episodic: bool = False,
        n: int | None = None, tools_only: bool = False,
    ) -> tuple[str, list[dict]]:
        log = self._memory.session_log
        source = "session_log" if log is not None else "session_log unavailable"
        roles = ("tool",) if tools_only else ("user", "tool")
        # The normal recent-tools path is bounded in SQL. Explicit legacy
        # selection checks the whole session, so a recent unknown row cannot
        # hide an older known log outcome and trigger a mixed-source report.
        rows = await log.read_trace(
            session_id, roles=roles, limit=None if legacy_episodic else n,
            newest_first=tools_only and not legacy_episodic,
        ) if log is not None else []
        if legacy_episodic and not any(
            r["role"] == "tool" and type(r["metadata"].get("success")) is bool for r in rows
        ):
            episodes = await self._memory.episodic.read_session(session_id)
            if episodes:
                source = "legacy episodic fallback"
                rows = [{
                    "role": "tool" if e.event_type == "tool_result" else "user",
                    "content": _redact_secrets(e.content) or "",
                    "metadata": _redact_in_place(dict(e.metadata)),
                    "created_at": e.created_at.isoformat(),
                } for e in episodes if e.event_type in ("tool_result", "message")]
        for row in rows:
            meta = row["metadata"]
            if row["role"] == "tool" and type(meta.get("success")) is not bool:
                meta["success"] = None
        if tools_only and legacy_episodic:
            rows = [r for r in rows if r["role"] == "tool"][::-1]
            if n is not None:
                rows = rows[:n]
        return source, rows

    async def recent_tool_calls(
        self, session_id: str, n: int = 10, *, legacy_episodic: bool = False,
    ) -> list[dict]:
        """Last n tool rows, newest first; source and unknown outcomes explicit."""
        if n <= 0:
            return []
        source, rows = await self._trace(session_id, legacy_episodic=legacy_episodic, n=n, tools_only=True)
        return [{
            "content": r["content"], "metadata": r["metadata"], "source": source,
            "timestamp": (
                datetime.fromisoformat(r["created_at"]).replace(tzinfo=UTC)
                if datetime.fromisoformat(r["created_at"]).tzinfo is None
                else datetime.fromisoformat(r["created_at"])
            ).astimezone(user_zone()).isoformat(),
        } for r in rows]

    async def session_summary(self, session_id: str, *, legacy_episodic: bool = False) -> str:
        source, rows = await self._trace(session_id, legacy_episodic=legacy_episodic)
        if not rows:
            return f"No activity recorded in this source. Source: {source}. Legacy episodic requires explicit opt-in."
        tools = [r for r in rows if r["role"] == "tool"]
        ok = sum(r["metadata"].get("success") is True for r in tools)
        fail = sum(r["metadata"].get("success") is False for r in tools)
        users = sum(r["role"] == "user" for r in rows)
        names = list(dict.fromkeys(r["metadata"].get("tool_name") for r in tools if r["metadata"].get("tool_name")))
        text = (f"Session so far: {users} user message(s), {len(tools)} tool call(s) "
                f"({ok} ok, {fail} failed, {len(tools)-ok-fail} unknown). Source: {source}.")
        return text + (f" Tools used: {', '.join(names)}." if names else "")

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

    async def failure_summary(self, session_id: str, *, legacy_episodic: bool = False) -> dict:
        """Known failures only; report unknown observations separately."""
        source, rows = await self._trace(session_id, legacy_episodic=legacy_episodic)
        counts: dict[str, int] = {}
        unknown = 0
        for row in rows:
            if row["role"] != "tool":
                continue
            meta = row["metadata"]
            if meta.get("success") is None:
                unknown += 1
            elif meta["success"] is False:
                kind = meta.get("failure_type") or "unclassified"
                counts[kind] = counts.get(kind, 0) + 1
        return {"source": source, "counts": counts, "unknown_outcomes": unknown}

    async def tool_success_rate(self, session_id: str, *, legacy_episodic: bool = False) -> dict:
        """Rates use known outcomes only; an entirely unknown tool has rate None.

        The envelope labels source and the per-tool excluded unknown counts,
        so callers cannot present a partial denominator as a full success rate.
        """
        source, rows = await self._trace(session_id, legacy_episodic=legacy_episodic)
        counts: dict[str, list[int]] = {}
        for row in rows:
            if row["role"] != "tool":
                continue
            name = row["metadata"].get("tool_name") or "unknown tool"
            ok, known, unknown = counts.setdefault(name, [0, 0, 0])
            value = row["metadata"].get("success")
            counts[name] = [ok + int(value is True), known + int(value is not None), unknown + int(value is None)]
        return {
            "source": source,
            "rates": {name: ok / known if known else None for name, (ok, known, _) in counts.items()},
            "unknown_outcomes": {name: unknown for name, (_, _, unknown) in counts.items() if unknown},
        }
