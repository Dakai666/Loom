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
    gate_should_fire,
    has_trace_anomaly,
    is_high_stakes,
    parse_verdict,
    should_inject_reminder,
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


def _node(
    name: str,
    *,
    trust: str = "GUARDED",
    capabilities=None,
    state: str = "memorialized",
) -> ExecutionNodeView:
    return ExecutionNodeView(
        node_id="n1", call_id="c1", action_id="n1",
        tool_name=name, level=0, state=state,
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


# ── gate predicate ──────────────────────────────────────────────────────────


class TestHasTraceAnomaly:
    def test_all_success_is_clean(self):
        env = _envelope(
            _node("write_file", capabilities=["MUTATES"]),
            _node("run_bash", capabilities=["EXEC", "MUTATES"]),
        )
        assert not has_trace_anomaly([env])

    def test_denied_node_is_anomaly(self):
        env = _envelope(_node("write_file", state="denied"))
        assert has_trace_anomaly([env])

    def test_timed_out_node_is_anomaly(self):
        env = _envelope(_node("run_bash", state="timed_out"))
        assert has_trace_anomaly([env])

    def test_aborted_node_is_anomaly(self):
        env = _envelope(_node("run_bash", state="aborted"))
        assert has_trace_anomaly([env])

    def test_reverted_node_is_anomaly(self):
        env = _envelope(_node("write_file", state="reverted"))
        assert has_trace_anomaly([env])

    def test_one_bad_among_many_good(self):
        envs = [
            _envelope(_node("read_file")),
            _envelope(_node("run_bash", state="timed_out")),
            _envelope(_node("write_file")),
        ]
        assert has_trace_anomaly(envs)

    def test_empty_envelopes(self):
        assert not has_trace_anomaly([])


class TestGateShouldFire:
    """Pure-predicate regression tests.

    Two structural triggers (issue #226):
    - is_high_stakes(envelopes) → fire (claim irrelevant)
    - has_trace_anomaly(envelopes) AND claims_completion(text) → fire

    Bare ``MUTATES + claim`` is intentionally NOT a trigger — that
    combination was the noise source the issue called out.

    Also guards the legacy iter-1-burns-the-slot bug: the gate is pure,
    so the dispatcher only commits the per-turn token when this returns
    True, allowing a late-appearing MUTATES tool to still be judged.
    """

    def test_no_envelopes_skips(self):
        assert not gate_should_fire([], "完成 ✅")

    def test_clean_run_with_completion_claim_skips(self):
        # The #226 noise case: read_file + completion text and nothing went
        # wrong. Should NOT fire.
        env = _envelope(_node("read_file", capabilities=["READ_PROBE"]))
        assert not gate_should_fire([env], "看完了，沒問題 ✅")

    def test_clean_mutates_with_claim_skips(self):
        # The other half of the noise case: a successful MUTATES write
        # plus completion text. Old gate fired here; new gate doesn't.
        env = _envelope(_node("write_file", capabilities=["MUTATES"]))
        assert not gate_should_fire([env], "技能 v1.1 已收斂 ✅")

    def test_anomaly_without_claim_skips(self):
        # Tool failed but the agent didn't claim done — agent is presumably
        # already going to retry / report. Don't add a judge layer.
        env = _envelope(_node("write_file", state="denied"))
        assert not gate_should_fire([env], "權限不夠，幫我加 scope 後我再試一次")

    def test_anomaly_plus_claim_fires(self):
        # The classic say-do gap: tool denied/timed_out but agent says done.
        env = _envelope(_node("write_file", state="denied"))
        assert gate_should_fire([env], "已寫入 ✅")

    def test_high_stakes_always_fires_no_claim_needed(self):
        # External-effect tools must be verified regardless of phrasing.
        env = _envelope(_node("git_push"))
        assert gate_should_fire([env], "推上去看看")

    def test_high_stakes_fires_even_without_anomaly(self):
        env = _envelope(_node("gh_pr_merge"))
        assert gate_should_fire([env], "merge")

    def test_critical_trust_fires(self):
        env = _envelope(_node("custom_tool", trust="CRITICAL"))
        assert gate_should_fire([env], "ok")

    def test_regression_late_appearing_signal_still_judged(self):
        """Iter-1-burns-the-slot regression guard.

        With the v1 buggy dispatcher this returned True at iter 1 (claim
        present, no envelopes ⇒ False) and False at iter 3 because the
        idempotency token was already consumed. Now the gate is pure and
        the caller only commits the token when the gate actually returns
        True — so iter 3 fires correctly.
        """
        assert not gate_should_fire([], "我先看一下 ✅")

        env = _envelope(_node("git_push"))
        assert gate_should_fire([env], "已 push ✅")


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


# ── dispatch policy ─────────────────────────────────────────────────────────


class TestShouldInjectReminder:
    """The single predicate both sync + async dispatch paths consult.

    Centralises the #226 fix: judge self-failures (verdict.error set) must
    NOT be promoted to agent-facing reminders, regardless of what verdict
    string parse_verdict happened to assign as a fallback.
    """

    def test_pass_is_silent(self):
        v = JudgeVerdict(verdict=VERDICT_PASS, reason="all good")
        assert not should_inject_reminder(v)

    def test_fail_injects(self):
        v = JudgeVerdict(verdict=VERDICT_FAIL, reason="say-do gap")
        assert should_inject_reminder(v)

    def test_uncertain_injects(self):
        v = JudgeVerdict(verdict=VERDICT_UNCERTAIN, reason="ambiguous trace")
        assert should_inject_reminder(v)

    def test_error_swallows_uncertain(self):
        # The exact #226 noise case: judge model returned empty → parse_verdict
        # produced uncertain + error="empty_response". Must NOT inject.
        v = JudgeVerdict(
            verdict=VERDICT_UNCERTAIN,
            reason="judge returned empty response",
            error="empty_response",
        )
        assert not should_inject_reminder(v)

    def test_error_swallows_fail(self):
        # Defensive: even if a malformed-fallback path lands on `fail`, an
        # error-tagged verdict still represents judge malfunction, not agent
        # gap. Don't promote it.
        v = JudgeVerdict(
            verdict=VERDICT_FAIL,
            reason="...",
            error="malformed_verdict",
        )
        assert not should_inject_reminder(v)

    def test_error_swallows_pass_too(self):
        # Vacuously true (pass already silent) but locks the contract: the
        # error gate runs first, independent of the verdict string.
        v = JudgeVerdict(verdict=VERDICT_PASS, reason="x", error="boom")
        assert not should_inject_reminder(v)


# ── lifecycle drift guard ───────────────────────────────────────────────────


def test_troubled_states_match_lifecycle_failure_states():
    """If lifecycle adds a new failure state, this test breaks loud.

    The judge module also asserts this at import time, but we duplicate the
    check at test time so a CI failure points at this exact concern rather
    than a generic ImportError.
    """
    from loom.core.cognition.judge import _TROUBLED_STATES
    from loom.core.harness.lifecycle import _FAILURE_STATES

    canonical = {s.value for s in _FAILURE_STATES}
    assert canonical == _TROUBLED_STATES, (
        f"lifecycle._FAILURE_STATES={canonical} drifted from "
        f"judge._TROUBLED_STATES={_TROUBLED_STATES}"
    )


# ── reminder formatting ─────────────────────────────────────────────────────


class TestReminderFormat:
    def test_fail_reminder_contains_marker_and_reason(self):
        v = JudgeVerdict(verdict=VERDICT_FAIL, reason="claim contradicts trace")
        body = format_verdict_reminder(v)
        assert "FAILED" in body
        assert "claim contradicts trace" in body
        assert "completion claim" in body

    def test_uncertain_reminder(self):
        v = JudgeVerdict(verdict=VERDICT_UNCERTAIN, reason="trace truncated")
        body = format_verdict_reminder(v)
        assert "UNCERTAIN" in body
        assert "trace truncated" in body

    def test_reminder_is_turn_agnostic(self):
        """No 'previous' / 'this' anchors — async verdicts can land 2+ turns
        late, so the wording must stay correct regardless of timing."""
        v = JudgeVerdict(verdict=VERDICT_FAIL, reason="x")
        body = format_verdict_reminder(v)
        assert "previous turn" not in body
        assert "this turn" not in body


# ── system prompt sanity ────────────────────────────────────────────────────


def test_judge_system_prompt_describes_format():
    # If the format ever drifts, parse_verdict needs to be updated in lockstep.
    assert "VERDICT:" in JUDGE_SYSTEM_PROMPT
    assert "pass" in JUDGE_SYSTEM_PROMPT
    assert "fail" in JUDGE_SYSTEM_PROMPT
    assert "uncertain" in JUDGE_SYSTEM_PROMPT
