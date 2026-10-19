"""
Big dream — a one-off, whole-corpus memory tidy-up (#613 E, docs/designs/57 §6).

The daily dreams work a few neighbourhoods at a time; a corpus that grew for
months without them needs one pass over everything first:

1. **census** — ``take_census`` before anything changes;
2. **converge** — ``run_convergent_dream`` over the whole corpus with no
   cluster caps (near-duplicates, cos ≥ 0.85), after its own backup;
3. **distill** — ``run_distill_dream`` passes of ``distill_batch``
   neighbourhoods each, until no eligible neighbourhood is left, a pass makes
   no progress (nothing written or rejected — only deferred / failed), or
   ``max_passes``. Rejections are suppressed across passes, so each pass
   moves on; every executing pass takes its own backup;
4. **census** again, and a before / after summary.

Each stage writes only when its ``execute_*`` flag is set (the CLI passes the
owner's loom.toml gates); otherwise it runs read-only and distillation runs a
single pass, since nothing gets consumed. Every pass holds
``memory_rewrite_lock`` and every report goes to the dreams journal, as the
daily dreams' do. Entry point for people and agents: ``loom memory big-dream``
(see ``skills/memory_big_dream/SKILL.md``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from loom.core.cognition.consolidation import LLMFn, memory_rewrite_lock

if TYPE_CHECKING:
    import aiosqlite

    from loom.core.memory.census import CorpusCensus
    from loom.core.memory.semantic import SemanticMemory

logger = logging.getLogger(__name__)

DEFAULT_DISTILL_BATCH = 20
DEFAULT_MAX_PASSES = 50
#: "No cap" for the convergent dream: the whole backlog in one pass.
_UNCAPPED = 1_000_000


@dataclass
class BigDreamSummary:
    mode: str                               # "execute" | "read-only" | "partial"
    census_before: "CorpusCensus"
    census_after: "CorpusCensus | None" = None
    live_before: int = 0                    # facts that aren't redirect stubs
    live_after: int = 0
    consolidation_mode: str = "read-only"   # "execute" | "read-only" | "skipped"
    consolidation_counts: dict = field(default_factory=dict)
    distill_passes: int = 0
    distill_executed: int = 0
    distill_rejected: int = 0
    distill_deferred: int = 0
    distill_failed: int = 0
    stop_reason: str = ""
    notes: list[str] = field(default_factory=list)

    def render(self) -> str:
        b, a = self.census_before, self.census_after
        rows = [("live facts", self.live_before, self.live_after),
                ("rows incl. redirect stubs", b.total, a.total if a else None),
                ("machine-written", b.machine, a.machine if a else None),
                ("hand-written", b.hand, a.hand if a else None)]
        if b.dup and a and a.dup:
            rows.append(("duplicate groups (estimate)", b.dup.groups, a.dup.groups))
        out = [f"big dream — mode: {self.mode}", "",
               f"{'':28} {'before':>8} {'after':>8} {'Δ':>7}"]
        for label, x, y in rows:
            d = f"{y - x:+d}" if y is not None else ""
            out.append(f"{label:28} {x:>8} {'' if y is None else y:>8} {d:>7}")
        out += ["",
                f"convergent dream ({self.consolidation_mode}): {self.consolidation_counts}",
                f"distill: {self.distill_passes} pass(es) · executed {self.distill_executed} · "
                f"rejected {self.distill_rejected} · deferred {self.distill_deferred} · "
                f"failed {self.distill_failed}",
                f"stopped: {self.stop_reason}"]
        out += [f"note: {n}" for n in self.notes]
        return "\n".join(out)


async def _live_count(db: "aiosqlite.Connection") -> int:
    cur = await db.execute(
        "SELECT count(*) FROM semantic_entries "
        "WHERE json_extract(metadata, '$.redirected_to') IS NULL")
    return (await cur.fetchone())[0]


async def run_big_dream(
    db: "aiosqlite.Connection",
    semantic: "SemanticMemory",
    llm_fn: LLMFn,
    *,
    execute_consolidation: bool,
    execute_distill: bool,
    distill_batch: int = DEFAULT_DISTILL_BATCH,
    max_passes: int = DEFAULT_MAX_PASSES,
    min_similarity: float | None = None,
    skip_consolidation: bool = False,
    dreams_dir: Path | None = None,
    log: Callable[[str], None] = logger.info,
) -> BigDreamSummary:
    """Run the big dream; see the module docstring. ``log`` gets progress lines."""
    from loom.autonomy.circadian.journal import (
        append_consolidation_report, append_distill_report,
    )
    from loom.core.cognition.consolidation import run_convergent_dream
    from loom.core.cognition.distillation import (
        DISTILL_MIN_SIMILARITY, OUTCOME_EXECUTED, STATUS_DEFERRED, STATUS_FAILED,
        STATUS_REJECTED, run_distill_dream,
    )
    from loom.core.memory import backup
    from loom.core.memory.census import take_census

    if distill_batch < 1 or max_passes < 1:
        raise ValueError("distill_batch and max_passes must be positive")
    journal = {} if dreams_dir is None else {"dreams_dir": dreams_dir}

    log("census (before)…")
    s = BigDreamSummary(
        mode="read-only", census_before=await take_census(db), live_before=await _live_count(db))
    corpus = s.census_before.total
    log(f"  {s.census_before.render_summary()}")

    if skip_consolidation:
        s.consolidation_mode = "skipped"
    else:
        run_execute = execute_consolidation
        async with memory_rewrite_lock():
            if run_execute and await backup.snapshot_before_write(
                    db, prefix="memory-pre-consolidation") is None:
                run_execute = False
                s.notes.append("convergent dream: pre-execute backup failed — ran read-only")
            log(f"convergent dream over {corpus} rows ({'execute' if run_execute else 'read-only'})…")
            plan, report = await run_convergent_dream(
                semantic, llm_fn, corpus_limit=max(corpus, 1),
                max_clusters=_UNCAPPED, max_review_clusters=_UNCAPPED, execute=run_execute,
            )
        append_consolidation_report(report, **journal)
        s.consolidation_mode = "execute" if run_execute else "read-only"
        s.consolidation_counts = dict(plan.counts())
        log(f"  clusters: {s.consolidation_counts}")

    distill_backup_failed = False
    for _ in range(max_passes if execute_distill else 1):
        s.distill_passes += 1
        log(f"distill pass {s.distill_passes} ({'execute' if execute_distill else 'read-only'}, "
            f"batch {distill_batch})…")
        async with memory_rewrite_lock():
            plan, report = await run_distill_dream(
                semantic, llm_fn, max_neighbourhoods=distill_batch, corpus_limit=max(corpus, 1),
                min_similarity=min_similarity or DISTILL_MIN_SIMILARITY, execute=execute_distill,
            )
        append_distill_report(report, **journal)
        c, o = plan.counts(), plan.outcome_counts()
        s.distill_executed += o[OUTCOME_EXECUTED]
        s.distill_rejected += c[STATUS_REJECTED]
        s.distill_deferred += c[STATUS_DEFERRED]
        s.distill_failed += c[STATUS_FAILED]
        log(f"  neighbourhoods {len(plan.neighbourhoods)} · {c} · outcomes {o}")
        if not plan.neighbourhoods:
            s.stop_reason = "no eligible neighbourhood left"
            break
        if plan.backup_failed:
            distill_backup_failed = True
            s.notes.append("distill: pre-execute backup failed — stopped")
            s.stop_reason = "distill backup failed"
            break
        if not execute_distill:
            s.stop_reason = "read-only: one pass (nothing is consumed, so more would repeat it)"
            break
        if not (o[OUTCOME_EXECUTED] or c[STATUS_REJECTED]):
            s.stop_reason = "no progress (only deferred / failed) — re-run later"
            break
    else:
        s.stop_reason = f"max_passes ({max_passes}) reached — re-run to continue"

    wrote_all = (s.consolidation_mode in ("execute", "skipped")
                 and execute_distill and not distill_backup_failed)
    if wrote_all:
        s.mode = "execute"
    elif execute_consolidation or execute_distill:
        s.mode = "partial"
    log("census (after)…")
    s.census_after = await take_census(db)
    s.live_after = await _live_count(db)
    return s
