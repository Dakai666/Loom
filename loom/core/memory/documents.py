"""
Document collections in recall (#650 S2, spec docs/designs/61 r3 §9.2).

Notes, diaries, journals and the research wiki live as Markdown files.  They
were invisible to ``recall`` (#643 symptom C): nothing pointed at them except
navigation facts written by hand.  This module makes explicitly configured
collections searchable *as documents* — no fact extraction, no embedding,
no scheduler, no extra model call.

Model
-----
A **section** is the text under one Markdown ATX heading (``#``–``###``),
heading line included; text before the first heading is ``_preamble``.  Its
**anchor** is the heading path (``book/chapter``), with ``~2``, ``~3`` for
repeated headings in one file.  A section's ref is::

    doc:{collection}/{path}#{anchor}@{first 8 hex of sha256(section)}

``doc_versions`` keeps every section version ever observed (DK decision A1:
history is kept).  It is the *source* of that history, not a cache — the
files are not under git, so an old version exists nowhere else.  Only the
search projections (``doc_fts`` trigram, ``doc_terms`` CJK bigrams) are
rebuildable.  ``doc_current`` says which version is current per anchor;
``doc_files`` holds each file's fingerprint and state.

Promises (and their limits)
---------------------------
- A section whose content and anchor did not change keeps its ref; every
  stored version stays readable by ref.  Any content change yields a new ref.
- Revision links are not inferred: a version is "old" when its anchor's
  current version differs; an anchor missing from the current file is
  "no longer in the file".
- Deleted / moved-out-of-scope files report a status only, never content.
- A move is recognised only when, in one sync, exactly one known file
  vanished and exactly one never-seen file appeared with the *same whole-file
  digest*; otherwise it is a deletion plus a new file.
- Change detection is metadata-only: ``mtime_ns``, ``size``, ``inode`` and
  ``ctime_ns``.  ``ctime`` cannot be set back by ``utime``, so edits that
  restore the mtime and atomic replaces are both seen.
- Every symlink inside a collection is refused; collection roots must
  resolve inside the workspace.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import stat
import time
import weakref
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import aiosqlite

from loom.core.memory.recall_index import bigram_projection, is_cjk_char

logger = logging.getLogger(__name__)

# Bounds (spec 61 r3 §9.2).  Per recall: files re-read, bytes read, seconds.
SYNC_MAX_FILES = 40
SYNC_MAX_BYTES = 4 * 1024 * 1024
SYNC_SECONDS = 0.5
MAX_FILE_BYTES = 2 * 1024 * 1024
DISCOVERY_MAX_ENTRIES = 20_000
# Matching rows read before grouping into sections; candidates per route.
_MATCH_ROWS = 400
ROUTE_SECTIONS = 40
# Expansion budget: whole output, and what each neighbour section keeps.
EXPAND_CHARS = 8_000
_NEIGHBOUR_RESERVE = 1_200
# bm25 column weight of a section's own heading against its body.
_TITLE_WEIGHT = 10.0

_COLLECTION_NAME = re.compile(r"^[A-Za-z0-9_-]+$")
_HEADING = re.compile(r"^(#{1,3})[ \t]+(.+?)(?:[ \t]+#+)?[ \t]*$")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_DOC_REF = re.compile(
    r"^doc:(?P<c>[A-Za-z0-9_-]+)/(?P<p>[^#]+)#(?P<a>[^@]*)(?:@(?P<d>[0-9a-f]{8,64}))?$"
)
_ENCODE = {"%": "%25", "#": "%23", "@": "%40", " ": "%20", "\t": "%09", "\n": "%0A"}

_DDL = """
CREATE TABLE IF NOT EXISTS doc_versions (
    id          INTEGER PRIMARY KEY,
    collection  TEXT NOT NULL,
    path        TEXT NOT NULL,
    anchor      TEXT NOT NULL,
    digest      TEXT NOT NULL,
    title       TEXT NOT NULL,
    content     TEXT NOT NULL,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    UNIQUE (collection, path, anchor, digest)
);
CREATE TABLE IF NOT EXISTS doc_current (
    collection  TEXT NOT NULL,
    path        TEXT NOT NULL,
    anchor      TEXT NOT NULL,
    version_id  INTEGER NOT NULL,
    ord         INTEGER NOT NULL,
    PRIMARY KEY (collection, path, anchor)
);
CREATE TABLE IF NOT EXISTS doc_files (
    collection  TEXT NOT NULL,
    path        TEXT NOT NULL,
    state       TEXT NOT NULL,          -- present | removed | moved
    mtime_ns    INTEGER,
    size        INTEGER,
    inode       INTEGER,
    ctime_ns    INTEGER,
    file_digest TEXT,
    moved_to    TEXT,                   -- "{collection}/{path}" when moved
    checked_at  TEXT,
    PRIMARY KEY (collection, path)
);
CREATE TABLE IF NOT EXISTS doc_collections (
    name        TEXT PRIMARY KEY,
    root        TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS doc_fts
USING fts5(title, content, content='doc_versions', content_rowid='id', tokenize='trigram');
CREATE VIRTUAL TABLE IF NOT EXISTS doc_terms
USING fts5(terms, content='', contentless_delete=1, detail='none', tokenize='unicode61');
"""

# One lock per connection: two recalls in one process must not interleave a
# file's version rows.  Other processes are serialised by SQLite itself and
# by the re-check before each file's writes.
_LOCKS: "weakref.WeakKeyDictionary[Any, asyncio.Lock]" = weakref.WeakKeyDictionary()


def _lock_for(db: Any) -> asyncio.Lock:
    lock = _LOCKS.get(db)
    if lock is None:
        lock = _LOCKS[db] = asyncio.Lock()
    return lock


# ---------------------------------------------------------------------------
# Sections and refs
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Section:
    anchor: str
    title: str
    content: str
    ord: int


def split_sections(text: str) -> list[Section]:
    """Split Markdown into heading sections (spec 61 r3 §9.2).

    ATX headings ``#``–``###`` outside code fences start a section; deeper
    headings and setext underlines stay body text.  No length-based split.
    """
    out: list[Section] = []
    stack: list[tuple[int, str]] = []
    seen: dict[str, int] = {}
    anchor, title, buf = "_preamble", "", []
    fence: str | None = None

    def flush() -> None:
        body = "".join(buf)
        if anchor == "_preamble" and not body.strip():
            return
        out.append(Section(anchor, title, body.strip("\n"), len(out)))

    for line in text.splitlines(keepends=True):
        bare = line.rstrip("\r\n")
        mark = _FENCE.match(bare)
        if fence is not None:
            if mark and mark.group(1)[0] == fence[0] and len(mark.group(1)) >= len(fence):
                fence = None
            buf.append(line)
            continue
        if mark:
            fence = mark.group(1)
            buf.append(line)
            continue
        heading = _HEADING.match(bare)
        if not heading:
            buf.append(line)
            continue
        flush()
        level, name = len(heading.group(1)), heading.group(2).strip()
        stack = [s for s in stack if s[0] < level] + [(level, name)]
        base = "/".join(s[1] for s in stack)
        seen[base] = seen.get(base, 0) + 1
        anchor = base if seen[base] == 1 else f"{base}~{seen[base]}"
        title, buf = name, [line]
    flush()
    return out


def _encode(part: str) -> str:
    return "".join(_ENCODE.get(ch, ch) for ch in part)


def format_doc_ref(collection: str, path: str, anchor: str, digest: str | None) -> str:
    ref = f"doc:{collection}/{_encode(path)}#{_encode(anchor)}"
    return f"{ref}@{digest[:8]}" if digest else ref


def _safe_relpath(path: str) -> bool:
    parts = path.split("/")
    return not path.startswith("/") and all(p not in ("", ".", "..") for p in parts)


def parse_doc_ref(ref: str) -> tuple[str, str, str, str | None] | None:
    """``doc:`` ref → (collection, path, anchor, digest prefix or None)."""
    m = _DOC_REF.match((ref or "").strip())
    if not m:
        return None
    path, anchor = unquote(m["p"]), unquote(m["a"])
    if not _safe_relpath(path):
        return None
    return m["c"], path, anchor, m["d"]


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


# ---------------------------------------------------------------------------
# File access
# ---------------------------------------------------------------------------

Fingerprint = tuple[int, int, int, int]


def _fingerprint(st: os.stat_result) -> Fingerprint:
    return (st.st_mtime_ns, st.st_size, st.st_ino, st.st_ctime_ns)


def _read_bytes(fd: int, limit: int) -> bytes:
    chunks, total = [], 0
    while total < limit:
        piece = os.read(fd, min(1 << 16, limit - total))
        if not piece:
            break
        chunks.append(piece)
        total += len(piece)
    return b"".join(chunks)


def _inside(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


class _Deferred(Exception):
    """The file changed while being read; try again next sync."""


@dataclass
class SyncReport:
    collections: int = 0
    present: int = 0
    pending: int = 0
    unreadable: int = 0
    too_large: int = 0
    discovery_incomplete: bool = False
    bad_collections: list[str] = field(default_factory=list)

    def note(self) -> str:
        cols = "collection" if self.collections == 1 else "collections"
        parts = [f"documents: lexical ({self.collections} {cols}, {self.present} files)"]
        if self.pending:
            parts.append(f"{self.pending} files pending re-index")
        if self.discovery_incomplete:
            parts.append("discovery incomplete — removed files not yet detected")
        if self.unreadable:
            parts.append(f"{self.unreadable} unreadable")
        if self.too_large:
            parts.append(f"{self.too_large} too large to index")
        if self.bad_collections:
            parts.append("unusable collection roots: " + ", ".join(self.bad_collections))
        return ", ".join(parts)


@dataclass
class _Read:
    collection: str
    path: str
    fp: Fingerprint
    data: bytes
    never_seen: bool


# ---------------------------------------------------------------------------
# DocumentIndex
# ---------------------------------------------------------------------------

class DocumentIndex:
    """Configured document collections, synced on demand and searchable."""

    def __init__(
        self,
        db: aiosqlite.Connection,
        workspace: Path,
        collections: dict[str, str],
        *,
        max_files: int = SYNC_MAX_FILES,
        max_bytes: int = SYNC_MAX_BYTES,
        max_file_bytes: int = MAX_FILE_BYTES,
        seconds: float = SYNC_SECONDS,
        max_entries: int = DISCOVERY_MAX_ENTRIES,
    ) -> None:
        self._db = db
        self._workspace = os.path.realpath(workspace)
        self._config = dict(collections or {})
        self._max_files = max_files
        self._max_bytes = max_bytes
        self._max_file_bytes = max_file_bytes
        self._seconds = seconds
        self._max_entries = max_entries
        self._ready = False
        self._roots: dict[str, str] = {}
        self._bad: list[str] = []
        for name, rel in self._config.items():
            root = os.path.realpath(os.path.join(self._workspace, str(rel)))
            if (not _COLLECTION_NAME.match(name) or os.path.isabs(str(rel))
                    or not _inside(root, self._workspace) or not os.path.isdir(root)):
                self._bad.append(name)
                continue
            self._roots[name] = root

    @property
    def configured(self) -> bool:
        return bool(self._config)

    async def _ensure(self) -> None:
        if not self._ready:
            await self._db.executescript(_DDL)
            await self._db.commit()
            self._ready = True

    # -- sync ------------------------------------------------------------

    async def sync(self) -> SyncReport:
        """Bring the index up to date within this call's budget."""
        await self._ensure()
        async with _lock_for(self._db):
            return await self._sync()

    async def _sync(self) -> SyncReport:
        db = self._db
        report = SyncReport(collections=len(self._roots), bad_collections=list(self._bad))
        await self._check_roots()

        seen, complete = self._discover(report)
        names = list(self._roots)
        known: dict[tuple[str, str], tuple] = {}
        if names:
            marks = ",".join("?" * len(names))
            cur = await db.execute(
                "SELECT collection, path, state, mtime_ns, size, inode, ctime_ns, "
                f"file_digest, checked_at FROM doc_files WHERE collection IN ({marks})",
                names,
            )
            for row in await cur.fetchall():
                known[(row[0], row[1])] = row[2:]

        new = sorted(k for k in seen if k not in known or known[k][0] != "present")
        changed = sorted(
            (k for k in seen if k in known and known[k][0] == "present"
             and tuple(known[k][1:5]) != seen[k]),
            key=lambda k: known[k][6] or "",
        )
        vanished = [k for k, v in known.items()
                    if v[0] == "present" and k not in seen and k[0] in complete]
        gone_digests = {known[k][5] for k in vanished if known[k][5]}

        # Read within budget: new files first, then the longest-unchecked.
        reads: list[_Read] = []
        work = new + changed
        deadline = time.monotonic() + self._seconds
        spent = 0
        unread = 0
        for i, key in enumerate(work):
            if (len(reads) >= self._max_files or spent >= self._max_bytes
                    or time.monotonic() >= deadline):
                unread = len(work) - i
                break
            size = seen[key][1]
            if size > self._max_file_bytes:
                report.too_large += 1
                continue
            try:
                data = self._read_verified(key, seen[key])
            except _Deferred:
                report.pending += 1
                continue
            except FileNotFoundError:
                report.pending += 1
                continue
            except OSError:
                report.unreadable += 1
                continue
            spent += len(data)
            reads.append(_Read(key[0], key[1], seen[key], data, key not in known))
        report.pending += unread
        # Disappearances wait until every new file has been read, so a move
        # is never mistaken for a deletion because its target was not read yet.
        read_keys = {(r.collection, r.path) for r in reads}
        all_new_read = all(
            k in read_keys for k in new if seen[k][1] <= self._max_file_bytes
        )

        # Moves: one vanished file ↔ one never-seen file, same whole digest.
        confirmed_gone: list[tuple[str, str]] = []
        if vanished:
            for key in vanished:
                try:
                    st = os.lstat(os.path.join(self._roots[key[0]], key[1]))
                    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
                        confirmed_gone.append(key)      # out of scope now
                    # still a regular file but not discovered → leave as is
                except FileNotFoundError:
                    confirmed_gone.append(key)
                except OSError:
                    report.unreadable += 1
        moves: dict[tuple[str, str], _Read] = {}
        if confirmed_gone and all_new_read:
            by_digest_new: dict[str, list[_Read]] = {}
            for r in reads:
                if r.never_seen:
                    by_digest_new.setdefault(_digest_bytes(r.data), []).append(r)
            by_digest_gone: dict[str, list[tuple[str, str]]] = {}
            for key in confirmed_gone:
                if known[key][5]:
                    by_digest_gone.setdefault(known[key][5], []).append(key)
            for d, olds in by_digest_gone.items():
                news = by_digest_new.get(d, [])
                if len(olds) == 1 and len(news) == 1:
                    moves[olds[0]] = news[0]

        moved_targets = {id(r) for r in moves.values()}
        for r in reads:
            if id(r) in moved_targets:
                continue
            if (r.never_seen and gone_digests and not all_new_read
                    and _digest_bytes(r.data) in gone_digests):
                report.pending += 1   # may be a move target; decide once all is read
                continue
            if not await self._commit_file(r):
                report.pending += 1

        if all_new_read:
            for old, r in moves.items():
                if not await self._commit_move(old, r):
                    report.pending += 1
            for key in confirmed_gone:
                if key not in moves:
                    await self._mark_removed(key)

        cur = await db.execute(
            "SELECT COUNT(*) FROM doc_files WHERE state='present' AND collection IN "
            f"({','.join('?' * len(names))})" if names else "SELECT 0",
            names,
        )
        report.present = (await cur.fetchone())[0]
        report.discovery_incomplete = len(complete) < len(self._roots)
        return report

    async def _check_roots(self) -> None:
        """A collection whose root moved is out of scope as a whole."""
        db = self._db
        for name, root in self._roots.items():
            cur = await db.execute("SELECT root FROM doc_collections WHERE name=?", (name,))
            row = await cur.fetchone()
            if row and row[0] == root:
                continue
            if row:
                # Everything seen under the old root moves under a retired
                # name: kept (nothing is erased), never shown again, and never
                # mistaken for history of a same-named file under the new root.
                # ``~`` cannot occur in a configured collection name.
                retired = f"{name}~retired~{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%f')}"
                await db.execute("DELETE FROM doc_current WHERE collection=?", (name,))
                await db.execute(
                    "UPDATE doc_files SET collection=?, state='removed', moved_to=NULL "
                    "WHERE collection=?", (retired, name))
                await db.execute("UPDATE doc_versions SET collection=? WHERE collection=?",
                                 (retired, name))
            await db.execute(
                "INSERT INTO doc_collections(name, root) VALUES (?, ?) "
                "ON CONFLICT(name) DO UPDATE SET root=excluded.root", (name, root))
            await db.commit()

    def _discover(self, report: SyncReport) -> tuple[dict, set]:
        """Walk every collection; refuse symlinks; bounded by entry count."""
        seen: dict[tuple[str, str], Fingerprint] = {}
        complete: set[str] = set()
        budget = self._max_entries
        for name, root in self._roots.items():
            ok = True
            stack = [root]
            while stack:
                folder = stack.pop()
                try:
                    with os.scandir(folder) as it:
                        entries = list(it)
                except OSError:
                    report.unreadable += 1
                    ok = False
                    continue
                for entry in entries:
                    budget -= 1
                    if budget < 0:
                        ok = False
                        stack.clear()
                        break
                    if entry.name.startswith(".") or entry.is_symlink():
                        continue
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif (entry.is_file(follow_symlinks=False)
                              and entry.name.lower().endswith(".md")):
                            rel = os.path.relpath(entry.path, root).replace(os.sep, "/")
                            seen[(name, rel)] = _fingerprint(entry.stat(follow_symlinks=False))
                    except OSError:
                        report.unreadable += 1
            if ok and budget >= 0:
                complete.add(name)
        return seen, complete

    def _read_verified(self, key: tuple[str, str], fp: Fingerprint) -> bytes:
        root = self._roots[key[0]]
        full = os.path.join(root, key[1])
        if not _inside(os.path.realpath(full), root):
            raise PermissionError(full)
        fd = os.open(full, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            if _fingerprint(os.fstat(fd)) != fp:
                raise _Deferred(full)
            data = _read_bytes(fd, self._max_file_bytes + 1)
            if _fingerprint(os.fstat(fd)) != fp:
                raise _Deferred(full)
        finally:
            os.close(fd)
        return data

    def _unchanged(self, r: _Read) -> bool:
        """Re-check right before writing: a newer read may have been committed."""
        try:
            st = os.lstat(os.path.join(self._roots[r.collection], r.path))
        except OSError:
            return False
        return stat.S_ISREG(st.st_mode) and _fingerprint(st) == r.fp

    async def _commit_file(self, r: _Read) -> bool:
        if not self._unchanged(r):
            return False
        db = self._db
        now = datetime.now(UTC).isoformat()
        text = r.data.decode("utf-8", "replace")
        current: list[tuple[str, int, int]] = []
        for sec in split_sections(text):
            version_id = await self._upsert_version(r.collection, r.path, sec, now)
            current.append((sec.anchor, version_id, sec.ord))
        await db.execute("DELETE FROM doc_current WHERE collection=? AND path=?",
                         (r.collection, r.path))
        await db.executemany(
            "INSERT INTO doc_current(collection, path, anchor, version_id, ord) "
            "VALUES (?, ?, ?, ?, ?)",
            [(r.collection, r.path, a, vid, o) for a, vid, o in current],
        )
        await self._put_file(r, now)
        await db.commit()
        return True

    async def _upsert_version(self, collection: str, path: str, sec: Section, now: str) -> int:
        db = self._db
        digest = _digest(sec.content)
        cur = await db.execute(
            "INSERT OR IGNORE INTO doc_versions(collection, path, anchor, digest, title, "
            "content, first_seen, last_seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (collection, path, sec.anchor, digest, sec.title, sec.content, now, now),
        )
        if cur.rowcount == 1:
            vid = cur.lastrowid
            await db.execute("INSERT INTO doc_fts(rowid, title, content) VALUES (?, ?, ?)",
                             (vid, sec.title, sec.content))
            await db.execute("INSERT INTO doc_terms(rowid, terms) VALUES (?, ?)",
                             (vid, bigram_projection(sec.content)))
            return vid
        cur = await db.execute(
            "SELECT id FROM doc_versions WHERE collection=? AND path=? AND anchor=? AND digest=?",
            (collection, path, sec.anchor, digest),
        )
        vid = (await cur.fetchone())[0]
        await db.execute("UPDATE doc_versions SET last_seen=? WHERE id=?", (now, vid))
        return vid

    async def _put_file(self, r: _Read, now: str) -> None:
        mtime, size, ino, ctime = r.fp
        await self._db.execute(
            "INSERT INTO doc_files(collection, path, state, mtime_ns, size, inode, ctime_ns, "
            "file_digest, moved_to, checked_at) VALUES (?, ?, 'present', ?, ?, ?, ?, ?, NULL, ?) "
            "ON CONFLICT(collection, path) DO UPDATE SET state='present', "
            "mtime_ns=excluded.mtime_ns, size=excluded.size, inode=excluded.inode, "
            "ctime_ns=excluded.ctime_ns, file_digest=excluded.file_digest, moved_to=NULL, "
            "checked_at=excluded.checked_at",
            (r.collection, r.path, mtime, size, ino, ctime, _digest_bytes(r.data), now),
        )

    async def _commit_move(self, old: tuple[str, str], r: _Read) -> bool:
        if not self._unchanged(r):
            return False
        db = self._db
        now = datetime.now(UTC).isoformat()
        target = f"{r.collection}/{r.path}"
        for table in ("doc_versions", "doc_current"):
            await db.execute(
                f"UPDATE {table} SET collection=?, path=? WHERE collection=? AND path=?",
                (r.collection, r.path, old[0], old[1]),
            )
        await db.execute(
            "UPDATE doc_files SET state='moved', moved_to=?, checked_at=? "
            "WHERE collection=? AND path=?", (target, now, old[0], old[1]))
        # Earlier moves that pointed at the old path now point here.
        await db.execute("UPDATE doc_files SET moved_to=? WHERE moved_to=?",
                         (target, f"{old[0]}/{old[1]}"))
        await self._put_file(r, now)
        await db.commit()
        return True

    async def _mark_removed(self, key: tuple[str, str]) -> None:
        db = self._db
        await db.execute(
            "UPDATE doc_files SET state='removed', moved_to=NULL, checked_at=? "
            "WHERE collection=? AND path=?",
            (datetime.now(UTC).isoformat(), key[0], key[1]))
        await db.execute("DELETE FROM doc_current WHERE collection=? AND path=?", key)
        await db.commit()

    # -- search ----------------------------------------------------------

    async def search(self, terms: list[str]) -> tuple[list, str]:
        """Documents route: one ranked list, at most two slots per section."""
        from loom.core.memory.search import EXCERPT_CHARS, MemorySearchResult, _excerpt
        from loom.core.memory.session_log import _redact_secrets

        if not self.configured:
            return [], "documents: not configured"
        report = await self.sync()
        if not self._roots:
            return [], report.note()

        long_terms = [t for t in terms if len(t) >= 3]
        single = [t for t in terms if is_cjk_char(t)]
        short = [t for t in terms if len(t) < 3 and t not in single]
        names = list(self._roots)
        where = [
            f"v.collection IN ({','.join('?' * len(names))})",
            "EXISTS (SELECT 1 FROM doc_files f WHERE f.collection=v.collection "
            "AND f.path=v.path AND f.state='present')",
        ]
        params: list[Any] = list(names)
        if short:
            where.append("v.id IN (SELECT rowid FROM doc_terms WHERE doc_terms MATCH ?)")
            params.append(_phrase(short))
        for t in single:
            where.append("v.content LIKE ? ESCAPE '\\'")
            params.append(_like(t))
        select = (
            "SELECT v.id, v.collection, v.path, v.anchor, v.digest, v.content, "
            "c.version_id, cv.digest FROM doc_versions v "
            "LEFT JOIN doc_current c ON c.collection=v.collection AND c.path=v.path "
            "AND c.anchor=v.anchor LEFT JOIN doc_versions cv ON cv.id=c.version_id "
        )
        if long_terms:
            # Full-text hits first, materialised: joined directly, the planner
            # may drive from doc_versions and run MATCH once per row (≈0.8 s
            # on the live corpus for one identifier).
            # A section's heading is its name: a title hit outranks the same
            # words deep in a long body (bm25 alone favours short sections).
            sql = ("WITH hits AS MATERIALIZED (SELECT rowid AS id, "
                   f"bm25(doc_fts, {_TITLE_WEIGHT}, 1.0) AS rank FROM doc_fts "
                   "WHERE doc_fts MATCH ?) "
                   + select + "JOIN hits ON hits.id=v.id WHERE "
                   + " AND ".join(where) + " ORDER BY hits.rank LIMIT ?")
            args = [_phrase(long_terms), *params, _MATCH_ROWS]
        else:
            sql = select + "WHERE " + " AND ".join(where) + " ORDER BY v.id DESC LIMIT ?"
            args = [*params, _MATCH_ROWS]
        cur = await self._db.execute(sql, args)
        rows = await cur.fetchall()

        groups: dict[tuple, list] = {}
        for row in rows:
            groups.setdefault((row[1], row[2], row[3]), []).append(row)
        picked: list = []
        for group in groups.values():
            if len({(p[1], p[2], p[3]) for p in picked}) >= ROUTE_SECTIONS:
                break
            best = group[0]
            picked.append(best)
            if best[6] is not None and best[0] != best[6]:
                cur_row = next((g for g in group if g[0] == best[6]), None)
                if cur_row is not None:
                    picked.append(cur_row)

        results = []
        for vid, coll, path, anchor, digest, content, cur_id, cur_digest in picked:
            ref = format_doc_ref(coll, path, anchor, digest)
            status = _status(coll, path, anchor, vid, cur_id, cur_digest)
            text = _redact_secrets(content) or ""
            results.append(MemorySearchResult(
                type="document", key=ref,
                value=_excerpt(text, terms, EXCERPT_CHARS),
                score=0.0,
                metadata={"method": "document", "status": status},
                updated_at="", ref=ref,
            ))
        note = report.note()
        if len(rows) >= _MATCH_ROWS or len(groups) > ROUTE_SECTIONS:
            note += (f", more matching sections beyond the {ROUTE_SECTIONS}-section "
                     "budget (add terms to narrow)")
        return results, note

    # -- expansion -------------------------------------------------------

    async def expand(self, ref: str):
        """Identity read of a ``doc:`` ref (spec 61 r3 §9.2)."""
        from loom.core.memory.search import RefExpansion, _cut
        from loom.core.memory.session_log import _redact_secrets

        parsed = parse_doc_ref(ref)
        if parsed is None:
            return RefExpansion(
                f"Not a valid doc: ref: {ref!r}. Expected "
                "doc:{collection}/{path}#{section}[@hash] as shown in recall results.",
                found=False)
        coll, path, anchor, want = parsed
        gone = (f"{format_doc_ref(coll, path, anchor, want)} — deleted or moved out of "
                "scope; its text is no longer available.")
        if coll not in self._roots:
            return RefExpansion(gone, found=False)
        await self.sync()
        db = self._db

        notes: list[str] = []
        for _ in range(10):  # follow recorded moves
            cur = await db.execute(
                "SELECT state, moved_to FROM doc_files WHERE collection=? AND path=?",
                (coll, path))
            row = await cur.fetchone()
            if row is None or row[0] == "removed":
                return RefExpansion(gone, found=False)
            if row[0] == "present":
                break
            new_coll, _, new_path = (row[1] or "").partition("/")
            if new_coll not in self._roots or not new_path:
                return RefExpansion(gone, found=False)
            coll, path = new_coll, new_path
        else:
            return RefExpansion(gone, found=False)
        try:
            full = os.path.join(self._roots[coll], path)
            st = os.lstat(full)
            if (not stat.S_ISREG(st.st_mode)
                    or not _inside(os.path.realpath(full), self._roots[coll])):
                return RefExpansion(gone, found=False)
        except OSError:
            return RefExpansion(gone, found=False)

        cur = await db.execute(
            "SELECT v.id, v.digest, v.content, v.first_seen FROM doc_current c "
            "JOIN doc_versions v ON v.id=c.version_id "
            "WHERE c.collection=? AND c.path=? AND c.anchor=?", (coll, path, anchor))
        current = await cur.fetchone()
        target = current
        if want:
            cur = await db.execute(
                "SELECT id, digest, content, first_seen FROM doc_versions "
                "WHERE collection=? AND path=? AND anchor=? AND digest LIKE ?",
                (coll, path, anchor, want + "%"))
            found = await cur.fetchall()
            if len(found) > 1:
                return RefExpansion(
                    f"doc:… @{want} matches {len(found)} stored versions of this "
                    "section; use a longer hash.", found=False)
            if found:
                target = found[0]
            elif current is not None:
                notes.append(f"version @{want} was not saved (versions from before this "
                             "file was first indexed cannot be retrieved); showing the "
                             "current version")
        if target is None:
            return RefExpansion(
                f"{format_doc_ref(coll, path, anchor, want)} — no such section in the "
                "current file, and that version was not saved.", found=False)

        moved = (coll, path) != parsed[:2]
        if moved:
            notes.insert(0, f"moved to {format_doc_ref(coll, path, anchor, None)}")
        target_ref = format_doc_ref(coll, path, anchor, target[1])
        is_current = current is not None and target[0] == current[0]
        since = (target[3] or "")[:10]
        if is_current:
            head = f"{target_ref} (current; indexed since {since} — not the date it was written)"
        elif current is not None:
            head = (f"{target_ref} (old version — current: "
                    f"{format_doc_ref(coll, path, anchor, current[1])})")
            notes.append("the sections around this version at that time were not saved; "
                         "read the current file's sections through the current ref")
        else:
            head = f"{target_ref} (old version — section no longer in the file)"
            notes.append("the sections around this version at that time were not saved")
        if anchor.rpartition("~")[2].isdigit() and "~" in anchor and not is_current:
            notes.append("repeated heading — the current section at this position may "
                         "not be the same section")

        lines = [head] + [f"({n})" for n in notes]
        neighbours: list[tuple[str, str]] = []
        if is_current:
            cur = await db.execute(
                "SELECT c.anchor, v.digest, v.content, c.ord FROM doc_current c "
                "JOIN doc_versions v ON v.id=c.version_id WHERE c.collection=? AND c.path=? "
                "AND c.ord IN ((SELECT ord FROM doc_current WHERE collection=? AND path=? "
                "AND anchor=?) - 1, (SELECT ord FROM doc_current WHERE collection=? AND "
                "path=? AND anchor=?) + 1) ORDER BY c.ord",
                (coll, path, coll, path, anchor, coll, path, anchor))
            mine = await db.execute(
                "SELECT ord FROM doc_current WHERE collection=? AND path=? AND anchor=?",
                (coll, path, anchor))
            my_ord = (await mine.fetchone())[0]
            for n_anchor, n_digest, n_content, n_ord in await cur.fetchall():
                label = "previous" if n_ord < my_ord else "next"
                neighbours.append((
                    f"— {label} section {format_doc_ref(coll, path, n_anchor, n_digest)} (current):",
                    _redact_secrets(n_content) or ""))
        used = sum(len(l) + 1 for l in lines) + 1
        room = EXPAND_CHARS - used - _NEIGHBOUR_RESERVE * len(neighbours)
        body = _cut(_redact_secrets(target[2]) or "", max(200, room))
        out = lines + [body]
        for label, text in neighbours:
            share = _NEIGHBOUR_RESERVE - len(label) - 2
            out += ["", label, _cut(text, max(100, share))]
        return RefExpansion("\n".join(out)[:EXPAND_CHARS], found=True)


def _status(coll, path, anchor, vid, cur_id, cur_digest) -> str:
    if cur_id is None:
        return "old version — section no longer in the file"
    if cur_id == vid:
        return "current"
    status = f"old version — current: {format_doc_ref(coll, path, anchor, cur_digest)}"
    if "~" in anchor and anchor.rpartition("~")[2].isdigit():
        status += " (repeated heading — may not be the same section)"
    return status


def _digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _phrase(terms: list[str]) -> str:
    return " ".join('"' + t.replace('"', " ") + '"' for t in terms)


def _like(term: str) -> str:
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def collections_from_config(config: dict) -> dict[str, str]:
    """``[memory.documents] collections`` → {name: workspace-relative path}."""
    raw = ((config or {}).get("memory", {}) or {}).get("documents", {}) or {}
    cols = raw.get("collections", {}) or {}
    return {str(k): str(v) for k, v in cols.items()} if isinstance(cols, dict) else {}
