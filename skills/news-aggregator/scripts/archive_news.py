"""Archive dated briefings older than N days from news/current/ to news/archive/.

Design invariants (#708 S4b):
- Move only, NEVER delete any briefing or overwrite an existing archive folder.
- Workspace root is derived from this script's location (no --workspace/--src/--dst
  CLI flags) so an unattended exec prefix grant cannot redirect moves elsewhere.
- Never follow symlinks: parent directories are pinned via O_DIRECTORY | O_NOFOLLOW
  fds verified with samestat; symlinked roots abort with ValueError and symlinked
  date entries (or entries containing symlinks) are skipped.
- Atomic no-replace rename (macOS renameatx_np with RENAME_EXCL; Linux renameat2
  with RENAME_NOREPLACE) prevents overwriting concurrent files or empty dirs.
- Keeps the active ``news/current/`` prefix well below ``skill_allows._SCAN_LIMIT``
  (2,000 entries) and isolated from Finder ``.DS_Store`` files in ``news/`` or
  ``news/archive/`` without relaxing any security check.
"""

import argparse
import ctypes
import errno
import json
import os
import re
import stat
import sys
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path

_DATE_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DIR_OPEN_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
_RENAME_EXCL = 0x00000004  # macOS <stdio.h> RENAME_EXCL
_RENAME_NOREPLACE = 1      # Linux <linux/fs.h> RENAME_NOREPLACE


@dataclass
class ArchiveResult:
    cutoff: str
    keep_days: int
    dry_run: bool
    moved: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def _check_workspace_dir(path: Path, ws_real: Path, label: str) -> None:
    """Verify *path* is not a symlink and stays strictly inside *ws_real*."""
    if path.is_symlink():
        raise ValueError(f"{label} must not be a symlink: {path}")
    resolved = path.resolve()
    if not resolved.is_relative_to(ws_real) or resolved == ws_real:
        raise ValueError(f"{label} escapes workspace: {path}")
    if path.exists() and not path.is_dir():
        raise ValueError(f"{label} must be a directory: {path}")


def _open_dir_nofollow(parent_fd: int, name: str, expected_path: Path, label: str) -> int:
    """Open child directory *name* relative to *parent_fd* without following symlinks,
    verifying its inode matches *expected_path* via samestat."""
    try:
        fd = os.open(name, _DIR_OPEN_FLAGS, dir_fd=parent_fd)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise ValueError(f"{label} must not be a symlink: {expected_path}") from exc
        raise
    try:
        fd_st = os.fstat(fd)
        path_st = os.stat(expected_path, follow_symlinks=False)
        if not stat.S_ISDIR(fd_st.st_mode) or not os.path.samestat(fd_st, path_st):
            raise ValueError(f"{label} changed or resolved outside expected path: {expected_path}")
    except Exception:
        os.close(fd)
        raise
    return fd


def _rename_no_replace(src_fd: int, dst_fd: int, name: str) -> None:
    """Atomically rename *name* from *src_fd* to *dst_fd* without replacing any target
    (including an empty directory)."""
    if "/" in name or name in ("", ".", ".."):
        raise ValueError(f"invalid entry name: {name!r}")
    name_b = os.fsencode(name)

    if sys.platform == "darwin":
        libc = ctypes.CDLL(None, use_errno=True)
        fn = libc.renameatx_np
        fn.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        fn.restype = ctypes.c_int
        ctypes.set_errno(0)
        rc = fn(src_fd, name_b, dst_fd, name_b, _RENAME_EXCL)
        if rc != 0:
            err = ctypes.get_errno()
            raise OSError(err, os.strerror(err), name)
        return

    if sys.platform.startswith("linux"):
        libc = ctypes.CDLL(None, use_errno=True)
        fn = getattr(libc, "renameat2", None)
        if fn is not None:
            fn.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
            fn.restype = ctypes.c_int
            ctypes.set_errno(0)
            rc = fn(src_fd, name_b, dst_fd, name_b, _RENAME_NOREPLACE)
            if rc == 0:
                return
            err = ctypes.get_errno()
            if err not in (errno.ENOSYS, errno.EINVAL):
                raise OSError(err, os.strerror(err), name)

    # Portable fallback when kernel no-replace rename is unavailable.
    try:
        os.stat(name, dir_fd=dst_fd, follow_symlinks=False)
    except FileNotFoundError:
        os.rename(name, name, src_dir_fd=src_fd, dst_dir_fd=dst_fd)
    else:
        raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), name)


def _contains_symlink(directory: Path) -> bool:
    for root, dirs, files in os.walk(directory, followlinks=False):
        for name in dirs + files:
            if (Path(root) / name).is_symlink():
                return True
    return False


def _parse_date_dir(name: str) -> date | None:
    if not _DATE_DIR_RE.match(name):
        return None
    try:
        return date.fromisoformat(name)
    except ValueError:
        return None


