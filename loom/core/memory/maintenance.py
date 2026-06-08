"""
Memory maintenance tools — Issue #149.

Houses the ``dream_cycle`` and ``memory_prune`` tool factories that used to
live in ``loom.extensibility.dreaming_plugin``. Both touch the
``SemanticMemory`` subsystem (relational triples live there too since
#451 phase B) and are conceptually the read-and-synthesize /
decay-management half of ``MemoryGovernor`` — they belong in core memory,
not in the plugin layer.

The core synthesis logic stays in ``loom.core.cognition.dreaming.dream_cycle``;
this module only adapts it to the ``ToolDefinition`` contract and the
``ToolCall`` / ``ToolResult`` types. ``LoomSession.start()`` registers both
factories alongside the other memory tools (``recall``, ``memorize``, ...).
"""

from __future__ import annotations

from typing import Awaitable, Callable, TYPE_CHECKING

from loom.core.harness.middleware import ToolResult
from loom.core.harness.permissions import TrustLevel
from loom.core.harness.registry import ToolDefinition

if TYPE_CHECKING:
    import aiosqlite
    from loom.core.memory.semantic import SemanticMemory


LLMFn = Callable[[list[dict]], Awaitable[str]]


def make_dream_cycle_tool(
    semantic: "SemanticMemory",
    llm_fn: LLMFn,
    db: "aiosqlite.Connection | None" = None,
) -> ToolDefinition:
    """Build the ``dream_cycle`` ToolDefinition.

    Parameters
    ----------
    semantic:
        The semantic memory subsystem (typically taken from ``LoomSession``
        after ``MemoryGovernor`` is set up). Since #451 phase B, dream
        cycle writes its discovered triples directly to the semantic
        store via the ``relational_bridge`` helpers.
    llm_fn:
        Async callable that takes an OpenAI-style messages list and returns
        the assistant's text. ``LoomSession.start()`` wires this to its
        configured router + model.
    db:
        Optional aiosqlite connection — required only when the agent
        invokes the tool with ``themed=true`` (round-robin theme picker
        reads/writes ``memory_meta``). Falls back to free dream when
        absent. Issue #281 P3-D.
    """
    from loom.core.cognition.dreaming import dream_cycle
    from loom.core.memory.ontology import (
        DOMAIN_KNOWLEDGE, DOMAIN_PROJECT, DOMAIN_SELF, DOMAIN_USER,
    )

    _ALLOWED_DOMAINS = {DOMAIN_SELF, DOMAIN_USER, DOMAIN_PROJECT, DOMAIN_KNOWLEDGE}

    async def _executor(call) -> ToolResult:
        sample = int(call.args.get("sample_size", 15))
        dry_run = bool(call.args.get("dry_run", False))
        themed = bool(call.args.get("themed", False))
        domain = call.args.get("domain")
        if domain is not None and domain not in _ALLOWED_DOMAINS:
            return ToolResult(
                call_id=call.id, tool_name=call.tool_name, success=False,
                output=(
                    f"Invalid domain={domain!r}. Allowed: "
                    f"{sorted(_ALLOWED_DOMAINS)} or omit for free dream."
                ),
            )

        result = await dream_cycle(
            semantic=semantic,
            llm_fn=llm_fn,
            sample_size=sample,
            dry_run=dry_run,
            domain=domain,
            themed=themed,
            db=db,
        )

        lines = [
            "Dream cycle complete",
            f"  Mode         : {'themed' if result.get('domain') else 'free'}"
            + (f" (domain={result['domain']})" if result.get("domain") else ""),
            f"  Facts sampled: {result['facts_sampled']}",
            f"  Triples found: {result['triples_found']}",
            f"  Triples written: {result['triples_written']}",
        ]
        if result["errors"]:
            lines.append(f"  Warnings: {'; '.join(result['errors'])}")
        if dry_run:
            lines.append("  (dry-run — nothing was written)")

        return ToolResult(
            call_id=call.id, tool_name=call.tool_name, success=True,
            output="\n".join(lines),
        )

    return ToolDefinition(
        name="dream_cycle",
        description=(
            "Run an offline dreaming cycle: sample random semantic facts, "
            "discover non-obvious connections via the LLM, and store the "
            "resulting insights as Relational triples (source='dreaming'). "
            "Default is a free cross-domain dream. Pass themed=true to "
            "round-robin through ontology domains (self → user → project "
            "→ knowledge), or domain='<name>' to pin one. Use this when "
            "the autonomy scheduler triggers a background synthesis task."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "sample_size": {
                    "type": "integer",
                    "description": "Number of random facts to sample (default 15, max 30).",
                    "default": 15,
                },
                "dry_run": {
                    "type": "boolean",
                    "description": "If true, run the full cycle but skip writing to the DB.",
                    "default": False,
                },
                "domain": {
                    "type": "string",
                    "enum": ["self", "user", "project", "knowledge"],
                    "description": (
                        "Pin the sample to one ontology domain. When set "
                        "alongside themed=true, domain wins (explicit name "
                        "is stronger than rotation)."
                    ),
                },
                "themed": {
                    "type": "boolean",
                    "description": (
                        "Round-robin the next domain (cycles self → user → "
                        "project → knowledge). Skipped when domain is also "
                        "set, or falls back to free dream if no DB is wired."
                    ),
                    "default": False,
                },
            },
        },
        executor=_executor,
        trust_level=TrustLevel.SAFE,
    )


