"""
Prediction Spine — semantic observation-surface expansion (#569, spec docs/designs/59 §9).

The explicit-path blocker: semantic resolvers (output_contains / output_regex /
row_count) were whitelisted but the action observation never carried the fields
they need, so every such bet starved ``unresolvable``. These tests pin the §9
contract *before* the implementation:

* **Capture projection** (``capture_output_fields``) — the canonical string is
  the same world-reply the agent saw (str(output) on success, error text on
  failure); prefix capped, sha256 digest + full length kept so a truncated miss
  stays honest; row_count is a pure world function (len for list, splitlines
  for str).
* **Observation widening** (``_resolve_action`` via ``resolve_observation_ref``)
  — new rows expose output / output_truncated / row_count; legacy rows (NULL
  capture columns) omit them so semantic resolvers stay ``unresolvable``
  (no silent pass, no retro-scoring).
* **Truncation semantics** (resolvers) — needle/pattern miss on a truncated
  capture raises (→ unresolvable skip), never a false "absent".
* **Write-time refusal** (predict tool) — a resolver kind whose fields the
  action surface does not capture (file_digest_changed, Q3 deferred) is refused
  at bet time instead of rotting pending; that rot is the exact failure mode
  that drove the agent away (§8).
* **End-to-end** — the 絲絲 scenario red→green: an explicit next_action
  output_contains bet settles against a captured action row.
"""

import json

import pytest
import pytest_asyncio

from loom.core.memory.store import SQLiteStore
from loom.core.memory.observation import (
    OUTPUT_PREFIX_MAX_CHARS,
    capture_output_fields,
    resolve_observation_ref,
)
from loom.core.memory.resolvers import (
    ACTION_UNOBSERVABLE_RESOLVERS,
    resolve,
)


@pytest_asyncio.fixture
async def store(tmp_path):
    s = SQLiteStore(str(tmp_path / "test_semantic_surface.db"))
    await s.initialize()
    return s


@pytest_asyncio.fixture
async def db(store):
    async with store.connect() as conn:
        yield conn


async def _insert_action(db, *, id="a1", tool_name="run_bash", call_id="c1",
                         session_id="sess", created_at="2026-09-06T00:00:00+00:00",
                         capture=None):
    """Insert a terminal (memorialized, executed) action row.

    ``capture`` None mimics a legacy row — capture columns stay NULL.
    """
    history = json.dumps([
        {"from": "declared", "to": "executing", "ts": created_at},
        {"from": "executing", "to": "memorialized", "ts": created_at},
    ])
    cols = ("id, envelope_id, session_id, turn_index, tool_name, call_id, "
            "final_state, duration_ms, state_history, created_at")
    vals = [id, "env", session_id, 0, tool_name, call_id,
            "memorialized", 42.0, history, created_at]
    if capture is not None:
        cols += ", output_prefix, output_digest, output_len, output_rows"
        vals += [capture["output_prefix"], capture["output_digest"],
                 capture["output_len"], capture["output_rows"]]
    await db.execute(
        f"INSERT INTO action_records ({cols}) VALUES ({','.join('?' * len(vals))})",
        vals,
    )
    await db.commit()


# ---------------------------------------------------------------------------
# Capture projection — pure world function, no narrative
# ---------------------------------------------------------------------------

class TestCaptureOutputFields:
    def test_success_string_is_captured_whole_when_small(self):
        f = capture_output_fields("PASS: 3 tests\nok", success=True, error=None)
        assert f["output_prefix"] == "PASS: 3 tests\nok"
        assert f["output_len"] == len("PASS: 3 tests\nok")
        assert f["output_rows"] == 2
        assert len(f["output_digest"]) == 64  # sha256 hex

    def test_failure_captures_the_error_text(self):
        """The error string is also world-generated (e.g. a 429 body) — a bet
        like «下一次會遇到 429» must be able to settle against it."""
        f = capture_output_fields("ignored", success=False, error="HTTP 429 rate limited")
        assert f["output_prefix"] == "HTTP 429 rate limited"
        assert f["output_rows"] == 1

    def test_success_none_output_is_empty_not_the_string_None(self):
        f = capture_output_fields(None, success=True, error=None)
        assert f["output_prefix"] == ""
        assert f["output_len"] == 0
        assert f["output_rows"] == 0

    def test_list_output_rows_is_element_count(self):
        f = capture_output_fields(["r1", "r2", "r3"], success=True, error=None)
        assert f["output_rows"] == 3

    def test_long_output_prefix_capped_digest_of_full_text(self):
        big = "x" * (OUTPUT_PREFIX_MAX_CHARS + 100)
        f = capture_output_fields(big, success=True, error=None)
        assert len(f["output_prefix"]) == OUTPUT_PREFIX_MAX_CHARS
        assert f["output_len"] == len(big)
        # digest covers the FULL text — auditability of the truncated capture
        small = capture_output_fields(big[:OUTPUT_PREFIX_MAX_CHARS], success=True, error=None)
        assert f["output_digest"] != small["output_digest"]


