"""
Offline Dreaming — background knowledge synthesis for Loom.

During idle / scheduled periods, `dream_cycle()` picks a random sample of
semantic facts, asks the LLM to find non-obvious connections, and writes the
resulting insights as Relational triples tagged ``source="dreaming"``.

Integration points
------------------
* `SemanticMemory.get_random()`   — random fact sampler (added in this PR)
* `RelationalMemory.upsert()`     — stores the extracted triples
* Any async LLM callable          — invoked with a structured prompt

Design decisions
----------------
* **No imports from platform or harness layers** — dreaming is a pure cognition
  module; it does not depend on session, bot, or tool registry.
* **Async, cancellation-safe** — each sub-task can be awaited from the autonomy
  daemon without blocking the Discord event loop.
* **JSON parsing is defensive** — if the LLM returns malformed JSON the cycle
  logs a warning and stores whatever well-formed triples it could decode.
* **Plugin wrapper** (`DreamingPlugin`) registers `dream_cycle` as a SAFE tool
  so the Autonomy Agent can call it by name from its natural-language intent.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, UTC
from typing import Callable, Awaitable

logger = logging.getLogger(__name__)

# Template for the inner prompt fed to the LLM
_DREAM_SYSTEM = """\
You are Loom's reflective cognition layer, running an offline dreaming cycle.

You will receive a collection of isolated facts from long-term semantic memory.
Your task is to identify non-obvious connections, abstract patterns, or emergent
insights that no single fact surfaces on its own.

Return ONLY a JSON array of relationship triples, each with exactly these keys:
  "subject"   — the first concept (string)
  "predicate" — the relationship verb (string, e.g. "implies", "contrasts_with",
                "enables", "is_prerequisite_of", "co-occurs_with")
  "object"    — the second concept or finding (string)
  "insight"   — one sentence explaining why this connection matters (string)

Rules:
  - Return 3–8 triples maximum.
  - Focus on CROSS-DOMAIN insights rather than paraphrasing existing facts.
  - Do not hallucinate facts not present in the input.
  - If there are fewer than 3 meaningful connections, return fewer triples.
  - Return ONLY the JSON array — no preamble, no explanation, no markdown fences.
"""

_DREAM_USER_TEMPLATE = """\
Semantic facts to analyse:
{facts_block}

