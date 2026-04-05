#!/usr/bin/env python3
"""
一次性腳本：將 systematic_code_analyst SKILL.md 寫入 ProceduralMemory。
如果技能已存在，保留現有 confidence 不覆蓋。
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

    db_path = Path.home() / ".loom" / "memory.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    store = SQLiteStore(str(db_path))
    await store.initialize()

    async with store.connect() as conn:
        procedural = ProceduralMemory(conn)

        existing = await procedural.get("systematic_code_analyst")

        genome = SkillGenome(
            name="systematic_code_analyst",
            body=skill_markdown,
            version=existing.version if existing else 1,
            confidence=existing.confidence if existing else 0.8,
            tags=["analysis", "code-review", "architecture", "open-source"],
        )

        await procedural.upsert(genome)

        final = await procedural.get("systematic_code_analyst")
        print(f"[OK] Skill 'systematic_code_analyst' upserted")
        print(f"      confidence={final.confidence:.2f} (preserved from existing={bool(existing)})")
        print(f"      version={final.version}")
        print(f"      DB: {db_path}")


if __name__ == "__main__":
    asyncio.run(main())
