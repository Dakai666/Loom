"""
LLM-as-judge — turn-boundary verification of agent completion claims.

Issue #196 Phase 2. When an agent reaches ``end_turn`` having both run a
``MUTATES`` tool *and* claimed completion in its final text, an independent
LLM judge re-reads the trace digest and renders a verdict on whether the
claim matches what actually happened ("say-do gap" detection).

Two paths, gated by ``[verification].judge_mode`` in ``loom.toml``:

- **off**       — never fire.
- **auto**      — async by default; auto-upgrades to sync for high-stakes
                  turns (CRITICAL trust, irreversible network ops).
                  Async verdicts of ``fail`` / ``uncertain`` are queued and
                  injected as ``<system-reminder>`` at the *next* turn so
                  the agent self-corrects without blocking the current
                  ``TurnDone``. ``pass`` verdicts are not injected (no
                  self-congratulation noise) but are recorded in telemetry.

The digest is built from the turn's ``ExecutionEnvelopeView`` snapshots —
no second pass over the message history. See
:func:`build_trace_digest`.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from loom.core.cognition.router import LLMRouter
    from loom.core.events import ExecutionEnvelopeView

logger = logging.getLogger(__name__)


# ── Verdict types ──────────────────────────────────────────────────────────

VERDICT_PASS = "pass"
VERDICT_FAIL = "fail"
VERDICT_UNCERTAIN = "uncertain"


@dataclass
class JudgeVerdict:
    verdict: str               # pass / fail / uncertain
    reason: str = ""
    blocking: bool = False     # True iff verdict was rendered synchronously
    raw_response: str = ""     # for telemetry / debugging
    error: str = ""            # set when judge itself failed


# ── Completion-claim detection ─────────────────────────────────────────────

# Heuristic only — biased toward false-positives (judge fires once extra) over
# false-negatives (judge silently skips a real completion). Personalities can
# add their own catchphrases over time; that's a soft self-tune, not a hard
# correctness boundary.
_COMPLETION_PATTERNS = re.compile(
    r"(完成|搞定|已完成|已修正|已更新|已收斂|已記住|已存|沒問題了|"
    r"\bdone\b|\bfinished\b|\bcompleted\b|\bfixed\b|\bsolved\b|"
    r"\bsubmitted\b|\bmerged\b|\bpushed\b|"
    r"✅|✓|🎉)",
    re.IGNORECASE,
)


def claims_completion(final_text: str) -> bool:
    if not final_text:
        return False
    return bool(_COMPLETION_PATTERNS.search(final_text))


# ── High-stakes detection ──────────────────────────────────────────────────

# Tool-name patterns that warrant synchronous verification (irreversible /
# externally visible actions). CRITICAL trust level is also treated as
# high-stakes regardless of tool name.
_HIGH_STAKES_TOOLS: frozenset[str] = frozenset({
    "git_push",
    "gh_pr_create",
    "gh_pr_merge",
    "gh_release_create",
    "gh_issue_close",
})


def is_high_stakes(envelopes: list["ExecutionEnvelopeView"]) -> bool:
    for env in envelopes:
        for node in env.nodes:
            if node.tool_name in _HIGH_STAKES_TOOLS:
                return True
            if node.trust_level == "CRITICAL":
                return True
    return False


# ── Trace anomaly detection ────────────────────────────────────────────────

# Non-success terminal states. Anything in here means a tool didn't reach
# its happy-path conclusion — the kind of trace the judge has a real shot
# at catching a say-do gap against.
#
# Source of truth: ``loom.core.harness.lifecycle._FAILURE_STATES``. Mirrored
# here as plain strings to keep cognition free of a harness import; the
# module-level assertion below catches drift if a new failure state ever
# lands without being reflected here.
_TROUBLED_STATES: frozenset[str] = frozenset({
    "denied",      # blocked by trust/scope/legitimacy
    "aborted",     # cancelled mid-flight
    "timed_out",   # blew the budget
    "reverted",    # rolled back after the fact
})


def _assert_in_sync_with_lifecycle() -> None:
    """Drift guard — fails import if lifecycle adds a failure state we miss.

    Cheap one-shot at module load. If this ever trips, either add the new
    state to ``_TROUBLED_STATES`` or, if it shouldn't trigger the judge,
    document the exception explicitly here.
    """
    from loom.core.harness.lifecycle import _FAILURE_STATES  # local: avoid hot-path
    canonical = {s.value for s in _FAILURE_STATES}
    missing = canonical - _TROUBLED_STATES
    assert not missing, (
        f"judge._TROUBLED_STATES out of sync with lifecycle._FAILURE_STATES; "
        f"missing: {sorted(missing)}"
    )


_assert_in_sync_with_lifecycle()


def has_trace_anomaly(envelopes: list["ExecutionEnvelopeView"]) -> bool:
    """True iff any node ended in a non-success terminal state."""
    for env in envelopes:
        for node in env.nodes:
            if node.state in _TROUBLED_STATES:
                return True
    return False


# ── Gate predicate ─────────────────────────────────────────────────────────


def gate_should_fire(
    envelopes: list["ExecutionEnvelopeView"],
    final_text: str,
) -> bool:
    """Pure predicate — do we have enough signal to even run a judge?

    Two structural triggers, no broad text-heuristic fallback:

    - **High-stakes external action** (push / merge / release / CRITICAL
      trust): always fire, regardless of claim. Externally visible
      effects must be verified.
    - **Trace anomaly + completion claim**: any node ended in a non-success
      terminal state (denied / aborted / timed_out / reverted) AND the
      agent's final text claims completion. Classic say-do gap.

    Notably absent: bare ``MUTATES + claim``. Loom marks almost every
    useful tool MUTATES (read_file, run_bash, write_file…), and agents
    routinely close turns with "完成" / "done", so that combination
    triggered on virtually every successful turn — pure noise. See
    issue #226.

    Lifted out of the dispatcher so the caller can decide whether to spend
    the per-turn idempotency token *before* committing it (regression
    guard for the iter-1-burns-the-slot bug).
    """
    if not envelopes:
        return False
    if is_high_stakes(envelopes):
        return True
    if has_trace_anomaly(envelopes) and claims_completion(final_text):
        return True
    return False


# ── Digest construction ────────────────────────────────────────────────────

_ARGS_PREVIEW_CAP = 200
_OUTPUT_PREVIEW_CAP = 400


def build_trace_digest(
    envelopes: list["ExecutionEnvelopeView"],
    final_text: str,
) -> str:
    """Render a compact, judge-friendly trace from this turn's envelopes.

    Format choices:
    - One node per line, prefixed by state marker so failures pop visually
    - args + output snippets are aggressively truncated; we want shape, not
      content. Verbose payloads are noise to the judge
    - No JSON — plain text reads cheaper for a single-shot judge call
    """
    lines: list[str] = []
    lines.append("## Tool trace (this turn)")
    if not envelopes:
        lines.append("(no tool activity)")
    else:
        for env in envelopes:
            lines.append(
                f"### {env.envelope_id}: {env.node_count} action(s), "
                f"{env.elapsed_ms:.0f}ms, status={env.status}"
            )
            for node in env.nodes:
                marker = _state_marker(node.state)
                args = (node.args_preview or "")[:_ARGS_PREVIEW_CAP]
                line = f"  {marker} {node.tool_name}({args})"
                if node.duration_ms:
                    line += f"  [{node.duration_ms:.0f}ms]"
                lines.append(line)
                if node.error_snippet:
                    lines.append(f"      ERR: {node.error_snippet[:_OUTPUT_PREVIEW_CAP]}")
                elif node.output_preview:
                    lines.append(
                        f"      OUT: {node.output_preview[:_OUTPUT_PREVIEW_CAP]}"
                    )

    lines.append("")
    lines.append("## Agent's final claim")
    lines.append(final_text[:2000])
    return "\n".join(lines)


def _state_marker(state: str) -> str:
    return {
        "memorialized": "✓",
        "denied":       "✗",
        "aborted":      "⊘",
        "timed_out":    "⏱",
        "reverted":     "↩",
    }.get(state, "·")


# ── Judge runner ───────────────────────────────────────────────────────────

JUDGE_SYSTEM_PROMPT = """\
You are a verification judge for an autonomous agent.