def archive_old_news(
    workspace: Path,
    *,
    keep_days: int = 30,
    today: date | None = None,
    dry_run: bool = False,
) -> ArchiveResult:
    """Move ``news/current/YYYY-MM-DD`` strictly older than *keep_days* to ``news/archive/``."""
    if isinstance(keep_days, bool) or not isinstance(keep_days, int) or keep_days < 1:
        raise ValueError(f"keep_days must be an integer >= 1, got {keep_days!r}")

    ws_real = workspace.resolve()
    news_root = ws_real / "news"
    current_dir = news_root / "current"
    archive_dir = news_root / "archive"

    _check_workspace_dir(news_root, ws_real, "news/")
    _check_workspace_dir(current_dir, ws_real, "news/current/")
    _check_workspace_dir(archive_dir, ws_real, "news/archive/")

    ref_today = today or date.today()
    cutoff = ref_today - timedelta(days=keep_days)
    result = ArchiveResult(
        cutoff=cutoff.isoformat(),
        keep_days=keep_days,
        dry_run=dry_run,
    )

    if not current_dir.exists():
        return result

    ws_fd = os.open(ws_real, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    news_fd: int | None = None
    current_fd: int | None = None
    try:
        news_fd = _open_dir_nofollow(ws_fd, "news", news_root, "news/")
        current_fd = _open_dir_nofollow(news_fd, "current", current_dir, "news/current/")

        for name in sorted(os.listdir(current_fd)):
            try:
                entry_st = os.stat(name, dir_fd=current_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(entry_st.st_mode):
                result.skipped.append(f"{name} (symlink)")
                continue
            if not stat.S_ISDIR(entry_st.st_mode):
                result.skipped.append(f"{name} (not a directory)")
                continue
            entry_date = _parse_date_dir(name)
            if entry_date is None:
                result.skipped.append(f"{name} (not YYYY-MM-DD)")
                continue
            if entry_date >= cutoff:
                result.kept.append(name)
                continue
            if _contains_symlink(current_dir / name):
                result.skipped.append(f"{name} (contains symlink)")
                continue

            if dry_run:
                if archive_dir.exists() or archive_dir.is_symlink():
                    archive_fd = _open_dir_nofollow(news_fd, "archive", archive_dir, "news/archive/")
                    try:
                        os.stat(name, dir_fd=archive_fd, follow_symlinks=False)
                    except FileNotFoundError:
                        result.moved.append(name)
                    else:
                        result.skipped.append(f"{name} (archive target already exists)")
                    finally:
                        os.close(archive_fd)
                else:
                    result.moved.append(name)
                continue

            # Ensure news/ and news/current/ have not been swapped and open
            # news/archive/ fresh with O_NOFOLLOW right before atomic rename.
            if not os.path.samestat(
                os.fstat(news_fd), os.stat("news", dir_fd=ws_fd, follow_symlinks=False)
            ):
                raise ValueError(f"news/ changed during archive: {news_root}")
            if not os.path.samestat(
                os.fstat(current_fd), os.stat("current", dir_fd=news_fd, follow_symlinks=False)
            ):
                raise ValueError(f"news/current/ changed during archive: {current_dir}")

            try:
                os.mkdir("archive", dir_fd=news_fd)
            except FileExistsError:
                pass
            archive_fd = _open_dir_nofollow(news_fd, "archive", archive_dir, "news/archive/")
            try:
                _rename_no_replace(current_fd, archive_fd, name)
            except FileExistsError:
                result.skipped.append(f"{name} (archive target already exists)")
            except OSError as exc:
                if exc.errno in (errno.EEXIST, errno.ENOTEMPTY):
                    result.skipped.append(f"{name} (archive target already exists)")
                elif exc.errno == errno.ELOOP:
                    result.skipped.append(f"{name} (symlink race refused)")
                else:
                    result.skipped.append(f"{name} (rename refused: {exc.strerror or exc})")
            else:
                result.moved.append(name)
            finally:
                os.close(archive_fd)
    finally:
        if current_fd is not None:
            os.close(current_fd)
        if news_fd is not None:
            os.close(news_fd)
        os.close(ws_fd)

    return result


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid integer: {value!r}") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("--keep-days must be >= 1")
    return parsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Move dated briefing folders older than --keep-days from "
            "news/current/ to news/archive/ (move-only, never deletes)."
        ),
    )
    parser.add_argument(
        "--keep-days",
        type=_positive_int,
        default=30,
        help="Number of recent days to keep in news/current/ (default: 30, min: 1).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report which folders would be archived without moving anything.",
    )
    args = parser.parse_args(argv)

    # Workspace is fixed to the parent tree of skills/news-aggregator/scripts/archive_news.py.
    workspace = Path(__file__).resolve().parents[3]
    try:
        result = archive_old_news(workspace, keep_days=args.keep_days, dry_run=args.dry_run)
    except (ValueError, OSError) as exc:
        print(f"archive_news error: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(asdict(result), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