def make_memory_prune_tool(semantic: "SemanticMemory") -> ToolDefinition:
    """Build the ``memory_prune`` ToolDefinition.

    Decays semantic-memory entries below ``threshold`` effective confidence.
    The default 90-day half-life means a fact written at confidence 0.8
    drops below 0.1 after ~282 days without an update.
    """
    async def _executor(call) -> ToolResult:
        threshold = float(call.args.get("threshold", 0.1))
        dry_run = bool(call.args.get("dry_run", False))

        result = await semantic.prune_decayed(threshold=threshold, dry_run=dry_run)

        lines = [
            "Memory prune complete",
            f"  Examined : {result['examined']} facts",
            f"  Pruned   : {result['pruned']} (effective_confidence < {threshold})",
            f"  Retained : {result['retained']}",
        ]
        if dry_run:
            lines.append("  (dry-run — nothing was deleted)")

        return ToolResult(
            call_id=call.id, tool_name=call.tool_name, success=True,
            output="\n".join(lines),
        )

    return ToolDefinition(
        name="memory_prune",
        description=(
            "Remove semantic memory entries whose effective confidence has "
            "decayed below a threshold. Effective confidence uses a 90-day "
            "half-life: a fact with initial confidence 0.8 drops below 0.1 "
            "after ~282 days of no update. Use dry_run=true to preview what "
            "would be deleted."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "threshold": {
                    "type": "number",
                    "description": "Delete entries with effective_confidence below this value (default 0.1).",
                    "default": 0.1,
                },
                "dry_run": {
                    "type": "boolean",
                    "description": "If true, report what would be pruned without deleting anything.",
                    "default": False,
                },
            },
        },
        executor=_executor,
        trust_level=TrustLevel.SAFE,
    )


