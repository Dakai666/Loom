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
**anchor** is the heading path (``book/chapter``); a repeated anchor gets
``~2``, ``~3`` … chosen so every anchor in a file is unique.  A section's
ref is::

    doc:{collection}/{path}#{anchor}@{first 8 hex of sha256(section)}

A path or anchor that would expose a secret, or is too long to print, is
shown as ``~h{hash}`` instead and resolved back through the index.

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
- A file confirmed gone is hidden at once and reports a status only.
- A move is recognised when a never-seen file has the *same inode, size and
  whole-file digest* as exactly one removed file — what ``mv`` within the
  workspace does.  A copy-then-delete, or a move that also edited the file,
  stays a deletion plus a new file.
- Change detection is metadata-only: ``mtime_ns``, ``size``, ``inode`` and
  ``ctime_ns``.  ``ctime`` cannot be set back by ``utime``, so edits that
  restore the mtime and atomic replaces are both seen.
- Every symlink inside a collection is refused, at any path component;
  collection roots must resolve inside the workspace.

Writes go through a connection of this module's own, one ``BEGIN
IMMEDIATE`` transaction per file, re-verified inside the transaction — the
session's shared connection is never used for them, so another writer's
``commit()`` can never publish half a file.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import stat
import time
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import aiosqlite

from loom.core.memory.recall_index import bigram_projection, is_cjk_char
from loom.core.memory.session_log import _redact_secrets

logger = logging.getLogger(__name__)

# Bounds (spec 61 r3 §9.2).  Per recall: files re-read, bytes read, seconds.
SYNC_MAX_FILES = 40
SYNC_MAX_BYTES = 4 * 1024 * 1024
SYNC_SECONDS = 0.5
MAX_FILE_BYTES = 2 * 1024 * 1024
# Directory entries examined per recall; a larger tree is walked over
# several recalls, resuming where the last one stopped.
DISCOVERY_MAX_ENTRIES = 20_000
# Matching rows read before grouping into sections; candidates per route.
_MATCH_ROWS = 400
ROUTE_SECTIONS = 40
# Expansion budget: whole output, and what each neighbour section keeps.
EXPAND_CHARS = 8_000
_NEIGHBOUR_RESERVE = 1_200
# bm25 column weight of a section's own heading against its body.
_TITLE_WEIGHT = 10.0
# Longest path or anchor printed as-is inside a ref.
_REF_PART_CHARS = 120

