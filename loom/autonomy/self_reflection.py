"""
SelfReflectionPlugin — Relational Memory as Mirror (Issue #26).

A ``LoomPlugin`` that observes the agent's own behaviour across sessions and
writes self-observations as RelationalMemory triples with ``subject="loom-self"``.

Behaviour
---------
*  ``on_session_stop``: queues a background reflection after each session ends.
   Queries recent episodic entries → LLM identifies behavioural patterns
   → writes ``(loom-self, <predicate>, <observation>)`` triples.
*  ``tools()``: exposes a ``reflect_self`` tool so the agent can trigger
   self-reflection on demand (also available via the autonomy daemon).

Triple predicates
-----------------
``tends_to``       — recurring behavioural tendency (positive or neutral)
``should_avoid``   — identified anti-pattern or failure mode
``discovered``     — open-ended self-observation

The agent loads ``loom-self`` triples at session start via ``MemoryIndex``
(``index.py`` already surfaces ``anti_pattern_count``).
``MemoryIndex.render()`` is extended here to show the full self-portrait
in a dedicated section when ``loom-self`` triples are present.

Usage
-----
Drop in ``~/.loom/plugins/self_reflection.py``::

    from loom.autonomy.self_reflection import SelfReflectionPlugin
    import loom
    loom.register_plugin(SelfReflectionPlugin())

Or call directly from the autonomy daemon / tests::

    from loom.autonomy.self_reflection import run_self_reflection
    await run_self_reflection(episodic=..., relational=..., llm_fn=...)
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, UTC
from typing import Any, Awaitable, Callable

from loom.core.memory.episodic import EpisodicMemory
from loom.core.memory.relational import RelationalEntry, RelationalMemory
from loom.extensibility.plugin import LoomPlugin

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

_REFLECT_PROMPT = """\
You are analysing an AI agent's own recent behaviour from its episodic memory.
Your goal is to identify **recurring patterns** — both effective habits and
problematic tendencies — so the agent can carry this self-knowledge forward.

Recent episodic entries (newest first):
{entries}

Output a JSON array of self-observation objects.  Each object must have:
  "predicate": one of "tends_to" | "should_avoid" | "discovered"
  "object":    a concise, specific description (max 120 chars)
  "confidence": float 0.0–1.0

Rules:
- Only emit patterns that appear in at least 2 entries, or that are
  clearly significant (e.g. an error that repeated 3 times in one session).
- Keep "object" values specific and actionable, not vague.
- Minimum 1 observation, maximum 6.
- Return ONLY the JSON array — no prose.

