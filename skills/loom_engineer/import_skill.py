#!/usr/bin/env python3
"""
一次性腳本：將 loom_engineer SKILL.md 寫入 ProceduralMemory。
執行一次即可，不需要每次 session 都跑。
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[2]))

from loom.core.memory.store import SQLiteStore
from loom.core.memory.procedural import ProceduralMemory, SkillGenome


SKILL_DIR = Path(__file__).parent


async def main() -> None:
    skill_markdown = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")

    genome = SkillGenome(
        name="loom_engineer",
        body=skill_markdown,
        version=1,
        confidence=0.8,
        tags=["coding", "implementation", "pr", "fix", "debug", "review"],
    )

    db_path = Path.home() / ".loom" / "memory.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    store = SQLiteStore(str(db_path))
    await store.initialize()
    async with store.connect() as conn:
        procedural = ProceduralMemory(conn)
        await procedural.upsert(genome)
        print(f"[OK] Skill 'loom_engineer' upserted (confidence={genome.confidence})")
    print(f"      DB: {db_path}")


if __name__ == "__main__":
    asyncio.run(main())