# ---------------------------------------------------------------------------
# Observation widening — resolve_observation_ref
# ---------------------------------------------------------------------------

class TestActionObservationOutputFields:
    async def test_new_row_exposes_output_fields(self, db):
        cap = capture_output_fields("deploy ok: PASS", success=True, error=None)
        await _insert_action(db, id="a-new", capture=cap)
        obs = await resolve_observation_ref(db, "action:a-new")
        assert obs["output"] == "deploy ok: PASS"
        assert obs["output_truncated"] is False
        assert obs["row_count"] == 1

    async def test_legacy_row_omits_output_fields(self, db):
        """A pre-#569 row must NOT grow fake fields — semantic resolvers stay
        unresolvable against it (no retro-scoring, §9.1)."""
        await _insert_action(db, id="a-old", capture=None)
        obs = await resolve_observation_ref(db, "action:a-old")
        assert "output" not in obs
        assert "row_count" not in obs
        with pytest.raises(KeyError):
            resolve({"kind": "output_contains", "needle": "PASS"}, obs)

    async def test_truncated_row_flags_truncation(self, db):
        big = "y" * (OUTPUT_PREFIX_MAX_CHARS + 5)
        cap = capture_output_fields(big, success=True, error=None)
        await _insert_action(db, id="a-big", capture=cap)
        obs = await resolve_observation_ref(db, "action:a-big")
        assert obs["output_truncated"] is True
        assert len(obs["output"]) == OUTPUT_PREFIX_MAX_CHARS


# ---------------------------------------------------------------------------
# Truncation semantics — miss on a truncated capture is unresolvable
# ---------------------------------------------------------------------------

class TestTruncationSemantics:
    def test_hit_in_prefix_resolves_even_when_truncated(self):
        obs = {"output": "PASS and more", "output_truncated": True}
        r = resolve({"kind": "output_contains", "needle": "PASS"}, obs)
        assert r.matched is True

    def test_miss_on_truncated_capture_is_unresolvable(self):
        obs = {"output": "prefix only", "output_truncated": True}
        with pytest.raises(KeyError):
            resolve({"kind": "output_contains", "needle": "EPILOGUE"}, obs)

    def test_miss_on_complete_capture_is_a_real_absence(self):
        obs = {"output": "no such marker", "output_truncated": False}
        r = resolve({"kind": "output_contains", "needle": "PASS"}, obs)
        assert r.matched is False
        assert r.error_score == 1.0

    def test_regex_miss_on_truncated_capture_is_unresolvable(self):
        obs = {"output": "prefix only", "output_truncated": True}
        with pytest.raises(KeyError):
            resolve({"kind": "output_regex", "pattern": r"triples?: [1-9]"}, obs)

    def test_regex_hit_in_prefix_resolves(self):
        obs = {"output": "parsed triples: 3", "output_truncated": True}
        r = resolve({"kind": "output_regex", "pattern": r"triples: [1-9]"}, obs)
        assert r.matched is True

    def test_expect_false_contains_needle_absent_but_truncated_still_unresolvable(self):
        """«輸出不含 X» on a truncated capture: absence is unprovable either way."""
        obs = {"output": "prefix only", "output_truncated": True}
        with pytest.raises(KeyError):
            resolve({"kind": "output_contains", "needle": "X-marker", "expect": False}, obs)

    def test_row_count_reads_captured_rows(self):
        r = resolve({"kind": "row_count", "expect": 2}, {"row_count": 2})
        assert r.matched is True


