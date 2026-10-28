"""
Memory Search — one recall entry over memory, skills and raw conversation.

#650 S1 (spec docs/designs/61 r2): every route gets its chance and none
short-circuits another.  Routes:

- memory, lexical   — ``semantic_fts`` (trigram) for terms of 3+ chars,
                      substring match for shorter terms;
- memory, embedding — cosine over stored vectors, only above
                      :data:`EMBEDDING_MIN_SCORE`;
- skills, lexical   — ``skill_fts``;
- conversation      — ``session_log_fts`` (trigram) / ``session_log_terms``
                      (CJK bigrams) over ``session_log``, one result per turn.

Query terms are whitespace-separated and ANDed; a long sentence is one
literal phrase.  Rewriting a natural question into search terms is the
model's job, not a hidden step here.  Ranks are merged with reciprocal-rank
fusion — raw scores are never compared across engines.  A query with no
match returns an empty list (never the most recent entries); the list's
``coverage`` line says which routes took part and where they were limited.

Usage
-----
    search = MemorySearch(semantic, procedural)
    results = await search.recall("夜間全量 驗證", limit=5)
    results.coverage   # one line, agent-facing
"""

from __future__ import annotations

import json
import logging
import math
import re
import time
from datetime import UTC, datetime
from dataclasses import dataclass, field
from typing import Any, Literal

from loom.core.timezone import user_zone

logger = logging.getLogger(__name__)

from loom.core.memory.procedural import ProceduralMemory
from loom.core.memory import recall_index
from loom.core.memory.session_log import _redact_secrets
from loom.core.memory.semantic import (
    SemanticMemory,
    _SELECT_COLS as _SEMANTIC_SELECT_COLS,
    _row_to_entry as _semantic_row_to_entry,
)


def _sanitize_fts(query: str) -> str:
    """Sanitize natural language into safe SQLite FTS5 MATCH format (AND logic)."""
    # Remove quotes to avoid syntax issues, then wrap each word in double quotes
    return " ".join(f'"{t}"' for t in query.replace('"', " ").split() if t)


def _semantic_result_type(key: str) -> str:
    """Issue #451: tag rel:* keys as ``"relational"`` so callers see the kind.

    Relational triples live in ``semantic_entries`` with key ``rel:{S}::{P}``
    (see ``loom/core/memory/relational_bridge.py``). They surface through
    the same BM25 / embedding paths as ordinary semantic facts; this helper
    just relabels them for the agent-facing display.
    """
    return "relational" if key.startswith("rel:") else "semantic"


def _axis_filter_sql(
    domain: str | None,
    temporal: str | None,
    prefix: str = "",
) -> tuple[str, tuple[str, ...]]:
    """Build a SQL fragment + params for optional (domain, temporal) filters.

    Returns ``("", ())`` when both axes are None — callers can splice the
    fragment after an existing ``WHERE`` clause without needing to branch
    on whether any filter was supplied. ``prefix`` is prepended to the
    column names for queries that join multiple tables (e.g. ``"e."``).
    """
    parts: list[str] = []
    params: list[str] = []
    if domain:
        parts.append(f"AND {prefix}domain = ?")
        params.append(domain)
    if temporal:
        parts.append(f"AND {prefix}temporal = ?")
        params.append(temporal)
    return (" " + " ".join(parts) if parts else ""), tuple(params)


def _time_filter_sql(
    since: datetime | None,
    until: datetime | None,
    prefix: str = "",
) -> tuple[str, tuple[str, ...]]:
    """Build SQL filters for semantic ``updated_at`` time windows."""
    parts: list[str] = []
    params: list[str] = []
    if since is not None:
        parts.append(f"AND {prefix}updated_at >= ?")
        params.append(since.isoformat())
    if until is not None:
        parts.append(f"AND {prefix}updated_at < ?")
        params.append(until.isoformat())
    return (" " + " ".join(parts) if parts else ""), tuple(params)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

MemoryType = Literal["semantic", "skill", "all"]

