"""
Offline Dreaming — background knowledge synthesis for Loom.

During idle / scheduled periods, `dream_cycle()` picks a random sample of
semantic facts, asks the LLM to find non-obvious connections, and writes the
resulting insights as Relational triples tagged ``source="dreaming"``.

Integration points
------------------
* `SemanticMemory.get_random()`   — random fact sampler
* `RelationalMemory.upsert()`     — stores the extracted triples
* Any async LLM callable          — invoked with a structured prompt

Design decisions
----------------
* **Pure cognition module** — no imports from platform, harness, or extensibility.
* **Async, cancellation-safe** — each sub-task can be awaited from the autonomy
  daemon without blocking the Discord event loop.
* **JSON parsing is defensive** — if the LLM returns malformed JSON the cycle
  logs a warning and stores whatever well-formed triples it could decode.
* **Age-aware prompts** — facts are presented with their creation timestamps and
  topic tags so the LLM can reason about cross-temporal patterns and cross-domain
  connections (e.g. a weeks-old infrastructure fact connecting with a recent skill
  insight enables "cross-version archaeology").

The ``ToolDefinition`` adapters (``dream_cycle`` / ``memory_prune``) live in
``loom.core.memory.maintenance`` and are registered by ``LoomSession.start()``
alongside the other memory tools (Issue #149).
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, UTC
from typing import Callable, Awaitable, TYPE_CHECKING

from loom.core.memory.ontology import (
    DOMAIN_KNOWLEDGE,
    DOMAIN_PROJECT,
    DOMAIN_SELF,
    DOMAIN_USER,
)

if TYPE_CHECKING:
    import aiosqlite

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Round-robin theme selector (Issue #281 P3-D)
# ---------------------------------------------------------------------------
#
# When themed sampling is requested without an explicit domain, the daemon
# cycles through the four ontology domains in order, persisting "what I
# dreamed last" in ``memory_meta`` so restarts don't reset the rotation.
#
# Design rationale (ontology §5): we explicitly chose round-robin over
# auto-heuristics ("most-written domain", "least-recently-dreamed", etc.)
# — simpler, no thinking required, no metric drift, and predictable enough
# for the agent to develop intuition over time. Re-evaluate after two weeks.

_THEME_ROTATION = (DOMAIN_SELF, DOMAIN_USER, DOMAIN_PROJECT, DOMAIN_KNOWLEDGE)
_META_KEY_LAST_THEME = "dream.last_theme"


async def next_themed_domain(db: "aiosqlite.Connection") -> str:
    """Return the next domain in the round-robin and persist it.

    Idempotent within a single call: reads the previous theme from
    ``memory_meta``, advances by one step in ``_THEME_ROTATION``, writes
    the new theme back, and returns it. First call ever returns the first
    domain in the cycle.
    """
    cursor = await db.execute(
        "SELECT value FROM memory_meta WHERE key = ?",
        (_META_KEY_LAST_THEME,),
    )
    row = await cursor.fetchone()
    last = row[0] if row else None

    if last in _THEME_ROTATION:
        idx = (_THEME_ROTATION.index(last) + 1) % len(_THEME_ROTATION)
    else:
        idx = 0  # first call, or stored value drifted out of the enum
    next_theme = _THEME_ROTATION[idx]

    now = datetime.now(UTC).isoformat()
    await db.execute(
        "INSERT INTO memory_meta(key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
        "updated_at = excluded.updated_at",
        (_META_KEY_LAST_THEME, next_theme, now),
    )
    # Same boundary effect note as pulse._mark_notified (#307 B1):
    # this commits whatever else is pending on the connection. Caller
    # (dream_cycle) holds no other un-committed writes at this point —
    # do not introduce any before next_themed_domain() returns.
    await db.commit()
    return next_theme


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _extract_topic(key: str) -> str:
    """
    Extract the topic tag from a semantic memory key.

    Keys follow the pattern ``topic:subtopic:...`` (e.g. ``skill:github_cli:eval``).
    Returns the first segment, or ``"general"`` if no colon is present.
    """
    return key.split(":")[0] if ":" in key else "general"


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

# Template for the inner prompt fed to the LLM
_DREAM_SYSTEM = """\
You are Loom's reflective cognition layer, running an offline dreaming cycle.

