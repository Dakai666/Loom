"""
Relational ↔ Semantic bridge (issue #451 phase A).

Encodes (subject, predicate, object) triples as ``SemanticEntry`` rows so
that ``recall`` — currently scoped to semantic + skill — can naturally
surface relational facts written by dream cycle, ``relate`` tool, and
self-reflection. Phase B will retire the standalone ``relational_entries``
table once this routing is proven.

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
"""

from __future__ import annotations

from loom.core.memory.relational import RelationalEntry
from loom.core.memory.semantic import SemanticEntry


REL_KEY_PREFIX = "rel:"
REL_KEY_SEP = "::"


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
