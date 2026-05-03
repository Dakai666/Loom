"""
Domain classifier — heuristic prefix-based domain inference for memory facts.

When a caller (memorize tool, session compressor, dreaming) doesn't know
the right domain, the governor calls :func:`infer_domain` to upgrade the
default ``knowledge`` to a more specific axis based on the entry's key /
value shape.

The heuristic looks at:

  1. Key namespace prefixes (``user:`` / ``project:`` / ``loom:identity:`` etc.)
  2. Subject prefixes for relational triples
  3. Source tier (``user_explicit`` → user-aimed unless key says otherwise)

This is intentionally **rule-based, not LLM-based**. P1-C will add an
LLM-assisted batch classifier for legacy data migration — that classifier
shares the canonical DOMAINS enum with this module so heuristic-vs-LLM
disagreement is a strict comparison.
"""

from __future__ import annotations

import json
import logging
import time

from loom.core.memory.ontology import (
    DOMAIN_KNOWLEDGE,
    DOMAIN_PROJECT,
    DOMAIN_SELF,
    DOMAIN_USER,
    DOMAINS,
)

logger = logging.getLogger(__name__)


# Lower-cased key prefix → domain.  Order matters only for documentation;
# matching is exact-prefix, so longer namespaces are checked first below.
_KEY_PREFIX_RULES: tuple[tuple[str, str], ...] = (
    # Self — agent identity / principles / self-awareness
    ("loom:identity", DOMAIN_SELF),
    ("loom:self", DOMAIN_SELF),
    ("agent:identity", DOMAIN_SELF),
    ("self:", DOMAIN_SELF),
    ("identity:", DOMAIN_SELF),
    # User
    ("user:", DOMAIN_USER),
    ("u:", DOMAIN_USER),
    # Project — code architecture, config, workflow
    ("project:", DOMAIN_PROJECT),
    ("loom:config", DOMAIN_PROJECT),
    ("loom:arch", DOMAIN_PROJECT),
    ("repo:", DOMAIN_PROJECT),
    ("config:", DOMAIN_PROJECT),
    # Knowledge — explicit knowledge namespace
    ("knowledge:", DOMAIN_KNOWLEDGE),
    ("fact:knowledge", DOMAIN_KNOWLEDGE),
    ("skill:", DOMAIN_KNOWLEDGE),
)


def infer_domain(key: str | None) -> str:
    """Best-effort domain inference from a fact key.

    Returns one of the four DOMAINS values. When no rule matches, returns
    ``DOMAIN_KNOWLEDGE`` — the safe default per Memory Ontology v0.1.
    """
    if not key:
        return DOMAIN_KNOWLEDGE
    k = key.lower().lstrip()
    for prefix, domain in _KEY_PREFIX_RULES:
        if k.startswith(prefix):
            return domain
    return DOMAIN_KNOWLEDGE


# ---------------------------------------------------------------------------
# LLM-assisted classifier — used by the P1-C migration script to relabel
# the legacy default-domain rows in bulk via Loom's existing LLM router
# (MiniMax-M2.7 by default). Live `governed_upsert` writes still go through
# the heuristic ``infer_domain`` above for zero per-write LLM cost.
# ---------------------------------------------------------------------------

_LLM_SYSTEM = """You classify Loom memory facts along the `domain` axis of \
Memory Ontology v0.1. Return one label per fact from this closed set:

  - self      : agent identity, principles, values, self-awareness
  - user      : user preferences, relationship, known/unknown territory
  - project   : architecture decisions, config, workflow, code structure
  - knowledge : external knowledge, tool usage, research, third-party facts

Heuristics:
  * "I am / I prefer / my purpose" → self
  * "user wants / user prefers / about user" → user
  * "this repo / this project / loom config / file X does Y" → project
  * "Tool X works like / library Y / external API behavior" → knowledge

Output strict JSON only — no prose, no markdown fences:
  {"results": [{"i": 0, "d": "self"}, {"i": 1, "d": "knowledge"}, ...]}

`i` is the input index (0-based). `d` must be one of the four labels."""


