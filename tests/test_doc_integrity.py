"""Doc Integrity Gate — Issue #313 Layer 1 + Layer 3.

Layer 1 — Reference Check
    Walks `doc/*.md` plus `README.md`. Extracts paths matching
    `loom/.../*.{py,md,toml,json,sql,yaml,yml}` and verifies each one resolves
    to a file in the repo.

    Fenced code blocks are scanned by default — they routinely carry real
    state claims (`# loom/foo/bar.py` headers above class bodies). For true
    tutorial / illustrative snippets that intentionally name non-existent
    paths, place `<!-- doc-integrity:ignore-block -->` on the line
    immediately preceding the fence opener; that whole block is then exempt.

Layer 3 — Version Sync
    `pyproject.toml` is the source of truth. The most recent changelog row in
    `README.md` and the `<!-- loom-version: ... -->` marker in
    `doc/00-總覽.md` must agree with it.

Opt-out markers
    File-level:  `<!-- doc-integrity:skip -->` anywhere in a markdown file
                 exempts the whole file. Use for audit / gap-analysis docs
                 that intentionally name non-existent paths.
    Block-level: `<!-- doc-integrity:ignore-block -->` on the line immediately
                 preceding a ```fence``` opener exempts that whole fenced
                 block. Use for tutorial / illustrative example code.
    Line-level:  `<!-- doc-integrity:ignore -->` appended to a single line
                 exempts that one reference. Use for retirement-list rows
                 that intentionally name deleted files.

Layer 2 (`已實作` contract tests) is deliberately out of scope here — those
go into per-feature `tests/test_doc_contract_*.py` modules as drift is
discovered.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

PATH_RE = re.compile(
    r"(?<![.~])\bloom/[A-Za-z0-9_./-]+?\.(?:py|md|toml|json|sql|yaml|yml)\b"
)
# Substring directives. The trailing `-->` is intentionally not required so
# authors can append free-form rationale inside the same HTML comment, e.g.
# `<!-- doc-integrity:ignore-block — see issue #390 -->`.
SKIP_FILE_MARK = "doc-integrity:skip"
SKIP_LINE_MARK = "doc-integrity:ignore"
SKIP_BLOCK_MARK = "doc-integrity:ignore-block"

VERSION_RE = re.compile(r'^version\s*=\s*"([^"]+)"', re.M)
README_LATEST_RE = re.compile(r"^\|\s*\*\*v(\d+(?:\.\d+){2,3})\*\*\s*\|", re.M)
DOC00_VERSION_RE = re.compile(
    r"<!--\s*loom-version:\s*v?(\d+(?:\.\d+){2,3})\s*-->"
)


@dataclass
class Violation:
    file: str
    line: int
    detail: str


def _layer1_targets() -> list[Path]:
    targets = sorted((REPO_ROOT / "doc").glob("*.md"))
    readme = REPO_ROOT / "README.md"
    if readme.exists():
        targets.append(readme)
    return targets


def _check_layer1() -> list[Violation]:
    violations: list[Violation] = []
    for md_path in _layer1_targets():
        text = md_path.read_text(encoding="utf-8")
        if SKIP_FILE_MARK in text:
            continue

        in_fence = False
        block_ignored = False
        prev_nonblank = ""
        for i, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("```"):
                if not in_fence:
                    block_ignored = SKIP_BLOCK_MARK in prev_nonblank
                    in_fence = True
                else:
                    in_fence = False
                    block_ignored = False
                # The fence delimiter line itself carries no path claims.
                if stripped:
                    prev_nonblank = stripped
                continue
            if in_fence and block_ignored:
                if stripped:
                    prev_nonblank = stripped
                continue
            if SKIP_LINE_MARK in line:
                if stripped:
                    prev_nonblank = stripped
                continue
            for m in PATH_RE.finditer(line):
                rel = m.group(0)
                if not (REPO_ROOT / rel).exists():
                    violations.append(
                        Violation(
                            file=str(md_path.relative_to(REPO_ROOT)),
                            line=i,
                            detail=f"missing path: {rel}",
                        )
                    )
            if stripped:
                prev_nonblank = stripped
    return violations


def _check_layer3() -> list[Violation]:
    violations: list[Violation] = []
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m = VERSION_RE.search(pyproject)
    if not m:
        return [Violation("pyproject.toml", 0, "no version line found")]
    truth = m.group(1)

    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    m2 = README_LATEST_RE.search(readme)
    if not m2:
        violations.append(
            Violation("README.md", 0, "no `| **vX.Y.Z** |` changelog row found")
        )
    elif m2.group(1) != truth:
        ln = readme[: m2.start()].count("\n") + 1
        violations.append(
            Violation(
                "README.md",
                ln,
                f"latest changelog v{m2.group(1)} != pyproject {truth}",
            )
        )

    doc00 = (REPO_ROOT / "doc/00-總覽.md").read_text(encoding="utf-8")
    m3 = DOC00_VERSION_RE.search(doc00)
    if not m3:
        violations.append(
            Violation(
                "doc/00-總覽.md",
                0,
                f"missing `<!-- loom-version: {truth} -->` marker",
            )
        )
    elif m3.group(1) != truth:
        ln = doc00[: m3.start()].count("\n") + 1
        violations.append(
            Violation(
                "doc/00-總覽.md",
                ln,
                f"version marker v{m3.group(1)} != pyproject {truth}",
            )
        )
    return violations


def _format(violations: list[Violation]) -> str:
    return "\n".join(f"  {v.file}:{v.line}  {v.detail}" for v in violations)


def test_layer1_doc_paths_exist():
    violations = _check_layer1()
    assert not violations, (
        f"{len(violations)} missing path reference(s) in docs:\n"
        f"{_format(violations)}"
    )


def test_layer3_version_sync():
    violations = _check_layer3()
    assert not violations, (
        f"{len(violations)} version sync issue(s):\n{_format(violations)}"
    )