# ---------------------------------------------------------------------------
# Write-time refusal — no bet may rot against an uncaptured field (Q3)
# ---------------------------------------------------------------------------

class TestPredictToolRefusesUnobservable:
    def test_file_digest_changed_is_declared_unobservable(self):
        assert "file_digest_changed" in ACTION_UNOBSERVABLE_RESOLVERS

    async def test_predict_refuses_file_digest_changed(self, db):
        from loom.core.memory.maintenance import make_predict_tool
        from loom.core.harness.middleware import ToolCall
        from loom.core.harness.permissions import TrustLevel

        tool = make_predict_tool(db=db)
        call = ToolCall(
            id="t1", tool_name="predict", session_id="sess",
            trust_level=TrustLevel.SAFE,
            args={
                "claim": "the next write_file changes the file",
                "tool": "write_file",
                "resolver": {"kind": "file_digest_changed", "expect": True},
            },
        )
        result = await tool.executor(call)
        assert result.success is False
        # The refusal must steer, not just slam the door (壞錯誤訊息會趕走 agent)
        assert "output_contains" in result.output

    async def test_predict_still_accepts_output_contains(self, db):
        from loom.core.memory.maintenance import make_predict_tool
        from loom.core.harness.middleware import ToolCall
        from loom.core.harness.permissions import TrustLevel

        tool = make_predict_tool(db=db)
        call = ToolCall(
            id="t2", tool_name="predict", session_id="sess",
            trust_level=TrustLevel.SAFE,
            args={
                "claim": "next run_bash prints PASS",
                "tool": "run_bash",
                "resolver": {"kind": "output_contains", "needle": "PASS"},
            },
        )
        result = await tool.executor(call)
        assert result.success is True


# ---------------------------------------------------------------------------
# End-to-end — the 絲絲 scenario settles (was: starved forever)
# ---------------------------------------------------------------------------

class TestExplicitSemanticBetSettles:
    async def test_output_contains_bet_reconciles_against_captured_action(self, db):
        from loom.core.memory.prediction import PredictionRecord, PredictionStore
        from loom.core.cognition.prediction_reconcile import (
            run_prediction_reconciliation,
        )

        pstore = PredictionStore(db)
        bet = PredictionRecord(
            session_id="sess",
            claim="dream_cycle parses at least one triple",
            due_condition={
                "kind": "next_action", "session_id": "sess",
                "tool": "dream_cycle", "after": "2026-09-06T00:00:00+00:00",
            },
            resolver={"kind": "output_contains", "needle": "triples: 1"},
            domain="dream_cycle",
            context="explicit:predict_tool",
        )
        await pstore.write(bet)

        cap = capture_output_fields(
            "dream done — parsed triples: 1", success=True, error=None
        )
        await _insert_action(
            db, id="a-dream", tool_name="dream_cycle", call_id="c9",
            created_at="2026-09-06T01:00:00+00:00", capture=cap,
        )

        report = await run_prediction_reconciliation(pstore, db, execute=True)
        assert report.settleable == 1
        p = report.proposals[0]
        assert p.resolver_kind == "output_contains"
        assert p.matched is True
        assert p.observation_ref == "action:a-dream"
        assert p.provenance == "explicit"

    async def test_legacy_action_row_leaves_bet_unresolvable(self, db):
        """The 12 starved production bets: settling row exists but carries no
        capture → skip 'unresolvable', never a silent score."""
        from loom.core.memory.prediction import PredictionRecord, PredictionStore
        from loom.core.cognition.prediction_reconcile import (
            run_prediction_reconciliation,
        )

        pstore = PredictionStore(db)
        await pstore.write(PredictionRecord(
            session_id="sess",
            claim="fetch contains Opus",
            due_condition={
                "kind": "next_action", "session_id": "sess",
                "tool": "fetch_url", "after": "2026-09-06T00:00:00+00:00",
            },
            resolver={"kind": "output_contains", "needle": "Opus"},
            domain="fetch_url",
            context="explicit:predict_tool",
        ))
        await _insert_action(
            db, id="a-legacy", tool_name="fetch_url", call_id="c2",
            created_at="2026-09-06T01:00:00+00:00", capture=None,
        )

        report = await run_prediction_reconciliation(pstore, db, execute=False)
        assert report.settleable == 0
        assert [s.reason for s in report.skipped] == ["unresolvable"]
