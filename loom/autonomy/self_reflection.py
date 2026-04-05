"""
Self-reflection core logic — Relational Memory as Mirror (Issue #26).

``run_self_reflection()`` analyses recent episodic entries and writes
behavioural observations as RelationalMemory triples (subject="loom-self").

The LoomPlugin wrapper (``SelfReflectionPlugin``) lives in
``loom.extensibility.self_reflection_plugin`` to keep the layer dependency clean.

Usage::

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