_COLLECTION_NAME = re.compile(r"^[A-Za-z0-9_-]+$")
_HEADING = re.compile(r"^(#{1,3})[ \t]+(.+?)(?:[ \t]+#+)?[ \t]*$")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_DOC_REF = re.compile(
    r"^doc:(?P<c>[A-Za-z0-9_-]+)/(?P<p>[^#]+)#(?P<a>[^@]*)(?:@(?P<d>[0-9a-f]{8,64}))?$"
)
_HASHED = re.compile(r"^~h[0-9a-f]{12}$")
_ENCODE = {"%": "%25", "#": "%23", "@": "%40", " ": "%20", "\t": "%09", "\n": "%0A"}
_PREAMBLE = "_preamble"

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
CREATE INDEX IF NOT EXISTS idx_doc_files_inode ON doc_files(inode);
CREATE TABLE IF NOT EXISTS doc_collections (
    name        TEXT PRIMARY KEY,
    root        TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS doc_fts
USING fts5(title, content, content='doc_versions', content_rowid='id', tokenize='trigram');
CREATE VIRTUAL TABLE IF NOT EXISTS doc_terms
USING fts5(terms, content='', contentless_delete=1, detail='none', tokenize='unicode61');
"""

# One lock per DB file: two recalls in one process never write at once.
# Other processes are serialised by SQLite (BEGIN IMMEDIATE) and by the
# re-check inside each transaction.
_LOCKS: dict[str, asyncio.Lock] = {}


def _lock_for(path: str) -> asyncio.Lock:
    lock = _LOCKS.get(path)
    if lock is None:
        lock = _LOCKS[path] = asyncio.Lock()
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
    Anchors are unique within the file, whatever the headings say.
    """
    out: list[Section] = []
    stack: list[tuple[int, str]] = []
    used: set[str] = set()
    anchor, title, buf = _PREAMBLE, "", []
    fence: str | None = None

    def unique(base: str) -> str:
        if base not in used:
            return base
        n = 2
        while f"{base}~{n}" in used:
            n += 1
        return f"{base}~{n}"

    def flush() -> None:
        body = "".join(buf)
        if anchor == _PREAMBLE and not body.strip():
            return
        used.add(anchor)
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
        if base == _PREAMBLE:
            base = f"{_PREAMBLE}~h"   # a heading literally named _preamble
        anchor = unique(base)
        used.add(anchor)
        title, buf = name, [line]
    flush()
    return out


def _encode(part: str) -> str:
    return "".join(_ENCODE.get(ch, ch) for ch in part)


def _hashed(part: str) -> str:
    return "~h" + hashlib.sha256(part.encode("utf-8", "replace")).hexdigest()[:12]


def _ref_part(part: str) -> str:
    """A path or anchor as printed: hashed if it would leak a secret or is long."""
    if len(part) > _REF_PART_CHARS or (_redact_secrets(part) or "") != part:
        return _hashed(part)
    return _encode(part)


def format_doc_ref(collection: str, path: str, anchor: str, digest: str | None) -> str:
    ref = f"doc:{collection}/{_ref_part(path)}#{_ref_part(anchor)}"
    return f"{ref}@{digest[:8]}" if digest else ref


def _safe_relpath(path: str) -> bool:
    parts = path.split("/")
    return not path.startswith("/") and all(p not in ("", ".", "..") for p in parts)


def parse_doc_ref(ref: str) -> tuple[str, str, str, str | None] | None:
    """``doc:`` ref → (collection, path, anchor, digest prefix or None).

    A ``~h…`` path or anchor is returned as-is; :meth:`DocumentIndex.expand`
    resolves it through the index.
    """
    m = _DOC_REF.match((ref or "").strip())
    if not m:
        return None
    path, anchor = unquote(m["p"]), unquote(m["a"])
    if not _safe_relpath(path):
        return None
    return m["c"], path, anchor, m["d"]


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def _digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# File access — every component opened without following symlinks
# ---------------------------------------------------------------------------

Fingerprint = tuple[int, int, int, int]
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)


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


