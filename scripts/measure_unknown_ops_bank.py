"""Record GPT-6 Luna's replies for the #704 S3 unknown-operation bank.

One live attempt per case on the ChatGPT plan (``plan_model_chat``), with
the shipped prompt and the real packet. Writes
``tests/fixtures/s3_unknown_ops_bank_luna.json``, which the bank test
replays offline, and prints the hit rate. No ledger and no daily review
budget are touched.

    uv run python scripts/measure_unknown_ops_bank.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from loom.core.harness.approval import REVIEW_MODEL, plan_model_chat  # noqa: E402
from tests.test_unknown_ops_bank_704_s3 import CASES, LUNA_PATH, decide_case
from tests.test_delegation_bank_704_s2 import prompt_sha  # noqa: E402


class _Tap:
    """Wraps the live chat and keeps the raw reply text."""

    def __init__(self, chat):
        self.chat, self.text = chat, None

    async def __call__(self, messages):
        response = await self.chat(messages)
        self.text = getattr(response, "text", None)
        return response


async def main() -> None:
    chat = plan_model_chat()
    replies, hits = {}, 0
    for case in CASES:
        tap = _Tap(chat)
        with tempfile.TemporaryDirectory() as tmp:
            reason, ok = await decide_case(case, Path(tmp), tap)
        replies[case["id"]] = tap.text if tap.text is not None else f"<no reply: {reason}>"
        hit = ok == (case["expected"] == "accept")
        hits += hit
        print(f"{case['id']} expected={case['expected']:7} got={'accept' if ok else 'abstain':7} "
              f"{'ok' if hit else 'MISS'}  ({reason})")
    LUNA_PATH.write_text(json.dumps({
        "model": REVIEW_MODEL, "prompt_sha256": prompt_sha(),
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "attempts": 1, "replies": replies,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"hit rate: {hits}/{len(CASES)}")


if __name__ == "__main__":
    asyncio.run(main())
