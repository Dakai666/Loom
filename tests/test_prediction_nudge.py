"""
Prediction Spine — autonomy-path betting nudge (epic #528, P0.5-a follow-up).

The ``predict`` tool (#537 slice A) is a passive registry tool: nothing prompts
the agent to use it, so the live corpus had **zero** explicit bets — a
monoculture of ``auto:`` heartbeats that only measures raw tool reliability, not
prediction skill. This nudge makes deliberate bets *flow* by softly inviting one
each self-driven turn.

Contract (pure function, no session state):
  * fires ONLY on self-driven origins (chime / autonomy) — never human chat,
    so the human path DK already steers stays unpolluted.
  * gated on ``predict_tool_enabled`` — never nudge toward a tool that isn't
    registered.
  * optional + behaviour-neutral wording — preserves I5 (prediction has no
    direct path to driving the action taken).
"""

from loom.core.session_support import prediction_nudge_body, _SELF_DRIVEN_ORIGINS


class TestSelfDrivenOriginSet:
    def test_self_driven_set_is_chime_and_autonomy(self):
        # subagent is context-isolation, not a betting context; human-facing
        # origins (interactive/user/discord/mcp) must stay out.
        assert _SELF_DRIVEN_ORIGINS == frozenset({"chime", "autonomy"})


class TestNudgeFires:
    def test_chime_with_tool_enabled_nudges(self):
        body = prediction_nudge_body("chime", predict_tool_enabled=True)
        assert body is not None
        assert "predict" in body.lower()

    def test_autonomy_with_tool_enabled_nudges(self):
        body = prediction_nudge_body("autonomy", predict_tool_enabled=True)
        assert body is not None
        assert "predict" in body.lower()

    def test_nudge_is_optional_and_behaviour_neutral(self):
        """I5: the bet must never change which action the agent takes."""
        body = prediction_nudge_body("autonomy", predict_tool_enabled=True).lower()
        assert "optional" in body
        # explicitly disclaims steering the action
        assert "must not change" in body or "without changing" in body

    def test_nudge_targets_uncertain_actions(self):
        """Hardened 2026-07-03 (#528): the original soft "consider …" wording
        fired for ~10 days on autonomy turns and yielded zero explicit bets, and
        the origins that fire are routine — so an obeyed nudge would just make a
        second trivial-bet monoculture. The wording now carries the judgement:
        bet when the outcome is genuinely uncertain, skip the sure things (the
        heartbeat already records those). Same criterion as Agent.md's chat-path
        guidance."""
        body = prediction_nudge_body("autonomy", predict_tool_enabled=True).lower()
        # steer toward genuine uncertainty
        assert "uncertain" in body
        # and explicitly tell it to skip sure-thing routine bets
        assert "skip" in body
        assert "already know" in body


class TestNudgeSuppressed:
    def test_tool_disabled_suppresses_nudge(self):
        # registering nothing → nudging toward an absent tool would be a dead end
        assert prediction_nudge_body("chime", predict_tool_enabled=False) is None
        assert prediction_nudge_body("autonomy", predict_tool_enabled=False) is None

    def test_human_origins_never_nudged(self):
        for origin in ("interactive", "user", "discord", "mcp"):
            assert prediction_nudge_body(origin, predict_tool_enabled=True) is None

    def test_subagent_origin_not_nudged(self):
        assert prediction_nudge_body("subagent", predict_tool_enabled=True) is None

    def test_missing_attr_falls_back_to_suppress(self):
        """The stream_turn call site reads the flag via getattr(self, ..., False),
        so a partially-constructed session that never set _predict_tool_enabled
        suppresses the nudge rather than raising — mirrors the nearby
        hasattr(self, "_legitimacy_guard") guard (絲絲 PR #560 review)."""
        class _BareSession:  # no _predict_tool_enabled attribute
            pass

        enabled = getattr(_BareSession(), "_predict_tool_enabled", False)
        assert prediction_nudge_body("autonomy", predict_tool_enabled=enabled) is None
