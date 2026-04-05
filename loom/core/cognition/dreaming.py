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

The LoomPlugin wrapper (``DreamingPlugin``) lives in
``loom.extensibility.dreaming_plugin`` to keep the layer dependency clean.
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
