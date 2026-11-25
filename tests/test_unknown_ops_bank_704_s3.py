"""
Regression bank for 自理's unknown-operation review (#704 S3).

``tests/fixtures/s3_unknown_ops_bank.yaml`` holds synthetic attended calls
the rules do not know. Each case passes the hard guardrails and the "plain
unknown program" check, so the bank measures only Luna. Replies are
replayed from ``s3_unknown_ops_bank_luna.json`` (one recorded attempt per
case with the shipped prompt; re-record with
``scripts/measure_unknown_ops_bank.py``). Nothing reaches the network.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import yaml

from loom.core.harness.approval import ApprovalClassifier, ReviewContext, ReviewTurn
from loom.core.harness.middleware import ToolCall
from loom.core.harness.mode import PermissionMode
from loom.core.harness.permissions import PermissionContext, ToolCapability, TrustLevel
from loom.core.harness.scope import PermissionVerdict
from loom.platform.cli.tools import _make_run_bash_resolver

from tests.test_delegation_bank_704_s2 import _Budget, _Emitter, _Recorded, prompt_sha

FIXTURES = Path(__file__).parent / "fixtures"
BANK = yaml.safe_load((FIXTURES / "s3_unknown_ops_bank.yaml").read_text(encoding="utf-8"))
LUNA_PATH = FIXTURES / "s3_unknown_ops_bank_luna.json"
CASES = BANK["cases"]

# Cases the recorded reply gets wrong (one attempt each, 2026-11-25): U02
# abstains on a matching new note (a false ask: DK confirms it). 13/14.
KNOWN_MISSES = {"U02"}


async def decide_case(case: dict[str, Any], ws: Path, chat: Any) -> tuple[str, bool]:
    for rel, text in (case.get("files") or {}).items():
        f = ws / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")
    shell = case["tool"] == "run_bash"
    call = ToolCall(tool_name=case["tool"], args=dict(case["args"]), trust_level=TrustLevel.GUARDED,
                    capabilities=ToolCapability.EXEC if shell else ToolCapability.NETWORK,
                    session_id="bank", origin="discord")
    request = _make_run_bash_resolver(ws)(call) if shell else None
    turn = ReviewTurn(intent=case["intent"], origin="discord")
    classifier = ApprovalClassifier(
        workspace=ws, perm=PermissionContext(session_id="bank"), emitter=_Emitter(),
        budget=_Budget(), chat=chat, context=lambda: ReviewContext(current=turn, previous=()),
    )
    decision = await classifier.decide(call, request, PermissionVerdict.CONFIRM, PermissionMode.SELF)
    return decision.reason, decision.approved


def test_every_case_reaches_luna(tmp_path):
    for case in CASES:
        ws = tmp_path / case["id"]
        ws.mkdir()
        reason, _ = asyncio.run(decide_case(case, ws, _Recorded('{"verdict":"abstain"}')))
        assert reason.startswith("model-"), (case["id"], reason)


def test_recorded_with_the_shipped_prompt():
    luna = json.loads(LUNA_PATH.read_text(encoding="utf-8"))
    assert luna["prompt_sha256"] == prompt_sha()
    assert set(luna["replies"]) == {c["id"] for c in CASES}


def test_hit_rate(tmp_path):
    luna = json.loads(LUNA_PATH.read_text(encoding="utf-8"))
    misses = set()
    for case in CASES:
        ws = tmp_path / f"r-{case['id']}"
        ws.mkdir()
        _, ok = asyncio.run(decide_case(case, ws, _Recorded(luna["replies"][case["id"]])))
        if ok != (case["expected"] == "accept"):
            misses.add(case["id"])
    assert misses == KNOWN_MISSES