# Per-route candidate budget and output bounds (spec 61 r2 §1.2).
ROUTE_CANDIDATES = 40
EXCERPT_CHARS = 512
# Whole initial recall output, sources and coverage line included.
RECALL_OUTPUT_CHARS = 4_000
# Below this cosine an embedding neighbour is noise, not an answer: #643
# measured unrelated queries topping out at 0.45–0.52 against the live corpus
# while true hits sat at 0.68–0.75.  Lexical routes are unaffected.
EMBEDDING_MIN_SCORE = 0.55
_RRF_K = 60
# Single CJK characters have no index; scan newest-first within this budget.
_SCAN_SECONDS = 0.15
_SCAN_CHUNK_ROWS = 20_000
# Matched rows read per turn to build its excerpt.
_TURN_ROWS = 8
# Unindexed short-term rows a recall call will catch up before reading.
_TERMS_SYNC_BUDGET = 5_000


@dataclass
class MemorySearchResult:
    """A single ranked result from memory search."""
    type: str               # "semantic" | "relational" | "skill" | "conversation"
    key: str                # fact key, skill name, or conversation ref
    value: str              # fact value, skill body, or conversation excerpt
    score: float            # fused rank score — not a probability
    metadata: dict = field(default_factory=dict)
    updated_at: str = ""    # memory: record time; conversation: event time
    ref: str = ""           # stable reference: mem:/skill:/log:

    def __post_init__(self) -> None:
        if not self.ref:
            if self.type == "skill":
                self.ref = f"skill:{self.key}"
            elif self.type == "conversation":
                self.ref = self.key
            else:
                self.ref = f"mem:{self.key}"

    def format(self, max_value_len: int = 300) -> str:
        """Human-readable one-entry summary with timestamp and effective confidence.

        The stored ``updated_at`` is a UTC ISO string; the date hint is
        rendered in the user's configured timezone so it matches the
        same boundary the rest of the agent-facing surface uses
        (see issue #388). Naked ISO strings are treated as UTC.
        """
        truncated = self.value[:max_value_len]
        if len(self.value) > max_value_len:
            truncated += "…"
        date_hint = ""
        if self.updated_at:
            try:
                parsed = datetime.fromisoformat(self.updated_at.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=UTC)
                local_date = parsed.astimezone(user_zone()).strftime("%Y-%m-%d")
                date_hint = f" [{local_date}]"
            except ValueError:
                date_hint = f" [{self.updated_at[:10]}]"
        if self.type == "conversation":
            return f"[conversation]{date_hint} {self.ref}\n  {self.value[:EXCERPT_CHARS]}"
        conf = self.metadata.get("effective_confidence") or self.metadata.get("confidence")
        conf_hint = f" conf={conf:.2f}" if conf is not None else ""
        return f"[{self.type}]{date_hint}{conf_hint} {self.ref}\n  {truncated}"


class RecallResults(list):
    """``list[MemorySearchResult]`` plus a one-line ``coverage`` report."""

    def __init__(self, items=(), coverage: str = "") -> None:
        super().__init__(items)
        self.coverage = coverage


def _split_terms(query: str) -> list[str]:
    return [t for t in query.replace('"', " ").split() if t]


def _fts_phrase(terms: list[str]) -> str:
    return " ".join(f'"{t}"' for t in terms)


