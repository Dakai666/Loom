"""
Relational triples as a view on the semantic store (issue #451 phase B).

Phase A introduced the bridge as an additive mirror — relational triples
were dual-written into ``semantic_entries`` so ``recall`` could surface
them while ``relational_entries`` remained the source of truth. Phase B
retires the standalone table: the bridge is now the *only* path. Triples
live as ``SemanticEntry`` rows; this module owns the dataclass, the
encoding, and the read/write helpers that callers use instead of the
old ``RelationalMemory`` class.

Encoding rules:

* ``key   = f"rel:{subject}::{predicate}"`` — preserves the original
  ``(subject, predicate)`` uniqueness via the semantic ``key`` constraint.
  The double-colon separator avoids ambiguity with subjects that already
  contain single colons (e.g. ``minimax:tts:speech-2.6-hd``).
* ``value = f"{subject} {predicate} {object}"`` — natural-language form
  so BM25 and embedding tiers can match without special handling.
* ``metadata`` carries the authoritative ``subject`` / ``predicate`` /
  ``object`` triple for round-trip decoding. The key is for uniqueness
  only and is *not* parsed back into a triple.

Callers:

* Producers — ``relate`` tool, ``dream_cycle``, ``self_reflection``,
  ``counter_factual``, REST ``POST /memory/relational`` — use
  :func:`upsert_triple`.
* Readers — :class:`MemoryIndex` self-portrait projection, REST
  ``GET /memory/relational`` — use :func:`query_triples` or
  :func:`get_triple`.
* Agent recall — flows through the ordinary :class:`MemorySearch.recall`
  path and surfaces ``rel:*`` keys with ``type="relational"``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from loom.core.memory.ontology import (
    DEFAULT_DOMAIN,
    DEFAULT_TEMPORAL,
    normalize_domain,
    normalize_temporal,
)
from loom.core.memory.semantic import SemanticEntry

if TYPE_CHECKING:
    from loom.core.memory.semantic import SemanticMemory


REL_KEY_PREFIX = "rel:"
REL_KEY_SEP = "::"


# ---------------------------------------------------------------------------
# Dataclass — was loom/core/memory/relational.py:RelationalEntry until #451 B
# ---------------------------------------------------------------------------

@dataclass
class RelationalEntry:
    """A single (subject, predicate, object) triple.

    Persisted as a :class:`SemanticEntry` via :func:`triple_to_semantic`.
    Producers construct this and call :func:`upsert_triple`; readers get
    them back from :func:`query_triples` / :func:`get_triple`.
    """
    subject:   str
    predicate: str
    object:    str
    confidence: float = 1.0
    source:    str = "agent"
    metadata:  dict[str, Any] = field(default_factory=dict)
    id:        str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    domain:    str = DEFAULT_DOMAIN
    temporal:  str = DEFAULT_TEMPORAL
    last_accessed_at: datetime | None = None

    def __post_init__(self) -> None:
        self.domain = normalize_domain(self.domain)
        self.temporal = normalize_temporal(self.temporal)

    def effective_confidence(self) -> float:
        """Time-decayed confidence using the (domain, temporal) half-life."""
        from loom.core.memory.lifecycle import effective_confidence
        return effective_confidence(
            confidence=self.confidence,
            updated_at=self.updated_at,
            last_accessed_at=self.last_accessed_at,
            domain=self.domain,
            temporal=self.temporal,
        )


# ---------------------------------------------------------------------------
# Encoding
# ---------------------------------------------------------------------------

def make_rel_key(subject: str, predicate: str) -> str:
    return f"{REL_KEY_PREFIX}{subject}{REL_KEY_SEP}{predicate}"


def is_rel_key(key: str) -> bool:
    return key.startswith(REL_KEY_PREFIX)


def triple_to_semantic(entry: RelationalEntry) -> SemanticEntry:
    """Encode a ``RelationalEntry`` as a ``SemanticEntry`` for the semantic store."""
    metadata = dict(entry.metadata)
    metadata["subject"] = entry.subject
    metadata["predicate"] = entry.predicate
    metadata["object"] = entry.object
    return SemanticEntry(
        key=make_rel_key(entry.subject, entry.predicate),
        value=f"{entry.subject} {entry.predicate} {entry.object}",
        confidence=entry.confidence,
        source=entry.source,
        metadata=metadata,
        id=entry.id,
        created_at=entry.created_at,
        updated_at=entry.updated_at,
        domain=entry.domain,
        temporal=entry.temporal,
        last_accessed_at=entry.last_accessed_at,
    )


def semantic_to_triple(entry: SemanticEntry) -> RelationalEntry | None:
    """Decode a ``SemanticEntry`` back to a ``RelationalEntry``.

    Returns ``None`` if the entry is not a relational fact (key prefix
    mismatch, or metadata missing required keys).
    """
    if not is_rel_key(entry.key):
        return None
    md = entry.metadata or {}
    subject = md.get("subject")
    predicate = md.get("predicate")
    if not subject or not predicate:
        return None
    clean_md = {
        k: v for k, v in md.items()
        if k not in ("subject", "predicate", "object")
    }
    return RelationalEntry(
        subject=subject,
        predicate=predicate,
        object=md.get("object", entry.value),
        confidence=entry.confidence,
        source=entry.source or "agent",
        metadata=clean_md,
        id=entry.id,
        created_at=entry.created_at,
        updated_at=entry.updated_at,
        domain=entry.domain,
        temporal=entry.temporal,
        last_accessed_at=entry.last_accessed_at,
    )


# ---------------------------------------------------------------------------
# Read/write helpers — replace the retired ``RelationalMemory`` class
# ---------------------------------------------------------------------------

async def upsert_triple(
    semantic: "SemanticMemory",
    entry: RelationalEntry,
) -> None:
    """Store a triple. (S, P) uniqueness comes from the semantic ``key``."""
    await semantic.upsert(triple_to_semantic(entry))


async def get_triple(
    semantic: "SemanticMemory",
    subject: str,
    predicate: str,
) -> RelationalEntry | None:
    """Point lookup by (subject, predicate)."""
    sem_entry = await semantic.get(make_rel_key(subject, predicate))
    return semantic_to_triple(sem_entry) if sem_entry else None


async def query_triples(
    semantic: "SemanticMemory",
    subject: str | None = None,
    predicate: str | None = None,
    limit: int = 10_000,
) -> list[RelationalEntry]:
    """List triples matching the given filters.

    All filtering happens after the key-prefix scan against
    ``semantic_entries``. Passing neither filter returns every triple
    (capped by ``limit``); passing ``subject`` narrows to a key prefix
    scan; passing ``predicate`` filters in Python.
    """
    if subject is not None:
        prefix = f"{REL_KEY_PREFIX}{subject}{REL_KEY_SEP}"
        sem_entries = await semantic.list_by_prefix(prefix, limit=limit)
    else:
        sem_entries = await semantic.list_by_prefix(REL_KEY_PREFIX, limit=limit)

    triples: list[RelationalEntry] = []
    for s in sem_entries:
        t = semantic_to_triple(s)
        if t is None:
            continue
        if predicate is not None and t.predicate != predicate:
            continue
        triples.append(t)
    return triples


async def delete_triple(
    semantic: "SemanticMemory",
    subject: str,
    predicate: str,
) -> bool:
    """Delete the triple at (subject, predicate). Returns True if removed."""
    return await semantic.delete(make_rel_key(subject, predicate))