def make_convergent_dream_tool(
    semantic: "SemanticMemory",
    llm_fn: LLMFn,
    *,
    timezone: str = "Asia/Taipei",
    dreams_dir=None,
) -> ToolDefinition:
    """Build the ``convergent_dream`` ToolDefinition (#488, P1).

    The convergent counterpart to ``dream_cycle``: scans the semantic store,
    proposes merge / reconcile / clean clusters, runs the diff-inventory gate
    and 絲絲's self-review, then writes a 夢境鞏固 report to its own dated
    ``dreams/`` file. **Read-only** — this phase never writes to the DB;
    execution of the approved clusters lands in P2 (#489).

    Parameters mirror ``make_journal_append_tool`` (``timezone`` /
    ``dreams_dir``) so tests can redirect the report IO.
    """
    from loom.core.cognition.consolidation import (
        KIND_CLEAN, KIND_MERGE, KIND_RECONCILE, run_convergent_dream,
    )
    from loom.autonomy.circadian.journal import append_consolidation_report

    async def _executor(call) -> ToolResult:
        corpus_limit = int(call.args.get("corpus_limit", 2000))
        max_clusters = int(call.args.get("max_clusters", 20))
        min_similarity = float(call.args.get("min_similarity", 0.85))
        review_batch_size = int(call.args.get("review_batch_size", 10))
        max_review_clusters = int(call.args.get("max_review_clusters", 40))

        # The batch/cap knobs feed range() steps and slice bounds; a
        # non-positive value would crash the pass or silently leave every
        # cluster unreviewed (#502 review). Reject before any work runs so the
        # SAFE tool returns a friendly error and writes no report.
        if review_batch_size < 1 or max_review_clusters < 1:
            return ToolResult(
                call_id=call.id, tool_name=call.tool_name, success=False,
                error=(
                    "review_batch_size and max_review_clusters must be positive "
                    f"integers; got review_batch_size={review_batch_size}, "
                    f"max_review_clusters={max_review_clusters}."
                ),
            )

        plan, report = await run_convergent_dream(
            semantic, llm_fn,
            corpus_limit=corpus_limit,
            min_similarity=min_similarity,
            max_clusters=max_clusters,
            review_batch_size=review_batch_size,
            max_review_clusters=max_review_clusters,
        )

        path = append_consolidation_report(
            report, timezone=timezone, dreams_dir=dreams_dir,
        )

        counts = plan.counts()
        verdicts: dict[str, int] = {}
        for d in plan.decisions:
            verdicts[d.verdict] = verdicts.get(d.verdict, 0) + 1
        verdict_str = ", ".join(f"{k}={v}" for k, v in sorted(verdicts.items())) or "(none)"

        lines = [
            "Convergent dream complete (read-only / P1)",
            f"  Scanned     : {plan.scanned} facts",
            f"  Clusters    : {counts[KIND_MERGE]} merge, "
            f"{counts[KIND_RECONCILE]} reconcile, {counts[KIND_CLEAN]} clean",
            f"  Self-review : {verdict_str}",
            f"  Report      : {path}",
        ]
        if plan.deferred_to_next_pass:
            lines.append(f"  Deferred    : {plan.deferred_to_next_pass} clusters → next pass")

        return ToolResult(
            call_id=call.id, tool_name=call.tool_name, success=True,
            output="\n".join(lines),
        )

    return ToolDefinition(
        name="convergent_dream",
        description=(
            "Run a read-only convergent dream: scan semantic memory for "
            "duplicate / contradicting / orphaned facts, propose how to "
            "consolidate them, run a self-review with veto, and write a "
            "夢境鞏固 report to the circadian journal. Does NOT modify memory "
            "(P1) — it plans and records so consolidation can be reviewed "
            "before it ever touches a fact. Counterpart to dream_cycle "
            "(which grows new connections)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "corpus_limit": {
                    "type": "integer",
                    "description": "Max facts to scan this pass (default 2000).",
                    "default": 2000,
                },
                "max_clusters": {
                    "type": "integer",
                    "description": "Cap on merge clusters proposed per pass; overflow is deferred and reported (default 20).",
                    "default": 20,
                },
                "min_similarity": {
                    "type": "number",
                    "description": "Cosine threshold for near-duplicate merge candidates (default 0.85).",
                    "default": 0.85,
                },
                "review_batch_size": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Clusters per self-review LLM call; keeps each prompt context-bounded (default 10).",
                    "default": 10,
                },
                "max_review_clusters": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Cap on clusters self-reviewed per pass; overflow is deferred to the next pass (default 40).",
                    "default": 40,
                },
            },
        },
        executor=_executor,
        trust_level=TrustLevel.SAFE,
    )


def make_prediction_reconcile_tool(
    db: "aiosqlite.Connection",
    *,
    timezone: str = "Asia/Taipei",
    dreams_dir=None,
) -> ToolDefinition:
    """Build the ``prediction_reconcile`` ToolDefinition (epic #528, slice 3.5).

    The convergent-dream sibling that closes the Prediction Spine loop on the
    dream schedule: matured bets are judged against runtime ground truth and a
    reconcile report is written to the dreams journal.

    **Read-only by default (I3 at the schedule level).** ``dry_run`` defaults to
    True — the scheduled/triggered pass proposes and reports without writing the
    spine. Only ``dry_run=false`` commits scores via ``mark_reconciled``, the
    single write path. This mirrors the convergent dream's P4a discipline so the
    spine is never written speculatively by a schedule.
    """
    from loom.core.cognition.prediction_reconcile import run_prediction_reconciliation
    from loom.core.memory.prediction import PredictionStore
    from loom.autonomy.circadian.journal import append_consolidation_report

    async def _executor(call) -> ToolResult:
        dry_run = bool(call.args.get("dry_run", True))  # P4a: read-only default

        store = PredictionStore(db)
        report = await run_prediction_reconciliation(store, db, execute=not dry_run)

        path = append_consolidation_report(
            report.render(),
            timezone=timezone, dreams_dir=dreams_dir,
        )

        verb = "reconciled" if report.executed else "would reconcile (dry-run)"
        return ToolResult(
            call_id=call.id, tool_name=call.tool_name, success=True,
            output=(
                f"prediction reconcile: {verb} {len(report.proposals)}, "
                f"skipped {len(report.skipped)}\n  Report: {path}"
            ),
        )

    return ToolDefinition(
        name="prediction_reconcile",
        description=(
            "Reconcile matured predictions against runtime observation and write "
            "a report to the circadian dream journal. Read-only by default "
            "(dry_run=true): proposes scores without writing the spine. Set "
            "dry_run=false to commit reconciliation. Counterpart to "
            "convergent_dream (which consolidates facts)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "dry_run": {
                    "type": "boolean",
                    "description": (
                        "Propose without writing the spine (default true). "
                        "Set false to commit scores via mark_reconciled."
                    ),
                    "default": True,
                },
            },
        },
        executor=_executor,
        trust_level=TrustLevel.SAFE,
    )
