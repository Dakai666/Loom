"""
AttentionList — the agent's own choice of what to carry forward (#635).

The corpus is written mostly by machines (session extraction, dreams); this
is the one place the agent itself decides what matters. It is deliberately
small and sits *beside* the facts, never inside them:

- **The list** — at most :data:`MAX_ITEMS` entries, each ``prepared`` (offer
  it to me next session) or ``shelved`` (I looked at it and set it aside).
  Replaced whole on every write, so an edit is one atomic ``memory_meta``
  row. Each item pins the fact's value hash; a fact rewritten, merged away
  or deleted since then silently drops out of the brief instead of
  resurfacing a judgement made about different content.
- **keep / let_go** — promotion is the existing ``temporal=milestone``
  tier (self/user/project never decay; knowledge 365 d; dreaming capped at
  21 d — :func:`~loom.core.memory.lifecycle.half_life_for`). A keep marks
  the fact ``metadata.kept_by = "attention"`` so let_go can only undo
  promotions this flow made; a milestone set by anyone else is left alone.
  Forgetting is let_go → recent → normal decay → prune.

Nothing here changes a fact's value, confidence, domain, source or
``updated_at``. MemoryPulse Hook G reads :meth:`prepared_brief_items` in
place of its old project×milestone top-3 when the list is enabled.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Callable

from loom.core.memory.ontology import TEMPORAL_MILESTONE, TEMPORAL_RECENT

if TYPE_CHECKING:
    import aiosqlite
    from loom.core.memory.semantic import SemanticEntry, SemanticMemory


MAX_ITEMS = 5
STANCES = ("prepared", "shelved")
DEFAULT_EXPIRES_DAYS = 7
MAX_EXPIRES_DAYS = 30
UNVISITED_MIN_DAYS = 30
UNVISITED_SAMPLE_N = 5

_META_KEY = "attention.list"
_KEPT_BY = "attention"


class AttentionError(ValueError):
    """Invalid write — nothing was changed."""


def value_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class AttentionItem:
    key: str
    stance: str
    reason: str
    value_hash: str
    expires_at: str
    set_at: str


@dataclass(frozen=True, slots=True)
class AttentionChange:
    added: list[str]
    removed: list[str]
    restanced: list[str]
    kept: list[str]
    let_go: list[str]
    unchanged_keep: list[str]
    unchanged_let_go: list[str]
    items: list[AttentionItem]


class AttentionList:
    def __init__(
        self,
        db: "aiosqlite.Connection",
        semantic: "SemanticMemory",
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._db = db
        self._semantic = semantic
        self._clock = clock or (lambda: datetime.now(UTC))

    # ── read ────────────────────────────────────────────────────────────

    async def read(self) -> list[AttentionItem]:
        cur = await self._db.execute(
            "SELECT value FROM memory_meta WHERE key = ?", (_META_KEY,),
        )
        row = await cur.fetchone()
        if not row:
            return []
        try:
            return [AttentionItem(**raw) for raw in json.loads(row[0])]
        except (ValueError, TypeError):
            return []

    async def status(self, item: AttentionItem) -> tuple[str, "SemanticEntry | None"]:
        """``live`` | ``expired`` | ``changed`` | ``merged`` | ``gone``."""
        entry = await self._semantic.get(item.key)
        if entry is None:
            return "gone", None
        if entry.metadata.get("redirected_to"):
            return "merged", entry
        if value_hash(entry.value) != item.value_hash:
            return "changed", entry
        if datetime.fromisoformat(item.expires_at) <= self._clock():
            return "expired", entry
        return "live", entry

    async def prepared_brief_items(self) -> list[tuple[AttentionItem, "SemanticEntry"]]:
        out = []
        for item in await self.read():
            if item.stance != "prepared":
                continue
            state, entry = await self.status(item)
            if state == "live" and entry is not None:
                out.append((item, entry))
        return out

    async def unvisited_sample(
        self, n: int = UNVISITED_SAMPLE_N, *, min_days: int = UNVISITED_MIN_DAYS,
    ) -> list["SemanticEntry"]:
        """A random handful of facts nobody has read in ``min_days``.

        recall and recall_period both need a cue; a memory the agent has no
        cue for never comes back on its own. This is its way in — chance,
        then the agent's judgement (keep, prepare, or ignore). Redirect
        stubs, dreaming hypotheses and items already on the list are left
        out. Showing a fact here is not a read: nothing is marked accessed.
        """
        from loom.core.memory.semantic import _SELECT_COLS, _row_to_entry

        cutoff = (self._clock() - timedelta(days=min_days)).isoformat()
        listed = [i.key for i in await self.read()]
        where = [
            "json_extract(metadata, '$.redirected_to') IS NULL",
            "COALESCE(source, '') != 'dreaming'",
            "COALESCE(last_accessed_at, created_at) < ?",
        ]
        params: list[Any] = [cutoff]
        if listed:
            where.append(f"key NOT IN ({','.join('?' * len(listed))})")
            params.extend(listed)
        params.append(max(int(n), 0))
        cur = await self._db.execute(
            f"SELECT {_SELECT_COLS} FROM semantic_entries "
            f"WHERE {' AND '.join(where)} ORDER BY RANDOM() LIMIT ?",
            tuple(params),
        )
        return [_row_to_entry(r) for r in await cur.fetchall()]

    # ── write ───────────────────────────────────────────────────────────

    async def write(
        self,
        *,
        items: list[dict[str, Any]] | None = None,
        keep: list[str] | None = None,
        let_go: list[str] | None = None,
    ) -> AttentionChange:
        """Validate everything first, then apply in one transaction.

        ``items=None`` leaves the list as is; ``items=[]`` clears it.

        The whole read → validate → write → commit runs under the shared
        memory rewrite lock: the daemon's dream passes rewrite facts on this
        same connection, so a merge landing between validation and the
        UPDATE would otherwise let ``keep`` promote a redirect stub (and a
        dream's commit could land half of this write). The UPDATEs are also
        conditional on what validation saw; a miss rejects the whole write.
        """
        # Lazy: the lock lives with the dream passes (cognition layer), as in
        # maintenance.py's distill path.
        from loom.core.cognition.consolidation import memory_rewrite_lock
        async with memory_rewrite_lock():
            return await self._write_locked(items=items, keep=keep, let_go=let_go)

    async def _write_locked(
        self,
        *,
        items: list[dict[str, Any]] | None,
        keep: list[str] | None,
        let_go: list[str] | None,
    ) -> AttentionChange:
        keep = _dedupe(keep or [])
        let_go = _dedupe(let_go or [])
        if set(keep) & set(let_go):
            raise AttentionError("a key cannot be both kept and let go in one write")

        old = await self.read()
        new_items = old if items is None else await self._validate_items(items)
        keep_entries = [await self._live_fact(k, "keep") for k in keep]
        let_go_entries = [await self._live_fact(k, "let_go") for k in let_go]

        kept, unchanged_keep = [], []
        for entry in keep_entries:
            if entry.temporal == TEMPORAL_MILESTONE:
                unchanged_keep.append(entry.key)     # someone else's anchor — not ours to own
            else:
                kept.append(entry.key)
        released, unchanged_let_go = [], []
        for entry in let_go_entries:
            if entry.temporal == TEMPORAL_MILESTONE and entry.metadata.get("kept_by") == _KEPT_BY:
                released.append(entry.key)
            else:
                unchanged_let_go.append(entry.key)

        now = self._clock().isoformat()
        try:
            if items is not None:
                await self._db.execute(
                    "INSERT INTO memory_meta(key, value, updated_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                    "updated_at = excluded.updated_at",
                    (_META_KEY,
                     json.dumps([asdict(i) for i in new_items], ensure_ascii=False),
                     now),
                )
            for key in kept:
                cur = await self._db.execute(
                    "UPDATE semantic_entries SET temporal = ?, metadata = json_set("
                    "COALESCE(metadata, '{}'), '$.kept_by', ?, '$.kept_at', ?) "
                    "WHERE key = ? AND temporal != ? "
                    "AND json_extract(metadata, '$.redirected_to') IS NULL",
                    (TEMPORAL_MILESTONE, _KEPT_BY, now, key, TEMPORAL_MILESTONE),
                )
                if cur.rowcount != 1:
                    raise AttentionError(f"keep: {key!r} changed while writing — look again")
            for key in released:
                cur = await self._db.execute(
                    "UPDATE semantic_entries SET temporal = ?, metadata = json_remove("
                    "metadata, '$.kept_by', '$.kept_at') WHERE key = ? AND temporal = ? "
                    "AND json_extract(metadata, '$.kept_by') = ? "
                    "AND json_extract(metadata, '$.redirected_to') IS NULL",
                    (TEMPORAL_RECENT, key, TEMPORAL_MILESTONE, _KEPT_BY),
                )
                if cur.rowcount != 1:
                    raise AttentionError(f"let_go: {key!r} changed while writing — look again")
            await self._db.commit()
        except Exception:
            await self._db.rollback()
            raise

        old_by_key = {i.key: i for i in old}
        new_by_key = {i.key: i for i in new_items}
        return AttentionChange(
            added=[k for k in new_by_key if k not in old_by_key],
            removed=[k for k in old_by_key if k not in new_by_key],
            restanced=[k for k in new_by_key
                       if k in old_by_key and old_by_key[k].stance != new_by_key[k].stance],
            kept=kept, let_go=released,
            unchanged_keep=unchanged_keep, unchanged_let_go=unchanged_let_go,
            items=new_items,
        )

    async def _validate_items(self, raw_items: list[dict[str, Any]]) -> list[AttentionItem]:
        if not isinstance(raw_items, list):
            raise AttentionError("items must be a list")
        if len(raw_items) > MAX_ITEMS:
            raise AttentionError(f"at most {MAX_ITEMS} items (got {len(raw_items)})")
        now = self._clock()
        seen: set[str] = set()
        out: list[AttentionItem] = []
        for raw in raw_items:
            if not isinstance(raw, dict):
                raise AttentionError("each item must be an object")
            key = str(raw.get("key", "")).strip()
            stance = str(raw.get("stance", "")).strip().lower()
            reason = str(raw.get("reason", "")).strip()
            if key in seen:
                raise AttentionError(f"duplicate key {key!r}")
            seen.add(key)
            if stance not in STANCES:
                raise AttentionError(f"{key!r}: stance must be one of {STANCES}")
            if not reason:
                raise AttentionError(f"{key!r}: reason is required")
            try:
                days = int(raw.get("expires_days", DEFAULT_EXPIRES_DAYS))
            except (TypeError, ValueError):
                raise AttentionError(f"{key!r}: expires_days must be an integer") from None
            if not 1 <= days <= MAX_EXPIRES_DAYS:
                raise AttentionError(f"{key!r}: expires_days must be 1-{MAX_EXPIRES_DAYS}")
            entry = await self._live_fact(key, "list")
            out.append(AttentionItem(
                key=key, stance=stance, reason=reason,
                value_hash=value_hash(entry.value),
                expires_at=(now + timedelta(days=days)).isoformat(),
                set_at=now.isoformat(),
            ))
        return out

    async def _live_fact(self, key: str, what: str) -> "SemanticEntry":
        entry = await self._semantic.get(key) if key else None
        if entry is None:
            raise AttentionError(f"{what}: no fact with key {key!r}")
        target = entry.metadata.get("redirected_to")
        if target:
            raise AttentionError(
                f"{what}: {key!r} was merged into {target!r} — use that key"
            )
        return entry


def _dedupe(keys: list[str]) -> list[str]:
    out: list[str] = []
    for k in keys:
        k = str(k).strip()
        if k and k not in out:
            out.append(k)
    return out
