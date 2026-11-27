"""Only reusable public skill sources are promoted, never private trees."""
from pathlib import Path

from loom.autonomy.skill_allows import read_skill_allows


def test_housekeeping_is_exact_reviewed_s2b_template_and_loads_in_scratch(tmp_path):
    repo = Path(__file__).resolve().parents[1]
    tracked = repo / "skills/memory-housekeeping/SKILL.md"
    template = repo / "docs/examples/708-s2b/memory-housekeeping.SKILL.md"
    assert tracked.read_bytes() == template.read_bytes()
    target = tmp_path / "skills/memory-housekeeping/SKILL.md"
    target.parent.mkdir(parents=True)
    target.write_bytes(tracked.read_bytes())
    allows = read_skill_allows("memory-housekeeping", tmp_path)
    assert allows.write == ("outputs/self_check",) and not allows.exec and not allows.tools