def _open_parent(root: str, rel: str) -> tuple[int, str]:
    """Open the directory holding *rel*, refusing a symlink at any component.

    Returns ``(dir fd, leaf name)``; the caller closes the fd.  A symlinked
    component raises ``OSError`` (ELOOP / ENOTDIR).
    """
    parts = rel.split("/")
    fd = os.open(root, os.O_RDONLY | _DIRECTORY)
    try:
        for comp in parts[:-1]:
            nxt = os.open(comp, os.O_RDONLY | _DIRECTORY | _NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nxt
    except BaseException:
        os.close(fd)
        raise
    return fd, parts[-1]


def _lstat_in(root: str, rel: str) -> os.stat_result:
    """``lstat`` of *rel* under *root* without following any symlink.

    ``FileNotFoundError`` when it is gone; ``_OutOfScope`` when a component
    (or the file itself) is a symlink or not a regular file.
    """
    try:
        fd, leaf = _open_parent(root, rel)
    except FileNotFoundError:
        raise
    except OSError as exc:
        if exc.errno in (getattr(os, "ELOOP", 62), getattr(os, "ENOTDIR", 20)):
            raise _OutOfScope(rel) from exc
        raise
    try:
        st = os.stat(leaf, dir_fd=fd, follow_symlinks=False)
    finally:
        os.close(fd)
    if not stat.S_ISREG(st.st_mode):
        raise _OutOfScope(rel)
    return st


class _Deferred(Exception):
    """The file changed while being read or committed; try again next sync."""


class _OutOfScope(Exception):
    """A symlink (at any component) or a non-regular file: not in scope."""


@dataclass
class SyncReport:
    collections: int = 0
    present: int = 0
    pending: int = 0
    unreadable: int = 0
    too_large: int = 0
    failed: int = 0
    discovery_incomplete: bool = False
    bad_collections: list[str] = field(default_factory=list)

    def note(self) -> str:
        cols = "collection" if self.collections == 1 else "collections"
        parts = [f"documents: lexical ({self.collections} {cols}, {self.present} files)"]
        if self.pending:
            parts.append(f"{self.pending} files pending re-index")
        if self.discovery_incomplete:
            parts.append("discovery in progress — new and removed files may not be "
                         "detected yet")
        if self.unreadable:
            parts.append(f"{self.unreadable} unreadable")
        if self.too_large:
            parts.append(f"{self.too_large} too large to index")
        if self.failed:
            parts.append(f"{self.failed} failed to index (will retry)")
        if self.bad_collections:
            parts.append("unusable collection roots: " + ", ".join(self.bad_collections))
        return ", ".join(parts)


@dataclass
class _Read:
    collection: str
    path: str
    fp: Fingerprint
    data: bytes
    db_row: tuple | None    # doc_files (state, mtime, size, inode, ctime) when read


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
        # The shared connection only tells us which DB file to open.
        self._shared = db
        self._path: str | None = None
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
        # Discovery generation, resumed across recalls (see _discover).
        self._queue: deque[tuple[str, str]] | None = None
        self._gen_seen: dict[tuple[str, str], Fingerprint] = {}
        self._gen_failed: set[str] = set()
        self._last_complete: dict[tuple[str, str], Fingerprint] | None = None
        self._gen_failed_done: set[str] = set(self._roots)

    @property
    def configured(self) -> bool:
        return bool(self._config)

    @asynccontextmanager
    async def _connect(self):
        if self._path is None:
            cur = await self._shared.execute("PRAGMA database_list")
            rows = await cur.fetchall()
            self._path = next(r[2] for r in rows if r[1] == "main")
        async with aiosqlite.connect(self._path, isolation_level=None) as conn:
            await conn.execute("PRAGMA busy_timeout = 5000")
            if not self._ready:
                await conn.executescript(_DDL)
                self._ready = True
            yield conn

    # -- sync ------------------------------------------------------------

    async def sync(self) -> SyncReport:
        """Bring the index up to date within this call's budget."""
        async with self._connect() as conn:
            return await self._locked_sync(conn)

    async def _locked_sync(self, conn) -> SyncReport:
        async with _lock_for(self._path or ""):
            return await self._sync(conn)

    async def _sync(self, conn) -> SyncReport:
        report = SyncReport(collections=len(self._roots), bad_collections=list(self._bad))
        await self._check_roots(conn)
        seen, complete = self._discover(report)
        names = list(self._roots)
        known = await self._known(conn, names)

        new = sorted(k for k in seen if k not in known or known[k][0] != "present")
        changed = sorted(
            (k for k in seen if k in known and known[k][0] == "present"
             and tuple(known[k][1:5]) != seen[k]),
            key=lambda k: known[k][6] or "",
        )
        # Move pairing must be unique on this side too: a never-seen file
        # is a move target only when no other never-seen file has its size.
        new_sizes: dict[int, int] = {}
        for k in new:
            if k not in known:
                new_sizes[seen[k][1]] = new_sizes.get(seen[k][1], 0) + 1

        # Gone files are hidden first — before any read — so a deletion is
        # never visible-while-pending.  Only a completed discovery
        # generation can tell that a file is gone.
        if complete is not None:
            for key, row in known.items():
                if row[0] == "present" and key not in complete and key[0] not in self._gen_failed_done:
                    try:
                        _lstat_in(self._roots[key[0]], key[1])
                        continue  # still there (e.g. skipped by discovery limits)
                    except (FileNotFoundError, _OutOfScope):
                        pass
                    except OSError:
                        report.unreadable += 1
                        continue
                    if not await self._commit_removed(conn, key, row):
                        report.pending += 1

        # Read within budget: new files first, then the longest-unchecked.
        work = new + changed
        deadline = time.monotonic() + self._seconds
        spent = 0
        done = 0
        for i, key in enumerate(work):
            size = seen[key][1]
            if size > self._max_file_bytes:
                report.too_large += 1
                continue
            if (done >= self._max_files or time.monotonic() >= deadline
                    or (done and spent + size > self._max_bytes)):
                report.pending += sum(1 for k in work[i:] if seen[k][1] <= self._max_file_bytes)
                break
            try:
                data = self._read_verified(key, seen[key])
            except (_Deferred, FileNotFoundError):
                report.pending += 1
                continue
            except (_OutOfScope, OSError):
                report.unreadable += 1
                continue
            done += 1
            spent += len(data)
            row = known.get(key)
            read = _Read(key[0], key[1], seen[key], data,
                         tuple(row[:5]) if row is not None else None)
            try:
                ok = await self._commit_file(conn, read, new_sizes)
            except Exception as exc:  # one bad file never fails recall
                logger.warning("[documents] indexing %s/%s failed: %s", key[0], key[1], exc)
                report.failed += 1
                continue
            if not ok:
                report.pending += 1

        cur = await conn.execute(
            "SELECT COUNT(*) FROM doc_files WHERE state='present' AND collection IN "
            f"({','.join('?' * len(names))})" if names else "SELECT 0",
            names,
        )
        report.present = (await cur.fetchone())[0]
        report.discovery_incomplete = self._queue is not None
        return report

    async def _known(self, conn, names: list[str]) -> dict[tuple[str, str], tuple]:
        if not names:
            return {}
        cur = await conn.execute(
            "SELECT collection, path, state, mtime_ns, size, inode, ctime_ns, "
            f"file_digest, checked_at FROM doc_files WHERE collection IN "
            f"({','.join('?' * len(names))})",
            names,
        )
        return {(r[0], r[1]): r[2:] for r in await cur.fetchall()}

    async def _check_roots(self, conn) -> None:
        """A collection whose root moved is out of scope as a whole."""
        for name, root in self._roots.items():
            cur = await conn.execute("SELECT root FROM doc_collections WHERE name=?", (name,))
            row = await cur.fetchone()
            if row and row[0] == root:
                continue
            async with _transaction(conn):
                if row:
                    # Everything seen under the old root moves under a retired
                    # name: kept (nothing is erased), never shown again, and
                    # never mistaken for history of a same-named file under
                    # the new root.  ``~`` cannot occur in a configured name.
                    retired = f"{name}~retired~{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%f')}"
                    await conn.execute("DELETE FROM doc_current WHERE collection=?", (name,))
                    await conn.execute(
                        "UPDATE doc_files SET collection=?, state='removed', moved_to=NULL "
                        "WHERE collection=?", (retired, name))
                    await conn.execute(
                        "UPDATE doc_versions SET collection=? WHERE collection=?",
                        (retired, name))
                await conn.execute(
                    "INSERT INTO doc_collections(name, root) VALUES (?, ?) "
                    "ON CONFLICT(name) DO UPDATE SET root=excluded.root", (name, root))
            if row:
                self._queue = None   # rediscover from scratch
                self._last_complete = None

    def _discover(self, report: SyncReport):
        """One bounded step of a resumable walk over every collection.

        Returns ``(seen, complete)``: *seen* holds every file found so far in
        the current generation plus, for files not reached yet, the last
        completed generation (so a partial walk never looks like deletion);
        *complete* is the finished generation's file set when this step
        completed one, else ``None``.  The queue is breadth-first across all
        collections, so none starves the others.
        """
        if self._queue is None:
            self._queue = deque((name, root) for name, root in self._roots.items())
            self._gen_seen = {}
            self._gen_failed = set()
        budget = self._max_entries
        while self._queue and budget > 0:
            name, folder = self._queue.popleft()
            try:
                with os.scandir(folder) as it:
                    for entry in it:
                        budget -= 1
                        if entry.name.startswith(".") or entry.is_symlink():
                            continue
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                self._queue.append((name, entry.path))
                            elif (entry.is_file(follow_symlinks=False)
                                  and entry.name.lower().endswith(".md")):
                                rel = os.path.relpath(entry.path, self._roots[name])
                                self._gen_seen[(name, rel.replace(os.sep, "/"))] = \
                                    _fingerprint(entry.stat(follow_symlinks=False))
                        except OSError:
                            report.unreadable += 1
            except OSError:
                report.unreadable += 1
                self._gen_failed.add(name)
        seen = dict(self._last_complete or {})
        seen.update(self._gen_seen)
        if self._queue:
            self._gen_failed_done = set(self._roots)   # nothing is judged gone
            return seen, None
        complete = dict(self._gen_seen)
        self._gen_failed_done = set(self._gen_failed)
        self._last_complete = complete
        self._queue = None
        return complete, complete

    def _read_verified(self, key: tuple[str, str], fp: Fingerprint) -> bytes:
        fd_dir, leaf = _open_parent(self._roots[key[0]], key[1])
        try:
            fd = os.open(leaf, os.O_RDONLY | _NOFOLLOW, dir_fd=fd_dir)
        finally:
            os.close(fd_dir)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise _OutOfScope(key[1])
            if _fingerprint(st) != fp:
                raise _Deferred(key[1])
            data = _read_bytes(fd, self._max_file_bytes + 1)
            if _fingerprint(os.fstat(fd)) != fp:
                raise _Deferred(key[1])
        finally:
            os.close(fd)
        return data

    def _still(self, collection: str, path: str, fp: Fingerprint) -> bool:
        try:
            return _fingerprint(_lstat_in(self._roots[collection], path)) == fp
        except (OSError, _OutOfScope):
            return False

    @staticmethod
    async def _db_row(conn, collection: str, path: str) -> tuple | None:
        cur = await conn.execute(
            "SELECT state, mtime_ns, size, inode, ctime_ns FROM doc_files "
            "WHERE collection=? AND path=?", (collection, path))
        row = await cur.fetchone()
        return tuple(row) if row else None

    async def _commit_file(self, conn, r: _Read, new_sizes: dict[int, int]) -> bool:
        """Index one file read, or record its move; False when superseded."""
        try:
            return await self._commit_file_tx(conn, r, new_sizes)
        except _Abort:
            return False

    async def _commit_file_tx(self, conn, r: _Read, new_sizes: dict[int, int]) -> bool:
        now = datetime.now(UTC).isoformat()
        digest = _digest_bytes(r.data)
        async with _transaction(conn):
            # Inside the write transaction: the file is still what we read,
            # and no other writer recorded a different state meanwhile.
            if not self._still(r.collection, r.path, r.fp):
                raise _Abort
            if await self._db_row(conn, r.collection, r.path) != r.db_row:
                raise _Abort
            if (r.db_row is None and new_sizes.get(r.fp[1]) == 1
                    and await self._try_move(conn, r, digest, now)):
                return True
            current: list[tuple[str, int, int]] = []
            for sec in split_sections(r.data.decode("utf-8", "replace")):
                vid = await self._upsert_version(conn, r.collection, r.path, sec, now)
                current.append((sec.anchor, vid, sec.ord))
            await conn.execute("DELETE FROM doc_current WHERE collection=? AND path=?",
                               (r.collection, r.path))
            await conn.executemany(
                "INSERT INTO doc_current(collection, path, anchor, version_id, ord) "
                "VALUES (?, ?, ?, ?, ?)",
                [(r.collection, r.path, a, vid, o) for a, vid, o in current],
            )
            await self._put_file(conn, r, digest, now)
        return True

    async def _try_move(self, conn, r: _Read, digest: str, now: str) -> bool:
        """A never-seen file is a move when exactly one removed file in scope
        had its inode, size and whole-file digest, and is still gone."""
        names = list(self._roots)
        cur = await conn.execute(
            "SELECT collection, path FROM doc_files WHERE state='removed' AND inode=? "
            "AND size=? AND file_digest=? AND collection IN "
            f"({','.join('?' * len(names))})",
            (r.fp[2], r.fp[1], digest, *names),
        )
        olds = await cur.fetchall()
        if len(olds) != 1:
            return False
        old = tuple(olds[0])
        try:
            _lstat_in(self._roots[old[0]], old[1])
            return False          # the old path exists again: not a move
        except (FileNotFoundError, _OutOfScope):
            pass
        except OSError:
            return False
        target = f"{r.collection}/{r.path}"
        for table in ("doc_versions", "doc_current"):
            await conn.execute(
                f"UPDATE {table} SET collection=?, path=? WHERE collection=? AND path=?",
                (r.collection, r.path, old[0], old[1]),
            )
        # The old path's last current sections become current at the new path.
        cur = await conn.execute(
            "SELECT COUNT(*) FROM doc_current WHERE collection=? AND path=?",
            (r.collection, r.path))
        if not (await cur.fetchone())[0]:
            for sec in split_sections(r.data.decode("utf-8", "replace")):
                vid = await self._upsert_version(conn, r.collection, r.path, sec, now)
                await conn.execute(
                    "INSERT INTO doc_current(collection, path, anchor, version_id, ord) "
                    "VALUES (?, ?, ?, ?, ?)", (r.collection, r.path, sec.anchor, vid, sec.ord))
        await conn.execute(
            "UPDATE doc_files SET state='moved', moved_to=?, checked_at=? "
            "WHERE collection=? AND path=?", (target, now, old[0], old[1]))
        await conn.execute("UPDATE doc_files SET moved_to=? WHERE moved_to=?",
                           (target, f"{old[0]}/{old[1]}"))
        await self._put_file(conn, r, digest, now)
        return True

    async def _upsert_version(self, conn, collection: str, path: str, sec: Section,
                              now: str) -> int:
        digest = _digest(sec.content)
        cur = await conn.execute(
            "INSERT OR IGNORE INTO doc_versions(collection, path, anchor, digest, title, "
            "content, first_seen, last_seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (collection, path, sec.anchor, digest, sec.title, sec.content, now, now),
        )
        if cur.rowcount == 1:
            vid = cur.lastrowid
            await conn.execute("INSERT INTO doc_fts(rowid, title, content) VALUES (?, ?, ?)",
                               (vid, sec.title, sec.content))
            await conn.execute("INSERT INTO doc_terms(rowid, terms) VALUES (?, ?)",
                               (vid, bigram_projection(sec.content)))
            return vid
        cur = await conn.execute(
            "SELECT id FROM doc_versions WHERE collection=? AND path=? AND anchor=? AND digest=?",
            (collection, path, sec.anchor, digest),
        )
        vid = (await cur.fetchone())[0]
        await conn.execute("UPDATE doc_versions SET last_seen=? WHERE id=?", (now, vid))
        return vid

    @staticmethod
    async def _put_file(conn, r: _Read, digest: str, now: str) -> None:
        mtime, size, ino, ctime = r.fp
        await conn.execute(
            "INSERT INTO doc_files(collection, path, state, mtime_ns, size, inode, ctime_ns, "
            "file_digest, moved_to, checked_at) VALUES (?, ?, 'present', ?, ?, ?, ?, ?, NULL, ?) "
            "ON CONFLICT(collection, path) DO UPDATE SET state='present', "
            "mtime_ns=excluded.mtime_ns, size=excluded.size, inode=excluded.inode, "
            "ctime_ns=excluded.ctime_ns, file_digest=excluded.file_digest, moved_to=NULL, "
            "checked_at=excluded.checked_at",
            (r.collection, r.path, mtime, size, ino, ctime, digest, now),
        )

    async def _commit_removed(self, conn, key: tuple[str, str], row: tuple) -> bool:
        try:
            async with _transaction(conn):
                await self._removed_tx(conn, key, row)
        except _Abort:
            return False
        return True

    async def _removed_tx(self, conn, key: tuple[str, str], row: tuple) -> None:
        if await self._db_row(conn, *key) != tuple(row[:5]):
            raise _Abort
        await conn.execute(
            "UPDATE doc_files SET state='removed', moved_to=NULL, checked_at=? "
            "WHERE collection=? AND path=?",
            (datetime.now(UTC).isoformat(), key[0], key[1]))
        await conn.execute("DELETE FROM doc_current WHERE collection=? AND path=?", key)

    # -- search ----------------------------------------------------------

    async def search(self, terms: list[str]) -> tuple[list, str]:
        """Documents route: one ranked list, at most two slots per section."""
        from loom.core.memory.search import EXCERPT_CHARS, MemorySearchResult, _excerpt

        if not self.configured:
            return [], "documents: not configured"
        async with self._connect() as conn:
            report = await self._locked_sync(conn)
            if not self._roots:
                return [], report.note()
            rows = await self._match(conn, terms)

        groups: dict[tuple, list] = {}
        for row in rows:
            groups.setdefault((row[1], row[2], row[3]), []).append(row)
        picked: list = []
        sections = 0
        for group in groups.values():
            if sections >= ROUTE_SECTIONS:
                break
            sections += 1
            best = group[0]
            picked.append(best)
            if best[6] is not None and best[0] != best[6]:
                cur_row = next((g for g in group if g[0] == best[6]), None)
                if cur_row is not None:
                    picked.append(cur_row)

        results = []
        for vid, coll, path, anchor, digest, content, cur_id, cur_digest in picked:
            ref = format_doc_ref(coll, path, anchor, digest)
            text = _redact_secrets(content) or ""
            results.append(MemorySearchResult(
                type="document", key=ref,
                value=_excerpt(text, terms, EXCERPT_CHARS),
                score=0.0,
                metadata={"method": "document",
                          "status": _status(coll, path, anchor, vid, cur_id, cur_digest)},
                updated_at="", ref=ref,
            ))
        note = report.note()
        if len(rows) >= _MATCH_ROWS or len(groups) > ROUTE_SECTIONS:
            note += (f", more matching sections beyond the {ROUTE_SECTIONS}-section "
                     "budget (add terms to narrow)")
        return results, note

    async def _match(self, conn, terms: list[str]) -> list:
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
            # on the live corpus for one identifier).  A section's heading is
            # its name: a title hit outranks the same words deep in a long
            # body (bm25 alone favours short sections).
            sql = ("WITH hits AS MATERIALIZED (SELECT rowid AS id, "
                   f"bm25(doc_fts, {_TITLE_WEIGHT}, 1.0) AS rank FROM doc_fts "
                   "WHERE doc_fts MATCH ?) "
                   + select + "JOIN hits ON hits.id=v.id WHERE "
                   + " AND ".join(where) + " ORDER BY hits.rank LIMIT ?")
            args = [_phrase(long_terms), *params, _MATCH_ROWS]
        else:
            sql = select + "WHERE " + " AND ".join(where) + " ORDER BY v.id DESC LIMIT ?"
            args = [*params, _MATCH_ROWS]
        cur = await conn.execute(sql, args)
        return await cur.fetchall()

    # -- expansion -------------------------------------------------------

    async def expand(self, ref: str):
        """Identity read of a ``doc:`` ref (spec 61 r3 §9.2)."""
        from loom.core.memory.search import RefExpansion

        parsed = parse_doc_ref(ref)
        if parsed is None:
            return RefExpansion(
                f"Not a valid doc: ref: {ref!r}. Expected "
                "doc:{collection}/{path}#{section}[@hash] as shown in recall results.",
                found=False)
        if parsed[0] not in self._roots:
            return RefExpansion(_gone(*parsed), found=False)
        async with self._connect() as conn:
            await self._locked_sync(conn)
            return await self._expand(conn, parsed)

    async def _expand(self, conn, parsed):
        from loom.core.memory.search import RefExpansion, _cut

        coll, path, anchor, want = parsed
        gone = RefExpansion(_gone(*parsed), found=False)
        if _HASHED.match(path):
            cur = await conn.execute(
                "SELECT DISTINCT path FROM doc_files WHERE collection=?", (coll,))
            path = next((p for (p,) in await cur.fetchall() if _hashed(p) == path), path)

        notes: list[str] = []
        start = (coll, path)
        for _ in range(10):  # follow recorded moves
            cur = await conn.execute(
                "SELECT state, moved_to FROM doc_files WHERE collection=? AND path=?",
                (coll, path))
            row = await cur.fetchone()
            if row is None or row[0] == "removed":
                return gone
            if row[0] == "present":
                break
            new_coll, _, new_path = (row[1] or "").partition("/")
            if new_coll not in self._roots or not new_path:
                return gone
            coll, path = new_coll, new_path
        else:
            return gone
        try:
            _lstat_in(self._roots[coll], path)   # re-validated at read time
        except (OSError, _OutOfScope):
            return gone

        if _HASHED.match(anchor):
            cur = await conn.execute(
                "SELECT DISTINCT anchor FROM doc_versions WHERE collection=? AND path=?",
                (coll, path))
            anchor = next((a for (a,) in await cur.fetchall() if _hashed(a) == anchor), anchor)
        cur = await conn.execute(
            "SELECT v.id, v.digest, v.content, v.first_seen, c.ord FROM doc_current c "
            "JOIN doc_versions v ON v.id=c.version_id "
            "WHERE c.collection=? AND c.path=? AND c.anchor=?", (coll, path, anchor))
        current = await cur.fetchone()
        target = current
        if want:
            cur = await conn.execute(
                "SELECT id, digest, content, first_seen FROM doc_versions "
                "WHERE collection=? AND path=? AND anchor=? AND digest LIKE ?",
                (coll, path, anchor, want + "%"))
            found = await cur.fetchall()
            if len(found) > 1:
                return RefExpansion(
                    f"{format_doc_ref(coll, path, anchor, want)} matches {len(found)} "
                    "stored versions of this section; use a longer hash.", found=False)
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

        if (coll, path) != start:
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
        if _repeated(anchor) and not is_current:
            notes.append("repeated heading — the current section at this position may "
                         "not be the same section")

        lines = [head] + [f"({n})" for n in notes]
        neighbours: list[tuple[str, str]] = []
        if is_current:
            my_ord = current[4]
            cur = await conn.execute(
                "SELECT c.anchor, v.digest, v.content, c.ord FROM doc_current c "
                "JOIN doc_versions v ON v.id=c.version_id WHERE c.collection=? AND c.path=? "
                "AND c.ord IN (?, ?) ORDER BY c.ord",
                (coll, path, my_ord - 1, my_ord + 1))
            for n_anchor, n_digest, n_content, n_ord in await cur.fetchall():
                label = "previous" if n_ord < my_ord else "next"
                neighbours.append((
                    f"— {label} section {format_doc_ref(coll, path, n_anchor, n_digest)} "
                    "(current):",
                    _redact_secrets(n_content) or ""))
        used = sum(len(l) + 1 for l in lines) + 1
        room = EXPAND_CHARS - used - _NEIGHBOUR_RESERVE * len(neighbours)
        out = lines + [_cut(_redact_secrets(target[2]) or "", max(200, room))]
        for label, text in neighbours:
            out += ["", label, _cut(text, max(100, _NEIGHBOUR_RESERVE - len(label) - 2))]
        return RefExpansion("\n".join(out)[:EXPAND_CHARS], found=True)


class _Abort(Exception):
    """Roll the current file's transaction back; it is retried next sync."""


@asynccontextmanager
async def _transaction(conn):
    """``BEGIN IMMEDIATE`` … ``COMMIT`` on this module's own connection.

    Any exception — including cancellation — rolls back.  ``_Abort`` is a
    planned rollback: the caller's function returns False.
    """
    await conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        await conn.execute("ROLLBACK")
        raise
    else:
        await conn.execute("COMMIT")


def _gone(coll: str, path: str, anchor: str, want: str | None) -> str:
    return (f"{format_doc_ref(coll, path, anchor, want)} — deleted or moved out of "
            "scope; its text is no longer available.")


def _repeated(anchor: str) -> bool:
    return "~" in anchor and anchor.rpartition("~")[2].isdigit()


def _status(coll, path, anchor, vid, cur_id, cur_digest) -> str:
    if cur_id is None:
        return "old version — section no longer in the file"
    if cur_id == vid:
        return "current"
    status = f"old version — current: {format_doc_ref(coll, path, anchor, cur_digest)}"
    if _repeated(anchor):
        status += " (repeated heading — may not be the same section)"
    return status


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