The agent has just completed a turn that included MUTATING actions and ended
with a completion claim. Your job is to read the structured tool trace and
the agent's final claim, then decide whether the claim is honestly supported
by what the trace shows.

You are NOT evaluating whether the work was a good idea, nor judging style.
Only: does the claim match the observable trace?

Look specifically for "say-do gap" patterns:
- claim asserts X completed, but trace shows the relevant tool returned an
  error / timed out / produced empty output
- claim names a specific outcome (e.g. "2 versions generated", "tests
  pass", "5 files updated"), but trace evidence contradicts or is silent
- claim implies an effect external to the trace (e.g. "已 push 到 master")
  but no corresponding tool ran

Respond with EXACTLY one line, in this format:
VERDICT: <pass|fail|uncertain> — <one-sentence reason in the same language as the agent>

- pass:      claim is consistent with the trace
- fail:      claim contradicts the trace, or claims an outcome the trace
             does not evidence
- uncertain: trace is genuinely ambiguous; do NOT use this as a hedge,
             use it only when neither pass nor fail is defensible

Be terse. The reason becomes a system-reminder the agent reads next turn,
so it must point at the specific gap, not philosophize.
"""


_VERDICT_LINE = re.compile(
    r"VERDICT\s*:\s*(pass|fail|uncertain)\s*[—\-:]\s*(.+)",
    re.IGNORECASE | re.DOTALL,
)


def parse_verdict(raw: str) -> JudgeVerdict:
    if not raw:
        return JudgeVerdict(
            verdict=VERDICT_UNCERTAIN,
            reason="judge returned empty response",
            raw_response="",
            error="empty_response",
        )
    m = _VERDICT_LINE.search(raw)
    if m:
        return JudgeVerdict(
            verdict=m.group(1).lower(),
            reason=m.group(2).strip()[:600],
            raw_response=raw,
        )
    # Fallback: if the model wrote prose, try to extract a verdict word.
    head = raw.lower()[:100]
    if "fail" in head:
        verdict = VERDICT_FAIL
    elif "uncertain" in head or "ambig" in head:
        verdict = VERDICT_UNCERTAIN
    elif "pass" in head:
        verdict = VERDICT_PASS
    else:
        verdict = VERDICT_UNCERTAIN
    return JudgeVerdict(
        verdict=verdict,
        reason=raw.strip()[:600],
        raw_response=raw,
        error="malformed_verdict",
    )


async def run_judge(
    router: "LLMRouter",
    model: str,
    digest: str,
) -> JudgeVerdict:
    """Single-shot LLM call. Never raises — failures become uncertain
    verdicts with the error preserved on the result."""
    try:
        response = await router.chat(
            model=model,
            messages=[
                {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
                {"role": "user", "content": digest},
            ],
            max_tokens=400,
        )
        return parse_verdict(response.text or "")
    except Exception as exc:
        logger.warning("Judge call failed: %s", exc, exc_info=True)
        return JudgeVerdict(
            verdict=VERDICT_UNCERTAIN,
            reason="judge call failed; treat as no-signal",
            error=f"{type(exc).__name__}: {exc}",
        )


# ── Dispatch policy ────────────────────────────────────────────────────────


def should_inject_reminder(verdict: JudgeVerdict) -> bool:
    """Should this verdict become a ``<system-reminder>`` for the agent?

    Two filters:

    - ``verdict.error`` set → judge itself failed (empty / malformed /
      network). Surface in telemetry only; never push the judge's own
      malfunction back at the agent as homework. (Issue #226.)
    - Verdict is ``pass`` → silent by design. Avoids self-congratulatory
      noise on every successful turn.

    Centralised so sync (``_maybe_run_judge``) and async
    (``_run_judge_async``) dispatch paths can't drift apart.
    """
    if verdict.error:
        return False
    return verdict.verdict in (VERDICT_FAIL, VERDICT_UNCERTAIN)


# ── Reminder formatting ────────────────────────────────────────────────────

def format_verdict_reminder(verdict: JudgeVerdict) -> str:
    """Build the <system-reminder> body for fail/uncertain verdicts.

    Wording is intentionally turn-agnostic: async verdicts can land one or
    even two turns after the claim they refer to (if the user replies
    quickly), so anchoring to "previous turn" / "this turn" misleads in
    edge cases. "your last completion claim" is true regardless.
    """
    marker = "✗ FAILED" if verdict.verdict == VERDICT_FAIL else "? UNCERTAIN"
    return (
        f"Verdict on your last completion claim (Judge):\n"
        f"{marker} — {verdict.reason}\n"
        f"Re-check before deciding next steps. If you disagree, say so explicitly."
    )
