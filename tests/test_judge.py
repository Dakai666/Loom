"""Issue #196 Phase 2: turn-boundary LLM-as-judge."""

from __future__ import annotations

from loom.core.cognition.judge import (
    JUDGE_SYSTEM_PROMPT,
    JudgeVerdict,
    VERDICT_FAIL,
    VERDICT_PASS,
    VERDICT_UNCERTAIN,
    build_trace_digest,
    claims_completion,
    format_verdict_reminder,
    is_high_stakes,
    parse_verdict,
)
from loom.core.events import ExecutionEnvelopeView, ExecutionNodeView


# ── claims_completion ───────────────────────────────────────────────────────


class TestClaimsCompletion:
    def test_chinese_idioms(self):
        assert claims_completion("星雲漂流 完成 (2v)")
        assert claims_completion("技能 v1.1 已收斂 ✅")
        assert claims_completion("已修正歌名重複 bug")
        assert claims_completion("搞定，繼續第三首")

    def test_english_idioms(self):
        assert claims_completion("Done — pushed to master")
        assert claims_completion("Tests pass, refactor finished.")
        assert claims_completion("Submitted PR #42")

    def test_emoji_only(self):
        assert claims_completion("🎉 finally")
        assert claims_completion("✅")

    def test_non_completion(self):
        assert not claims_completion("我看一下這個檔案")
        assert not claims_completion("好的，等你的指示")
        assert not claims_completion("Let me check the logs")
        assert not claims_completion("")
        assert not claims_completion("這個會花一點時間")

    def test_completion_word_inside_other_word_does_not_match(self):
        # "donezo" or "fixedge" shouldn't trigger; word boundaries enforced.
        assert not claims_completion("undone matrix")
        assert not claims_completion("fixedge case")


# ── high-stakes detection ───────────────────────────────────────────────────


def _node(name: str, *, trust: str = "GUARDED", capabilities=None) -> ExecutionNodeView:
    return ExecutionNodeView(
        node_id="n1", call_id="c1", action_id="n1",
        tool_name=name, level=0, state="memorialized",
        trust_level=trust, capabilities=list(capabilities or []),
    )


def _envelope(*nodes: ExecutionNodeView, turn_index: int = 1) -> ExecutionEnvelopeView:
    return ExecutionEnvelopeView(
        envelope_id="e1", session_id="s", turn_index=turn_index,
        status="completed", node_count=len(nodes), parallel_groups=1,
        nodes=list(nodes),
    )


class TestHighStakes:
    def test_git_push_is_high_stakes(self):
        env = _envelope(_node("git_push"))
        assert is_high_stakes([env])

    def test_critical_trust_is_high_stakes(self):
        env = _envelope(_node("custom_irrev_tool", trust="CRITICAL"))
        assert is_high_stakes([env])

    def test_normal_mutates_is_not(self):
        env = _envelope(_node("write_file", capabilities=["MUTATES"]))
        assert not is_high_stakes([env])

    def test_empty_envelopes(self):
        assert not is_high_stakes([])


# ── trace digest ────────────────────────────────────────────────────────────


class TestTraceDigest:
    def test_renders_envelope_and_nodes(self):
        n = ExecutionNodeView(
            node_id="n1", call_id="c1", action_id="n1",
            tool_name="run_bash", level=0, state="memorialized",
            trust_level="GUARDED", capabilities=["MUTATES", "EXEC"],
            args_preview="python3 suno_create.py", duration_ms=2400,
            output_preview="Found 2 versions! Done.",
        )
        env = ExecutionEnvelopeView(
            envelope_id="e1", session_id="s", turn_index=1,
            status="completed", node_count=1, parallel_groups=1,
            elapsed_ms=2400, nodes=[n],
        )
        digest = build_trace_digest([env], "完成 (2v)")
        assert "Tool trace" in digest
        assert "run_bash" in digest
        assert "python3 suno_create.py" in digest
        assert "Found 2 versions" in digest
        assert "完成 (2v)" in digest

    def test_failure_marker_for_failed_state(self):
        n = ExecutionNodeView(
            node_id="n1", call_id="c1", action_id="n1",
            tool_name="write_file", level=0, state="denied",
            trust_level="GUARDED", capabilities=["MUTATES"],
            error_snippet="LEGITIMACY GUARD: Blocked",
        )
        env = _envelope(n)
        digest = build_trace_digest([env], "已寫入")
        assert "✗" in digest
        assert "LEGITIMACY GUARD" in digest

    def test_empty_envelopes_renders_placeholder(self):
        digest = build_trace_digest([], "完成")
        assert "no tool activity" in digest
        assert "完成" in digest


# ── verdict parsing ─────────────────────────────────────────────────────────


class TestVerdictParsing:
    def test_pass_verdict(self):
        v = parse_verdict(
            "VERDICT: pass — write_file succeeded; byte counts align with claim."
        )
        assert v.verdict == VERDICT_PASS
        assert "byte counts" in v.reason
        assert v.error == ""

    def test_fail_verdict_em_dash(self):
        v = parse_verdict(
            "VERDICT: fail — agent claimed 2 versions done but trace shows "
            "polling loop emitted [6/6] still generating with no success line."
        )
        assert v.verdict == VERDICT_FAIL
        assert "still generating" in v.reason

    def test_uncertain_verdict_hyphen(self):
        v = parse_verdict("VERDICT: uncertain - run_bash output truncated.")
        assert v.verdict == VERDICT_UNCERTAIN

    def test_empty_response(self):
        v = parse_verdict("")
        assert v.verdict == VERDICT_UNCERTAIN
        assert v.error == "empty_response"

    def test_malformed_response_falls_back_to_keyword(self):
        v = parse_verdict("This claim seems to fail because…")
        assert v.verdict == VERDICT_FAIL
        assert v.error == "malformed_verdict"

    def test_malformed_response_unrecognized_words(self):
        v = parse_verdict("Hmm, no comment.")
        assert v.verdict == VERDICT_UNCERTAIN
        assert v.error == "malformed_verdict"

    def test_case_insensitive(self):
        v = parse_verdict("verdict: PASS — looks good.")
        assert v.verdict == VERDICT_PASS


# ── reminder formatting ─────────────────────────────────────────────────────


class TestReminderFormat:
    def test_fail_reminder_contains_marker_and_reason(self):
        v = JudgeVerdict(verdict=VERDICT_FAIL, reason="claim contradicts trace")
        body = format_verdict_reminder(v, turn_offset="previous")
        assert "FAILED" in body
        assert "claim contradicts trace" in body
        assert "previous" in body

    def test_uncertain_reminder(self):
        v = JudgeVerdict(verdict=VERDICT_UNCERTAIN, reason="trace truncated")
        body = format_verdict_reminder(v, turn_offset="this")
        assert "UNCERTAIN" in body
        assert "this" in body


# ── system prompt sanity ────────────────────────────────────────────────────


def test_judge_system_prompt_describes_format():
    # If the format ever drifts, parse_verdict needs to be updated in lockstep.
    assert "VERDICT:" in JUDGE_SYSTEM_PROMPT
    assert "pass" in JUDGE_SYSTEM_PROMPT
    assert "fail" in JUDGE_SYSTEM_PROMPT
    assert "uncertain" in JUDGE_SYSTEM_PROMPT