def _build_user_prompt(items: list[tuple[str, str]]) -> str:
    """Render (key, value) items as a numbered list for the LLM."""
    lines = ["Classify each fact below. Reply with strict JSON only.\n"]
    for i, (key, value) in enumerate(items):
        # Trim long values — domain is recoverable from the first 240 chars
        snippet = value if len(value) <= 240 else value[:237] + "..."
        lines.append(f"[{i}] key={key!r}\n    value={snippet!r}")
    return "\n".join(lines)


def _parse_llm_response(raw: str, n: int) -> list[str]:
    """Parse the LLM's JSON list. Falls back to ``DOMAIN_KNOWLEDGE`` for any
    index the model omitted, returned in an unknown label, or otherwise broke."""
    out = [DOMAIN_KNOWLEDGE] * n
    if not raw:
        return out
    text = raw.strip()
    # Strip code fences if the model ignored instructions.
    if text.startswith("```"):
        text = text.strip("`").lstrip("json").strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Some models prepend reasoning — try to slice from first '{'.
        first = text.find("{")
        if first >= 0:
            try:
                data = json.loads(text[first:])
            except json.JSONDecodeError:
                logger.warning("LLM domain classifier: JSON parse failed, falling back")
                return out
        else:
            return out

    for item in data.get("results", []):
        try:
            i = int(item["i"])
            d = str(item["d"]).strip().lower()
        except (KeyError, TypeError, ValueError):
            continue
        if 0 <= i < n and d in DOMAINS:
            out[i] = d
    return out


class LLMDomainClassifier:
    """Batch domain classification via Loom's existing :class:`LLMRouter`.

    Designed for the P1-C one-shot migration — not wired into the live
    write path (the heuristic :func:`infer_domain` covers that with no
    LLM cost). Use the router that's already configured for the running
    Loom installation; defaults to ``minimax-m2.7`` since that's Loom's
    standard daily model.
    """

    def __init__(
        self,
        router,
        model: str = "minimax-m2.7",
        batch_size: int = 30,
        concurrency: int = 5,
    ):
        self._router = router
        self._model = model
        self._batch_size = batch_size
        self._concurrency = max(1, concurrency)

    async def classify_batch(
        self,
        items: list[tuple[str, str]],
        progress: "Callable[[int, int, float], None] | None" = None,
    ) -> list[str]:
        """Return one domain label per (key, value) pair.

        Items are chunked at ``batch_size`` and chunks run concurrently up
        to ``concurrency`` in flight. Any chunk that fails (network, parse,
        etc.) is filled with ``DOMAIN_KNOWLEDGE`` — callers can re-run the
        migration later for only those still-default rows.

        If ``progress`` is supplied it's called after every batch with
        ``(done, total, batch_seconds)`` — note ``done`` reflects completion
        order, not source order.
        """
        if not items:
            return []
        import asyncio

        total = len(items)
        chunks: list[tuple[int, list[tuple[str, str]]]] = []
        for offset in range(0, total, self._batch_size):
            chunks.append((offset, items[offset : offset + self._batch_size]))

        results: list[str] = [DOMAIN_KNOWLEDGE] * total
        sem = asyncio.Semaphore(self._concurrency)
        completed = 0

        async def _one(offset: int, chunk: list[tuple[str, str]]) -> None:
            nonlocal completed
            async with sem:
                t0 = time.monotonic()
                try:
                    domains = await self._classify_chunk(chunk)
                except Exception as exc:
                    logger.warning(
                        "LLMDomainClassifier batch %d-%d failed (%s) — "
                        "using DEFAULT_DOMAIN for these rows; safe to re-run",
                        offset, offset + len(chunk), exc,
                    )
                    domains = [DOMAIN_KNOWLEDGE] * len(chunk)
                for i, d in enumerate(domains):
                    results[offset + i] = d
                completed += len(chunk)
                if progress is not None:
                    progress(completed, total, time.monotonic() - t0)

        await asyncio.gather(*(_one(o, c) for o, c in chunks))
        return results

    async def _classify_chunk(self, chunk: list[tuple[str, str]]) -> list[str]:
        messages = [
            {"role": "system", "content": _LLM_SYSTEM},
            {"role": "user", "content": _build_user_prompt(chunk)},
        ]
        response = await self._router.chat(
            model=self._model, messages=messages, max_tokens=2048,
        )
        return _parse_llm_response(response.text or "", len(chunk))