def _excerpt(text: str, terms: list[str], budget: int) -> str:
    """Window of *text* around the first matching term, at most *budget* chars."""
    if len(text) <= budget:
        return text
    lowered = text.lower()
    hits = [lowered.find(t.lower()) for t in terms]
    hits = [h for h in hits if h >= 0]
    start = max(0, min(hits) - budget // 4) if hits else 0
    window = text[start:start + budget]
    return ("…" if start else "") + window + ("…" if start + budget < len(text) else "")


# ---------------------------------------------------------------------------
# MemorySearch
# ---------------------------------------------------------------------------

class MemorySearch:
    """
    Ranked retrieval across semantic facts and procedural skills using BM25.

    BM25 indexes are cached in memory and revalidated on each call via a
    lightweight fingerprint query ``(COUNT, MAX(updated_at))``.  The full
    corpus fetch only runs when the fingerprint changes (new/updated entries).
    """

    def __init__(
        self,
        semantic: SemanticMemory,
        procedural: ProceduralMemory,
    ) -> None:
        self._semantic = semantic
        self._procedural = procedural
        self._health: Any = None  # Optional MemoryHealthTracker, set post-init

    async def recall(
        self,
        query: str,
        type: MemoryType = "all",
        limit: int = 5,
        domain: str | None = None,
        temporal: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> RecallResults:
        """
        Return up to *limit* results for *query* across every applicable route.

        Parameters
        ----------
        query:    Search terms, whitespace-separated and ANDed.  A long
                  sentence is matched as one literal phrase.
        type:     ``"semantic"`` (memory only), ``"skill"``, or ``"all"``
                  (memory, skills and conversation).
        limit:    Maximum number of results.
        domain / temporal:
                  Memory-Ontology filters.  They exist only on memory, so
                  conversation is excluded when either is set.
        since / until:
                  Hard limits: memory record time, conversation event time.
                  Skills carry no event time and are excluded when set.
        """
        terms = _split_terms(query)
        if not terms:
            return RecallResults()

        notes: list[str] = []
        routes: list[list[MemorySearchResult]] = []
        windowed = since is not None or until is not None

        if type in ("semantic", "all"):
            routes.append(await self._search_semantic(
                terms, ROUTE_CANDIDATES, domain=domain, temporal=temporal,
                since=since, until=until,
            ))
            memory_note = "memory: lexical"
            if self._semantic.has_embeddings:
                try:
                    routes.append(await self._search_semantic_embedding(
                        query, ROUTE_CANDIDATES, domain=domain, temporal=temporal,
                        since=since, until=until,
                    ))
                    memory_note += "+embedding"
                except Exception as exc:
                    logger.warning("Embedding search failed: %s", exc)
                    if self._health:
                        self._health.record_failure("embedding_search", str(exc))
                    memory_note += " (embedding failed)"
            if windowed:
                memory_note += ", by record time"
            notes.append(memory_note)

        if type in ("skill", "all"):
            if windowed:
                notes.append("skills: excluded (no event time)")
            else:
                routes.append(await self._search_skills(" ".join(terms), ROUTE_CANDIDATES))
                notes.append("skills: lexical")

        if type == "all":
            if domain or temporal:
                notes.append("conversation: excluded (domain/temporal apply to memory only)")
            else:
                found, note = await self._search_conversation(terms, since=since, until=until)
                routes.append(found)
                notes.append(note)
        else:
            notes.append(f"conversation: excluded (type={type})")

        fused = _fuse(routes)
        ranked = fused[:limit]
        await self._mark_accessed(ranked)
        notes.append(f"{len(fused)} candidates, showing {len(ranked)}")
        return RecallResults(ranked, coverage="Coverage — " + "; ".join(notes) + ".")

    async def _mark_accessed(self, results: list[MemorySearchResult]) -> None:
        """Bump last_accessed_at for semantic + relational hits (Memory Ontology v0.1).

        Issue #451 phase A: ``relational`` is a display label for ``rel:*``
        keys that live in ``semantic_entries`` (see ``_semantic_result_type``).
        Both kinds need their access timestamp refreshed; ``SemanticMemory.
        mark_accessed`` handles the relational source-table mirror.
        """
        keys = [r.key for r in results if r.type in ("semantic", "relational")]
        if keys:
            await self._semantic.mark_accessed(keys)

    async def _search_semantic_embedding(
        self, query: str, limit: int,
        domain: str | None = None,
        temporal: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[MemorySearchResult]:
        """Rank semantic entries by cosine similarity using sqlite-vec.

        Only neighbours at or above :data:`EMBEDDING_MIN_SCORE` count; stubs
        are excluded in SQL so they never take a LIMIT slot.
        """
        provider = self._semantic._embeddings
        if provider is None:
            return []

        query_vectors = await provider.embed([query])
        if not query_vectors:
            return []
        query_vec = query_vectors[0]

        axis_where, axis_params = _axis_filter_sql(domain, temporal)
        time_where, time_params = _time_filter_sql(since, until)
        cursor = await self._semantic._db.execute(
            f"""
            SELECT {_SEMANTIC_SELECT_COLS},
                   1.0 - vec_distance_cosine(embedding, ?) AS score
            FROM semantic_entries
            WHERE embedding IS NOT NULL
              AND json_extract(metadata, '$.redirected_to') IS NULL{axis_where}{time_where}
            ORDER BY vec_distance_cosine(embedding, ?) ASC
            LIMIT ?
            """,
            (
                json.dumps(query_vec),
                *axis_params,
                *time_params,
                json.dumps(query_vec),
                limit,
            )
        )
        rows = await cursor.fetchall()

        results: list[MemorySearchResult] = []
        for r in rows:
            # _SELECT_COLS produces 11 columns; index 11 holds the score.
            score = r[11]
            if score is None or score < EMBEDDING_MIN_SCORE:
                continue
            entry = _semantic_row_to_entry(r[:11])
            results.append(
                MemorySearchResult(
                    type=_semantic_result_type(entry.key),
                    key=entry.key,
                    value=entry.value,
                    score=score,
                    metadata={
                        "confidence": entry.confidence,
                        "effective_confidence": entry.effective_confidence(),
                        "method": "embedding",
                    },
                    updated_at=entry.updated_at.isoformat() if entry.updated_at else "",
                )
            )
        return results

    # ------------------------------------------------------------------

    async def _search_semantic(
        self, terms: list[str] | str, limit: int,
        domain: str | None = None,
        temporal: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[MemorySearchResult]:
        """Lexical memory route: trigram MATCH for long terms, substring for short.

        Every term must match (AND).  Stubs are excluded in SQL, before LIMIT.
        """
        if isinstance(terms, str):
            terms = _split_terms(terms)
        if not terms:
            return []
        long_terms = [t for t in terms if len(t) >= 3]
        short_terms = [t for t in terms if len(t) < 3]

        axis_where, axis_params = _axis_filter_sql(domain, temporal, prefix="e.")
        time_where, time_params = _time_filter_sql(since, until, prefix="e.")
        short_where = "".join(
            " AND (e.key LIKE ? ESCAPE '\\' OR e.value LIKE ? ESCAPE '\\')" for _ in short_terms
        )
        short_params = tuple(p for t in short_terms for p in (_like(t), _like(t)))
        select_cols = ", ".join(f"e.{c.strip()}" for c in _SEMANTIC_SELECT_COLS.split(","))
        live = " AND json_extract(e.metadata, '$.redirected_to') IS NULL"

        if long_terms:
            sql = f"""
                SELECT {select_cols}, bm25(semantic_fts) AS fts_score
                FROM semantic_fts
                JOIN semantic_entries e ON semantic_fts.rowid = e.rowid
                WHERE semantic_fts MATCH ?{live}{short_where}{axis_where}{time_where}
                ORDER BY rank
                LIMIT ?
            """
            params = (_fts_phrase(long_terms), *short_params, *axis_params, *time_params, limit)
        else:
            sql = f"""
                SELECT {select_cols}, 0.0 AS fts_score
                FROM semantic_entries e
                WHERE 1 = 1{live}{short_where}{axis_where}{time_where}
                ORDER BY e.updated_at DESC
                LIMIT ?
            """
            params = (*short_params, *axis_params, *time_params, limit)
        cursor = await self._semantic._db.execute(sql, params)
        rows = await cursor.fetchall()

        results: list[MemorySearchResult] = []
        for r in rows:
            entry = _semantic_row_to_entry(r[:11])
            results.append(
                MemorySearchResult(
                    type=_semantic_result_type(entry.key),
                    key=entry.key,
                    value=entry.value,
                    score=abs(r[11]) if r[11] else 0.0,
                    metadata={
                        "confidence": entry.confidence,
                        "effective_confidence": entry.effective_confidence(),
                        "method": "lexical",
                    },
                    updated_at=entry.updated_at.isoformat() if entry.updated_at else "",
                )
            )
        return results

    async def _search_conversation(
        self,
        terms: list[str],
        *,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> tuple[list[MemorySearchResult], str]:
        """Conversation route over ``session_log``: one result per turn.

        Candidates are picked per *turn*, never per row, so one turn with
        hundreds of matching rows cannot push other turns out of the pool.
        Returns ``(results, coverage note)``.
        """
        db = self._semantic._db
        if not await recall_index.indexes_ready(db):
            return [], "conversation: unavailable (index not built)"
        note_parts = ["conversation: lexical"]
        pending = await recall_index.sync_terms(db, budget=_TERMS_SYNC_BUDGET)
        if pending:
            note_parts.append(f"{pending} newest rows not yet in the short-term index")

        long_terms = [t for t in terms if len(t) >= 3]
        single_cjk = [t for t in terms if recall_index.is_cjk_char(t)]
        short_terms = [t for t in terms if len(t) < 3 and t not in single_cjk]

        # Row conditions that need no full-text index (role, single
        # characters, event time); index conditions are added per query.
        plain = [f"l.role IN {recall_index.SEARCHABLE_ROLES!r}"]
        plain_params: list[Any] = []
        for t in single_cjk:
            plain.append("l.content LIKE ? ESCAPE '\\'")
            plain_params.append(_like(t))
        if since is not None:
            plain.append("l.created_at >= ?")
            plain_params.append(since.isoformat())
        if until is not None:
            plain.append("l.created_at < ?")
            plain_params.append(until.isoformat())
        plain_where = " AND ".join(plain)
        # Turn picking: the driving index is joined; the other is a filter.
        match = list(plain)
        params = list(plain_params)
        if long_terms and short_terms:
            match.append(
                "l.id IN (SELECT rowid FROM session_log_terms WHERE session_log_terms MATCH ?)"
            )
            params.append(_fts_phrase(short_terms))
        where = " AND ".join(match)
        turn_cap = ROUTE_CANDIDATES + 1  # one extra to detect overflow

        if long_terms:
            cursor = await db.execute(
                "SELECT l.session_id, l.turn_index FROM session_log_fts f "
                "JOIN session_log l ON l.id = f.rowid "
                f"WHERE f.session_log_fts MATCH ? AND {where} "
                "GROUP BY l.session_id, l.turn_index ORDER BY MIN(f.rank) LIMIT ?",
                (_fts_phrase(long_terms), *params, turn_cap),
            )
            turns = await cursor.fetchall()
        elif short_terms:
            # Stream matches newest-first and stop once the turn budget is
            # full: cost follows the budget, not how common the term is.
            cursor = await db.execute(
                "SELECT l.session_id, l.turn_index FROM session_log_terms t "
                "JOIN session_log l ON l.id = t.rowid "
                f"WHERE session_log_terms MATCH ? AND {where} ORDER BY t.rowid DESC",
                (_fts_phrase(short_terms), *params),
            )
            turns = []
            seen: set = set()
            while len(turns) < turn_cap:
                batch = await cursor.fetchmany(256)
                if not batch:
                    break
                for turn in batch:
                    if turn not in seen and len(turns) < turn_cap:
                        seen.add(turn)
                        turns.append(turn)
            await cursor.close()
        else:
            turns, frontier = await self._scan_conversation(db, where, params, since, until)
            if frontier:
                lo, earliest = frontier
                note_parts.append(
                    f"single-character scan covered event times from {lo} on; earlier "
                    f"turns not searched (continue with since={earliest} until={lo})"
                )
        if len(turns) > ROUTE_CANDIDATES:
            turns = turns[:ROUTE_CANDIDATES]
            note_parts.append(
                f"more matching turns beyond the {ROUTE_CANDIDATES}-turn candidate budget "
                "(add terms or since/until to narrow)"
            )
        index_matches = []
        if long_terms:
            index_matches.append(("session_log_fts", _fts_phrase(long_terms)))
        if short_terms:
            index_matches.append(("session_log_terms", _fts_phrase(short_terms)))
        rows = await self._turn_rows(db, turns, plain_where, plain_params, index_matches)
        return _conversation_results(rows, terms), ", ".join(note_parts)

    @staticmethod
    async def _turn_rows(db, turns, plain_where: str, plain_params: list[Any],
                         index_matches: list[tuple[str, str]]) -> list:
        """Matched rows of the picked turns, in turn rank order, bounded per turn.

        Each full-text index is queried once over the picked rows' id range;
        a per-row correlated MATCH re-reads the doclist for every row of a
        long turn.
        """
        if not turns:
            return []
        picked = ", ".join("(?, ?, ?)" for _ in turns)
        picked_params = [v for i, (sid, turn) in enumerate(turns) for v in (sid, turn, i)]
        cursor = await db.execute(
            f"WITH picked(session_id, turn_index, ord) AS (VALUES {picked}) "
            "SELECT l.id, p.ord FROM picked p JOIN session_log l "
            "  ON l.session_id = p.session_id AND l.turn_index = p.turn_index "
            f"WHERE {plain_where}",
            (*picked_params, *plain_params),
        )
        order = dict(await cursor.fetchall())
        if not order:
            return []
        keep = set(order)
        lo, hi = min(keep), max(keep)
        for table, phrase in index_matches:
            cursor = await db.execute(
                f"SELECT rowid FROM {table} WHERE {table} MATCH ? AND rowid BETWEEN ? AND ?",
                (phrase, lo, hi),
            )
            keep &= {r[0] for r in await cursor.fetchall()}
        per_turn: dict[int, list[int]] = {}
        for row_id in sorted(keep):
            ids = per_turn.setdefault(order[row_id], [])
            if len(ids) < _TURN_ROWS:
                ids.append(row_id)
        wanted = [i for ord_ in sorted(per_turn) for i in per_turn[ord_]]
        cursor = await db.execute(
            "SELECT id, session_id, turn_index, role, content, metadata, created_at "
            "FROM session_log WHERE id IN (SELECT value FROM json_each(?))",
            (json.dumps(wanted),),
        )
        by_id = {r[0]: r for r in await cursor.fetchall()}
        return [by_id[i] for i in wanted if i in by_id]

    @staticmethod
    async def _scan_conversation(
        db, where: str, params: list[Any], since: datetime | None, until: datetime | None,
    ):
        """Newest-event-first bounded scan for single-character CJK terms.

        Walks ``created_at`` downwards in chunks, so the stopping point is an
        event time and ``since``/``until`` can continue exactly where it
        stopped — row ids are not event order (forks keep the original time).
        Returns ``(turns, frontier)``; ``frontier`` is ``(stopped_at,
        earliest)`` when older rows were left unsearched.
        """
        floor = since.isoformat() if since is not None else None
        deadline = time.monotonic() + _SCAN_SECONDS
        turns: list = []
        seen: set = set()
        hi: str | None = until.isoformat() if until is not None else None
        while len(turns) < ROUTE_CANDIDATES + 1:
            bound = ["1=1"]
            bound_params: list[Any] = []
            if hi is not None:
                bound.append("created_at < ?")
                bound_params.append(hi)
            if floor is not None:
                bound.append("created_at >= ?")
                bound_params.append(floor)
            cursor = await db.execute(
                f"SELECT created_at FROM session_log WHERE {' AND '.join(bound)} "
                "ORDER BY created_at DESC LIMIT 1 OFFSET ?",
                (*bound_params, _SCAN_CHUNK_ROWS - 1),
            )
            row = await cursor.fetchone()
            lo = row[0] if row else None
            chunk = [where]
            chunk_params = list(params)
            if hi is not None:
                chunk.append("l.created_at < ?")
                chunk_params.append(hi)
            if lo is not None:
                chunk.append("l.created_at >= ?")
                chunk_params.append(lo)
            cursor = await db.execute(
                f"SELECT l.session_id, l.turn_index FROM session_log l "
                f"WHERE {' AND '.join(chunk)} GROUP BY l.session_id, l.turn_index "
                "ORDER BY MAX(l.created_at) DESC",
                chunk_params,
            )
            for turn in await cursor.fetchall():
                if turn not in seen:
                    seen.add(turn)
                    turns.append(turn)
            if lo is None:
                return turns, None
            hi = lo
            if time.monotonic() >= deadline:
                break
        if hi is None:
            return turns, None
        cursor = await db.execute(
            "SELECT MIN(created_at) FROM session_log WHERE created_at < ?"
            + (" AND created_at >= ?" if floor is not None else ""),
            (hi, floor) if floor is not None else (hi,),
        )
        earliest = (await cursor.fetchone())[0]
        return turns, ((hi, earliest) if earliest is not None else None)

    async def _search_skills(self, query: str, limit: int) -> list[MemorySearchResult]:
        if not query.strip():
            return []
            
        safe_query = _sanitize_fts(query)
        if not safe_query:
            return []

        cursor = await self._procedural._db.execute(
            """
            SELECT
                g.id, g.name, g.version, g.confidence, g.usage_count, 
                g.success_rate, g.parent_skill, g.deprecation_threshold, 
                g.tags, g.body, g.created_at, g.updated_at,
                bm25(skill_fts) AS fts_score
            FROM skill_fts
            JOIN skill_genomes g ON skill_fts.rowid = g.rowid
            WHERE skill_fts MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (safe_query, limit)
        )
        rows = await cursor.fetchall()

        from loom.core.memory.procedural import SkillGenome
        results: list[MemorySearchResult] = []
        for r in rows:
            skill = SkillGenome(
                id=r[0], name=r[1], version=r[2], confidence=r[3], usage_count=r[4],
                success_rate=r[5], parent_skill=r[6], deprecation_threshold=r[7],
                tags=json.loads(r[8]), body=r[9],
                created_at=datetime.fromisoformat(r[10]),
                updated_at=datetime.fromisoformat(r[11]),
            )
            if skill.confidence <= skill.deprecation_threshold:
                continue

            score = abs(r[12]) if r[12] else 0.0
            results.append(
                MemorySearchResult(
                    type="skill",
                    key=skill.name,
                    value=skill.body,
                    score=score,
                    metadata={"confidence": skill.confidence, "tags": skill.tags},
                    updated_at=skill.updated_at.isoformat() if skill.updated_at else "",
                )
            )

        return results[:limit]


# ---------------------------------------------------------------------------
# Fusion and conversation shaping
# ---------------------------------------------------------------------------

def _like(term: str) -> str:
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _fuse(routes: list[list[MemorySearchResult]]) -> list[MemorySearchResult]:
    """Reciprocal-rank fusion: rank positions merge, raw scores never do."""
    best: dict[str, MemorySearchResult] = {}
    fused: dict[str, float] = {}
    methods: dict[str, list[str]] = {}
    for route in routes:
        for rank, result in enumerate(route):
            ref = result.ref
            fused[ref] = fused.get(ref, 0.0) + 1.0 / (_RRF_K + rank + 1)
            best.setdefault(ref, result)
            method = result.metadata.get("method")
            if method and method not in methods.setdefault(ref, []):
                methods[ref].append(method)
    out = []
    for ref, result in best.items():
        result.score = fused[ref]
        if methods.get(ref):
            result.metadata["method"] = "+".join(methods[ref])
        out.append(result)
    out.sort(key=lambda r: r.score, reverse=True)
    return out


def _conversation_results(rows, terms: list[str]) -> list[MemorySearchResult]:
    """Group matched rows into one result per (session, turn), in rank order."""
    turns: dict[str, list] = {}
    for row in rows:
        ref = f"log:{row[1]}:{row[2]}"
        if ref not in turns and len(turns) >= ROUTE_CANDIDATES:
            continue
        turns.setdefault(ref, []).append(row)
    results: list[MemorySearchResult] = []
    seen_sources: set[tuple] = set()
    for ref, matched in turns.items():
        matched.sort(key=lambda r: r[0])
        # fork_session copies turn_index, role, content and created_at
        # verbatim; only that exact match proves a shared source.  Equal
        # text alone may be a recurring event and keeps its own slot.
        source = (matched[0][2], tuple((r[3], r[6], r[4]) for r in matched))
        if source in seen_sources:
            continue
        seen_sources.add(source)
        parts: list[str] = []
        budget = EXCERPT_CHARS
        for _id, _sid, _turn, role, content, metadata, _at in matched:
            text = _redact_secrets(content) or ""
            label = role
            if role == "tool":
                try:
                    tool_name = json.loads(metadata or "{}").get("tool_name", "tool")
                except Exception:
                    tool_name = "tool"
                label = f"tool {tool_name} (stored preview)"
            piece = f"{label}: {_excerpt(text, terms, max(80, budget - len(label) - 2))}"
            parts.append(piece)
            budget -= len(piece) + 3
            if budget <= 80:
                break
        excerpt = " / ".join(parts)[:EXCERPT_CHARS]
        results.append(MemorySearchResult(
            type="conversation",
            key=ref,
            value=excerpt,
            score=0.0,
            metadata={"method": "conversation", "rows": len(matched)},
            updated_at=matched[0][6] or "",
            ref=ref,
        ))
    return results