Example:
[
  {{"predicate": "tends_to", "object": "over-explain before acting on simple tasks", "confidence": 0.7}},
  {{"predicate": "should_avoid", "object": "recursive bash retries without an exit condition", "confidence": 0.9}}
]
"""

# ---------------------------------------------------------------------------
# Core coroutine (used by plugin & autonomy daemon)
# ---------------------------------------------------------------------------

LLMFn = Callable[[str], Awaitable[str]]


async def run_self_reflection(
    episodic: EpisodicMemory,
    relational: RelationalMemory,
    llm_fn: LLMFn,
    session_id: str | None = None,
    max_entries: int = 40,
) -> list[RelationalEntry]:
    """
    Analyse recent episodic entries and persist behavioural triples.

    Parameters
    ----------
    episodic:   EpisodicMemory to query.
    relational: RelationalMemory to write triples into.
    llm_fn:     Async callable that takes a prompt string and returns the
                LLM response text.  Inject ``router.chat()`` here.
    session_id: When given, only considers entries from this session.
                When None, considers all recent entries.
    max_entries: Cap on episodic entries sent to the LLM.

    Returns
    -------
    List of ``RelationalEntry`` objects actually written.
    """
    # 1. Collect recent episodic entries
    if session_id:
        entries = await episodic.read_session(session_id)
    else:
        # EpisodicMemory has no read_recent(); query directly for cross-session mode.
        cursor = await episodic._db.execute(
            "SELECT id, session_id, event_type, content, metadata, created_at "
            "FROM episodic_entries ORDER BY created_at DESC LIMIT ?",
            (max_entries,),
        )
        from loom.core.memory.episodic import EpisodicEntry
        from datetime import datetime
        rows = await cursor.fetchall()
        entries = [
            EpisodicEntry(
                id=r[0], session_id=r[1], event_type=r[2],
                content=r[3],
                metadata=__import__("json").loads(r[4]),
                created_at=datetime.fromisoformat(r[5]),
            )
            for r in rows
        ]

    if not entries:
        logger.debug("self_reflection: no episodic entries found, skipping")
        return []

    # Build a compact text representation (newest first, capped)
    recent = entries[-max_entries:][::-1]
    entry_text = "\n".join(
        f"[{e.event_type}] {e.content[:200]}" for e in recent
    )

    # 2. Ask the LLM
    prompt = _REFLECT_PROMPT.format(entries=entry_text)
    try:
        raw = await llm_fn(prompt)
    except Exception as exc:
        logger.warning("self_reflection: LLM call failed: %s", exc)
        return []

    # 3. Parse JSON — defensive (same pattern as dreaming.py)
    observations: list[dict[str, Any]] = _parse_observations(raw)
    if not observations:
        logger.debug("self_reflection: LLM returned no parseable observations")
        return []

    # 4. Write to RelationalMemory
    written: list[RelationalEntry] = []
    now = datetime.now(UTC)
    for obs in observations:
        predicate = obs.get("predicate", "discovered")
        obj = obs.get("object", "").strip()
        confidence = float(obs.get("confidence", 0.7))
        if not obj:
            continue
        # Sanitise predicate to allow only the three canonical values
        if predicate not in ("tends_to", "should_avoid", "discovered"):
            predicate = "discovered"

        entry = RelationalEntry(
            subject="loom-self",
            predicate=predicate,
            object=obj,
            confidence=min(1.0, max(0.0, confidence)),
            source="self_reflection",
            metadata={"reflected_at": now.isoformat(), "session_id": session_id or ""},
        )
        try:
            await relational.upsert(entry)
            written.append(entry)
        except Exception as exc:
            logger.warning("self_reflection: failed to write triple: %s", exc)

    logger.info(
        "self_reflection: wrote %d loom-self triple(s): %s",
        len(written),
        [f"{e.predicate}={e.object[:40]}" for e in written],
    )
    return written


def _parse_observations(raw: str) -> list[dict[str, Any]]:
    """
    Robustly extract a JSON array of observations from the LLM response.

    Handles:
    - Pure JSON array
    - Array wrapped in markdown fences (```json … ```)
    - Single object instead of array
    """
    text = raw.strip()

    # Strip markdown fences
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(
            l for l in lines if not l.strip().startswith("```")
        ).strip()

    # Try to find the outermost JSON array
    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end != -1 and end > start:
        try:
            result = json.loads(text[start : end + 1])
            if isinstance(result, list):
                return [r for r in result if isinstance(r, dict)]
        except json.JSONDecodeError:
            pass

    # Single object fallback
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1:
        try:
            obj = json.loads(text[start : end + 1])
            if isinstance(obj, dict):
                return [obj]
        except json.JSONDecodeError:
            pass

    return []


# ---------------------------------------------------------------------------
# LoomPlugin implementation
# ---------------------------------------------------------------------------


class SelfReflectionPlugin(LoomPlugin):
    """
    Plugin that hooks into session stop to reflect on behavioural patterns.

    Installs a ``reflect_self`` tool (SAFE trust) so the agent can also
    trigger reflection on demand via a slash command or autonomy trigger.

    Example drop-in (``~/.loom/plugins/self_reflection.py``)::

        from loom.autonomy.self_reflection import SelfReflectionPlugin
        import loom
        loom.register_plugin(SelfReflectionPlugin())
    """

    name = "self_reflection"
    version = "1.0"

    # ------------------------------------------------------------------
    # LoomPlugin.tools()
    # ------------------------------------------------------------------

    def tools(self):
        """Register the ``reflect_self`` tool into the session registry."""
        from loom.core.harness.registry import ToolDefinition
        from loom.core.harness.middleware import ToolResult
        from loom.core.harness.permissions import TrustLevel

        async def _reflect_self_executor(call) -> ToolResult:
            """Run a self-reflection cycle and return a summary."""
            session = call.args.get("_session")  # injected by on_session_start
            if session is None:
                return ToolResult(
                    call_id=call.id, tool_name=call.tool_name, success=False,
                    error="reflect_self: session context not available.",
                )

            episodic = getattr(session, "_episodic", None)
            relational = getattr(session, "_relational", None)
            if episodic is None or relational is None:
                return ToolResult(
                    call_id=call.id, tool_name=call.tool_name, success=False,
                    error="reflect_self: memory stores not initialised.",
                )

            async def _llm(prompt: str) -> str:
                resp = await session.router.chat(
                    model=session.model,
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=512,
                )
                return resp.text or ""

            written = await run_self_reflection(
                episodic=episodic,
                relational=relational,
                llm_fn=_llm,
                session_id=session.session_id,
            )
            if not written:
                return ToolResult(
                    call_id=call.id, tool_name=call.tool_name, success=True,
                    output="Self-reflection complete — no new patterns identified.",
                )
            lines = [f"Self-reflection: {len(written)} pattern(s) recorded:"]
            for e in written:
                lines.append(f"  [{e.predicate}] {e.object}")
            return ToolResult(
                call_id=call.id, tool_name=call.tool_name, success=True,
                output="\n".join(lines),
            )

        tool = ToolDefinition(
            name="reflect_self",
            description=(
                "Trigger a self-reflection cycle: analyse recent episodic memory and "
                "write behavioural observations (tends_to / should_avoid / discovered) "
                "as loom-self relational triples. No arguments required."
            ),
            input_schema={
                "type": "object",
                "properties": {},
                "required": [],
            },
            executor=_reflect_self_executor,
            trust_level=TrustLevel.SAFE,
        )
        return [tool]

    # ------------------------------------------------------------------
    # LoomPlugin.on_session_start()
    # ------------------------------------------------------------------

    def on_session_start(self, session: object) -> None:
        """
        Inject the session reference into tool args so the executor can
        access memory stores without a global.  We store the session on
        ``self`` — the plugin instance is unique per session.
        """
        self._session = session

        # Patch the reflect_self executor's closure to include the session.
        # We do this by monkey-patching the tool's executor via a wrapper.
        tool_def = session.registry.get("reflect_self")  # type: ignore[attr-defined]
        if tool_def is None:
            return

        _original_executor = tool_def.executor

        async def _bound_executor(call):
            # Inject session reference into the args dict (ephemeral, not sent to LLM)
            call.args["_session"] = session
            return await _original_executor(call)

        tool_def.executor = _bound_executor  # type: ignore[attr-defined]

    # ------------------------------------------------------------------
    # LoomPlugin.on_session_stop()
    # ------------------------------------------------------------------

    def on_session_stop(self, session: object) -> None:
        """
        Schedule a post-session reflection.

        The reflection is fire-and-forget: failures are logged but never
        block session teardown.  We use ``asyncio.ensure_future`` so this
        is non-blocking.
        """
        import asyncio

        episodic = getattr(session, "_episodic", None)
        relational = getattr(session, "_relational", None)
        if episodic is None or relational is None:
            return

        async def _llm(prompt: str) -> str:
            try:
                resp = await session.router.chat(  # type: ignore[attr-defined]
                    model=session.model,  # type: ignore[attr-defined]
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=512,
                )
                return resp.text or ""
            except Exception:
                return ""

        async def _run():
            try:
                await run_self_reflection(
                    episodic=episodic,
                    relational=relational,
                    llm_fn=_llm,
                    session_id=getattr(session, "session_id", None),
                )
            except Exception as exc:
                logger.warning("SelfReflectionPlugin.on_session_stop failed: %s", exc)

        try:
            asyncio.ensure_future(_run())
        except RuntimeError:
            pass  # No running event loop — skip silently