You will receive a collection of isolated facts from long-term semantic memory.
Each fact includes:
  - ``key``:     its memory key (reveals the topic/subject area)
  - ``topic``:   the high-level topic category
  - ``fact_time``: when the fact was first stored (YYYY-MM-DD)
  - ``value``:   the actual fact content

Your task is to identify NON-OBVIOUS connections, especially:
  - CROSS-DOMAIN: connecting facts from different topic areas
  - CROSS-TEMPORAL: connecting old facts (weeks ago) with recent ones
  - EMERGENT PATTERNS: connections only visible when many isolated facts align

When you notice facts spanning very different time periods (e.g. a fact from three
weeks ago connecting with a recent one), note that this long-range mixing is valuable —
it enables "cross-version archaeology."

Return ONLY a JSON array of relationship triples, each with exactly these keys:
  "subject"   — the first concept (string)
  "predicate" — the relationship verb (string, e.g. "implies", "contrasts_with",
                "enables", "is_prerequisite_of", "co-occurs_with")
  "object"    — the second concept or finding (string)
  "insight"   — one sentence explaining why this connection matters (string)

Rules:
  - Return 3–8 triples maximum.
  - Focus on CROSS-DOMAIN and CROSS-TEMPORAL insights rather than paraphrasing.
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
    domain: str | None = None,
    themed: bool = False,
    db: "aiosqlite.Connection | None" = None,
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
    domain:       Constrain sampling to a single ontology domain
                  (self/user/project/knowledge). ``None`` keeps the original
                  cross-domain free-dream behaviour.
    themed:       When True and ``domain`` is None, round-robin the next
                  domain via ``next_themed_domain(db)`` (requires ``db``).
                  Issue #281 P3-D — themed dreams stay focused, free
                  dreams find cross-domain bridges; both modes coexist.
    db:           Required only when ``themed=True``; ignored otherwise.

    Returns
    -------
    dict with keys:
      - ``facts_sampled``   int
      - ``triples_found``   int
      - ``triples_written`` int
      - ``domain``          str | None  (resolved domain, or None for free)
      - ``errors``          list[str]  (non-fatal parse issues)
    """
    errors: list[str] = []

    # ── 0. Resolve domain (themed → round-robin) ───────────────────────
    if themed and domain is None:
        if db is None:
            errors.append("themed=True requires db connection — falling back to free dream")
        else:
            domain = await next_themed_domain(db)

    # ── 1. Sample random facts ──────────────────────────────────────────
    facts = await semantic.get_random(limit=sample_size, domain=domain)
    if not facts:
        logger.info(
            "[dreaming] No semantic facts found (domain=%s) — skipping cycle.",
            domain or "any",
        )
        return {
            "facts_sampled": 0, "triples_found": 0, "triples_written": 0,
            "domain": domain, "errors": errors,
        }

    # ── 1b. Enrich facts with topic + timestamp ────────────────────────
    fact_lines: list[str] = []
    sampled_facts_metadata: list[dict] = []
    for f in facts:
        topic = _extract_topic(f.key)
        fact_time = f.created_at.strftime("%Y-%m-%d") if f.created_at else "unknown"
        fact_lines.append(
            f'- [{len(fact_lines)+1}] key="{f.key}"  topic="{topic}"'
            f'  fact_time="{fact_time}"  value="{f.value}"'
        )
        sampled_facts_metadata.append({
            "fact_time": fact_time,
            "key": f.key,
            "topic": topic,
        })

    facts_block = "\n".join(fact_lines)

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
            "domain": domain,
            "errors": [*errors, f"LLM error: {exc}"],
        }

    # ── 3. Parse JSON triples ──────────────────────────────────────────
    triples = _parse_triples(raw, errors)

    # ── 4. Write to RelationalMemory ──────────────────────────────────
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
                        "sampled_facts": sampled_facts_metadata,
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
        "domain": domain,
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