Find hidden connections and return the JSON triple array now.
"""


# ---------------------------------------------------------------------------
# Core dream_cycle coroutine
# ---------------------------------------------------------------------------

async def dream_cycle(
    *,
    semantic,                                      # SemanticMemory
    relational,                                    # RelationalMemory
    llm_fn: Callable[..., Awaitable[str]],         # async fn(messages) -> str
    sample_size: int = 15,
    dry_run: bool = False,
) -> dict:
    """
    Execute one dreaming cycle.

    Parameters
    ----------
    semantic:     SemanticMemory instance (must expose ``get_random``).
    relational:   RelationalMemory instance (must expose ``upsert``).
    llm_fn:       Async callable receiving OpenAI-style messages list and
                  returning the assistant reply string.  Typically wraps
                  ``LoomSession._router.route()`` or a provider ``complete()``.
    sample_size:  Number of random semantic facts to sample (default 15).
    dry_run:      When True, run the prompt and parse but skip writing to DB.

    Returns
    -------
    dict with keys:
      - ``facts_sampled``   int
      - ``triples_found``   int
      - ``triples_written`` int
      - ``errors``          list[str]  (non-fatal parse issues)
    """
    errors: list[str] = []

    # ── 1. Sample random facts ──────────────────────────────────────────
    facts = await semantic.get_random(limit=sample_size)
    if not facts:
        logger.info("[dreaming] No semantic facts found — skipping cycle.")
        return {"facts_sampled": 0, "triples_found": 0, "triples_written": 0, "errors": []}

    facts_block = "\n".join(
        f'- [{i+1}] key="{f.key}"  value="{f.value}"'
        for i, f in enumerate(facts)
    )

    # ── 2. Call LLM ────────────────────────────────────────────────────
    messages = [
        {"role": "system", "content": _DREAM_SYSTEM},
        {"role": "user", "content": _DREAM_USER_TEMPLATE.format(facts_block=facts_block)},
    ]

    try:
        raw = await llm_fn(messages)
    except Exception as exc:
        logger.warning("[dreaming] LLM call failed: %s", exc)
        return {
            "facts_sampled": len(facts),
            "triples_found": 0,
            "triples_written": 0,
            "errors": [f"LLM error: {exc}"],
        }

    # ── 3. Parse JSON triples ──────────────────────────────────────────
    triples = _parse_triples(raw, errors)

    # ── 4. Write to RelationalMemory ───────────────────────────────────
    written = 0
    if not dry_run:
        from loom.core.memory.relational import RelationalEntry
        for triple in triples:
            try:
                entry = RelationalEntry(
                    subject=triple["subject"],
                    predicate=triple["predicate"],
                    object=triple["object"],
                    confidence=0.7,          # dreaming-sourced insights start lower
                    source="dreaming",
                    metadata={
                        "insight": triple.get("insight", ""),
                        "dream_ts": datetime.now(UTC).isoformat(),
                    },
                )
                await relational.upsert(entry)
                written += 1
            except Exception as exc:
                errors.append(f"upsert error: {exc}")
                logger.warning("[dreaming] Failed to write triple %s: %s", triple, exc)

    result = {
        "facts_sampled": len(facts),
        "triples_found": len(triples),
        "triples_written": written,
        "errors": errors,
    }
    logger.info("[dreaming] Cycle complete: %s", result)
    return result


# ---------------------------------------------------------------------------
# JSON parsing helper
# ---------------------------------------------------------------------------

def _parse_triples(raw: str, errors: list[str]) -> list[dict]:
    """Extract a list of triple dicts from raw LLM output.

    Handles:
    - Clean JSON array
    - JSON wrapped in markdown fences (```json ... ```)
    - Partial arrays with some invalid entries
    """
    # Strip markdown fences if present
    cleaned = re.sub(r"```(?:json)?\s*", "", raw.strip()).strip().rstrip("```").strip()

    # Attempt whole-array parse first
    try:
        data = json.loads(cleaned)
        if isinstance(data, list):
            valid = [t for t in data if _is_valid_triple(t)]
            if len(valid) < len(data):
                errors.append(f"Skipped {len(data) - len(valid)} malformed triple(s)")
            return valid
    except json.JSONDecodeError:
        pass

    # Fallback: pick out individual {...} objects
    partial: list[dict] = []
    for m in re.finditer(r"\{[^{}]+\}", cleaned, re.DOTALL):
        try:
            obj = json.loads(m.group())
            if _is_valid_triple(obj):
                partial.append(obj)
        except json.JSONDecodeError:
            pass

    if not partial:
        errors.append(f"Could not parse any triples from LLM output: {raw[:200]}")
    else:
        errors.append(f"Used fallback triple extraction ({len(partial)} found)")

    return partial


def _is_valid_triple(obj: object) -> bool:
    return (
        isinstance(obj, dict)
        and isinstance(obj.get("subject"), str)
        and isinstance(obj.get("predicate"), str)
        and isinstance(obj.get("object"), str)
    )


# ---------------------------------------------------------------------------
# DreamingPlugin — LoomPlugin wrapper
# ---------------------------------------------------------------------------

def _make_dreaming_plugin(session):
    """
    Build and return a DreamingPlugin instance wired to *session*.

    Called by ``DreamingPlugin.on_session_start(session)`` so the tool
    always closes over the live session's memory references.
    """
    from loom.core.harness.registry import ToolDefinition
    from loom.core.harness.middleware import ToolResult
    from loom.core.harness.permissions import TrustLevel

    async def _dream_cycle_executor(call) -> ToolResult:
        """
        Execute one offline dreaming cycle.

        Samples random semantic facts, asks the LLM to discover non-obvious
        connections, and stores the resulting insights as Relational triples
        tagged source="dreaming". Returns a brief summary.
        """
        sample = int(call.args.get("sample_size", 15))
        dry_run = bool(call.args.get("dry_run", False))

        # Build an llm_fn from the session's router.
        # We call router.chat(model, messages) directly — bypassing the full
        # turn pipeline — so dreaming never writes episodic events or triggers
        # harness middleware.
        async def llm_fn(messages):
            response = await session.router.chat(
                model=session.model,
                messages=messages,
                max_tokens=2048,
            )
            return response.text or ""

        sem = getattr(session, "_semantic", None)
        rel = getattr(session, "_relational", None)
        if sem is None or rel is None:
            return ToolResult(
                call_id=call.id, tool_name=call.tool_name, success=False,
                error="Dreaming skipped — memory subsystems not available.",
            )

        result = await dream_cycle(
            semantic=sem,
            relational=rel,
            llm_fn=llm_fn,
            sample_size=sample,
            dry_run=dry_run,
        )

        lines = [
            "Dream cycle complete",
            f"  Facts sampled: {result['facts_sampled']}",
            f"  Triples found: {result['triples_found']}",
            f"  Triples written: {result['triples_written']}",
        ]
        if result["errors"]:
            lines.append(f"  Warnings: {'; '.join(result['errors'])}")
        if dry_run:
            lines.append("  (dry-run — nothing was written)")
        return ToolResult(
            call_id=call.id, tool_name=call.tool_name, success=True,
            output="\n".join(lines),
        )

    tool_def = ToolDefinition(
        name="dream_cycle",
        description=(
            "Run an offline dreaming cycle: sample random semantic facts, "
            "discover non-obvious connections via the LLM, and store the resulting "
            "insights as Relational triples (source='dreaming'). "
            "Use this when the autonomy scheduler triggers a background synthesis task."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "sample_size": {
                    "type": "integer",
                    "description": "Number of random facts to sample (default 15, max 30).",
                    "default": 15,
                },
                "dry_run": {
                    "type": "boolean",
                    "description": "If true, run the full cycle but skip writing to the DB.",
                    "default": False,
                },
            },
        },
        executor=_dream_cycle_executor,
        trust_level=TrustLevel.SAFE,
    )
    return tool_def


class DreamingPlugin:
    """
    LoomPlugin that registers the ``dream_cycle`` tool.

    Drop this file in ``~/.loom/plugins/dreaming.py`` and add the autonomy
    schedule (see loom.toml.example).  The plugin wires itself to the session's
    memory and router on ``on_session_start``.

    Alternatively, install it programmatically in a session bootstrap script:

        from loom.core.cognition.dreaming import DreamingPlugin
        import loom
        loom.register_plugin(DreamingPlugin())
    """

    name = "dreaming"
    version = "1.0"

    def __init__(self) -> None:
        self._tool_def = None

    # -- LoomPlugin required interface --

    def tools(self) -> list:
        # Dynamic tool is wired in on_session_start; return empty here.
        return []

    def middleware(self) -> list:
        return []

    def lenses(self) -> list:
        return []

    def notifiers(self) -> list:
        return []

    def on_session_start(self, session) -> None:
        """Wire dream_cycle tool into the session's tool registry."""
        self._tool_def = _make_dreaming_plugin(session)
        session.registry.register(self._tool_def)

    def on_session_stop(self, session) -> None:
        pass
