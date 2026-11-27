"""#708 S1 (喵吉直切): a skill's ``allows`` is the one boundary of its activity.

DK's plan (2026-11-27): SKILL.md frontmatter carries an optional ``allows``
listing only the skill's own things (exec shapes, write paths, extra tools).
Furniture points at a skill with ``skill = "<name>"`` and carries no grants of
its own; at wake time the boundary comes from that skill's ``allows``.

Invariants written first:
- ``allows`` translates into the existing allowed_tools / scope_grants forms,
  through ``parse_permission_fields``; the harness needs no change.
- ``allows`` is part of the skill's guard (#704 S3): changing it is a
  ``skill_guard`` hard guardrail, whatever the mode.
- furniture may narrow its skill's boundary, never widen it.
- a missing skill, a missing ``allows`` or a missing script rejects the
  furniture at load, loudly (an ERROR and a room-log row), never a quiet skip.
- Loom Agent's scenarios: the boundary is on the chime before the turn
  starts (no load-the-skill-to-get-permission loop); loading a skill in an
  attended turn grants nothing; one ``allows`` covers judgement-based care
  (status, then play / feed / leave her be) without one furniture per
  reaction.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from loom.autonomy.circadian import room
from loom.autonomy.circadian import state as st
from loom.autonomy.circadian.lifecycle import CircadianConfig
from loom.autonomy.circadian.room import load_room, parse_furniture_output, room_tick
from loom.autonomy.skill_allows import (
    SkillAllows,
    SkillAllowsError,
    allows_permission_fields,
    read_skill_allows,
)
from loom.core.harness.mode import PermissionMode
from loom.core.harness.permissions import ToolCapability
from loom.core.harness.scope import ScopeGrant
from tests.test_approval_classifier_690_s6 import (  # noqa: F401  (fixtures)
    _no_network,
    ledger,
)
from tests.test_permission_mode_704 import _env
from tests.test_single_decision_704_s3 import _call

PET = "python3 skills/pet-cat/pet.py"
CARE = ("status", "watch", "journal", "feed", "play", "clean")

PET_SKILL = (
    "---\n"
    "name: pet-cat\n"
    "precondition_checks:\n"
    "  - ref: checks.restrict_to_pet_script\n"
    "    applies_to: [run_bash]\n"
    "allows:\n"
    "  exec:\n"
    + "".join(f"    - {PET} {sub}\n" for sub in CARE)
    + "  tools: [memorize]\n"
    "---\n# Pet-Cat\n本文。\n"
)


def _pet_skill(ws: Path, text: str = PET_SKILL, *, script: bool = True) -> Path:
    d = ws / "skills/pet-cat"
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(text, encoding="utf-8")
    (d / "checks.py").write_text("async def restrict_to_pet_script(call):\n    return True\n")
    if script:
        (d / "pet.py").write_text("print('{}')\n")
    return d


def _room(ws: Path, furniture: str) -> Path:
    p = ws / "autonomy/circadian/room.toml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("[room]\nenabled = true\n\n" + furniture, encoding="utf-8")
    return p


MIJI = (
    '[[furniture]]\nname = "miji"\nlabel = "喵吉"\nskill = "pet-cat"\n'
    f'command = ["python3", "skills/pet-cat/pet.py", "peek", "--json"]\n'
)


# ---------------------------------------------------------------------------
# allows → existing permission forms
# ---------------------------------------------------------------------------


class TestAllowsParsing:
    def test_reads_exec_write_and_tools(self, tmp_path):
        _pet_skill(tmp_path, PET_SKILL.replace(
            "  tools: [memorize]\n", "  write:\n    - skills/pet-cat/notes\n  tools: [memorize]\n"))
        allows = read_skill_allows("pet-cat", tmp_path)
        assert allows == SkillAllows(
            exec=tuple(f"{PET} {s}" for s in CARE),
            write=("skills/pet-cat/notes",),
            tools=("memorize",),
        )

    def test_translates_into_the_existing_forms(self, tmp_path):
        _pet_skill(tmp_path)
        fields = allows_permission_fields(read_skill_allows("pet-cat", tmp_path), "skill pet-cat")
        assert fields["allowed_tools"] == ["memorize"]
        assert fields["scope_grants"] == [
            {"resource": "exec", "action": "execute", "selector": f"{PET} {s}"} for s in CARE
        ]

    def test_write_paths_become_path_write_grants(self, tmp_path):
        _pet_skill(tmp_path, PET_SKILL.replace("  tools: [memorize]\n",
                                               "  write: [skills/pet-cat/notes]\n"))
        fields = allows_permission_fields(read_skill_allows("pet-cat", tmp_path), "s")
        assert {"resource": "path", "action": "write",
                "selector": "skills/pet-cat/notes"} in fields["scope_grants"]

    def test_empty_allows_means_nothing(self, tmp_path):
        _pet_skill(tmp_path, "---\nname: pet-cat\nallows: {}\n---\n")
        fields = allows_permission_fields(read_skill_allows("pet-cat", tmp_path), "s")
        assert fields == {"trust_level": None, "allowed_tools": [], "scope_grants": []}

    @pytest.mark.parametrize("text,why", [
        ("---\nname: pet-cat\n---\n", "allows"),                      # nothing declared
        ("---\nname: pet-cat\nallows: [x]\n---\n", "allows"),         # not a table
        ("---\nname: pet-cat\nallows:\n  exec: [x]\n  grants: [y]\n---\n", "grants"),
        ("---\nname: pet-cat\nallows:\n  exec: 'python3 x'\n---\n", "exec"),
        # Exact command prefixes only: no wildcards, no shell.
        (f"---\nname: pet-cat\nallows:\n  exec: ['{PET} *']\n---\n", "*"),
        (f"---\nname: pet-cat\nallows:\n  exec: ['{PET} status && rm x']\n---\n", "&&"),
        # A shape must run one of this skill's own scripts.
        ("---\nname: pet-cat\nallows:\n  exec: ['git push']\n---\n", "git push"),
        ("---\nname: pet-cat\nallows:\n  exec: ['python3 skills/other/run.py']\n---\n",
         "skills/other/run.py"),
        # Write paths are plain workspace prefixes.
        ("---\nname: pet-cat\nallows:\n  write: ['/etc']\n---\n", "/etc"),
        ("---\nname: pet-cat\nallows:\n  write: ['../x']\n---\n", "../x"),
        ("---\nname: pet-cat\nallows:\n  write: ['.']\n---\n", "."),
        ("---\nname: pet-cat\nallows:\n  write: ['news/*']\n---\n", "news/*"),
        ("---\nname: pet-cat\nallows:\n  tools: ['*']\n---\n", "*"),
    ])
    def test_bad_allows_is_an_error(self, tmp_path, text, why):
        _pet_skill(tmp_path, text)
        with pytest.raises(SkillAllowsError, match=__import__("re").escape(why)):
            read_skill_allows("pet-cat", tmp_path)

    def test_missing_skill_is_an_error(self, tmp_path):
        with pytest.raises(SkillAllowsError, match="pet-cat"):
            read_skill_allows("pet-cat", tmp_path)

    def test_missing_script_is_an_error(self, tmp_path):
        _pet_skill(tmp_path, script=False)
        with pytest.raises(SkillAllowsError, match="skills/pet-cat/pet.py"):
            read_skill_allows("pet-cat", tmp_path)

    @pytest.mark.parametrize("name", ["../pet-cat", "pet-cat/..", "", "a/b"])
    def test_skill_name_is_one_directory(self, tmp_path, name):
        _pet_skill(tmp_path)
        with pytest.raises(SkillAllowsError):
            read_skill_allows(name, tmp_path)


# ---------------------------------------------------------------------------
# allows is part of the skill's guard (#704 S3)
# ---------------------------------------------------------------------------


class TestAllowsIsGuarded:
    def test_skill_guard_sees_an_allows_change(self, tmp_path):
        from pathlib import PurePosixPath

        from loom.core.harness.approval import _skill_guard
        _pet_skill(tmp_path)
        rel = PurePosixPath("skills/pet-cat/SKILL.md")
        widened = PET_SKILL.replace("  tools: [memorize]\n", "  tools: [memorize, run_bash]\n")
        added = PET_SKILL.replace("allows:\n", "allows:\n  write: [news]\n")
        removed = PET_SKILL.split("allows:\n")[0] + "---\n# Pet-Cat\n本文。\n"
        for content in (widened, added, removed):
            assert _skill_guard(rel, tmp_path, content), content
        # The body is still hers.
        assert not _skill_guard(rel, tmp_path, PET_SKILL.replace("本文。", "新的本文。"))

    def test_adding_allows_to_a_skill_without_one_is_guarded(self, tmp_path):
        from pathlib import PurePosixPath

        from loom.core.harness.approval import _skill_guard
        bare = "---\nname: pet-cat\n---\n# Pet\n"
        _pet_skill(tmp_path, bare)
        assert _skill_guard(PurePosixPath("skills/pet-cat/SKILL.md"), tmp_path,
                            bare.replace("---\n# Pet", f"allows:\n  exec: ['{PET} feed']\n---\n# Pet"))

    @pytest.mark.parametrize("mode", list(PermissionMode))
    async def test_allows_edit_asks_dk_in_every_mode(self, tmp_path, ledger, mode):
        env = _env(tmp_path, ledger, mode=mode)
        _pet_skill(env.ws)
        widened = PET_SKILL.replace("  tools: [memorize]\n", "  tools: [memorize, fetch_url]\n")
        await env.run("write_file", {"path": "skills/pet-cat/SKILL.md", "content": widened},
                      origin="discord")
        assert env.confirm.await_count == 1
        if mode is PermissionMode.YOLO:
            assert env.confirm.await_args.args[0].metadata["classifier_note"] == "hard: skill_guard"


# ---------------------------------------------------------------------------
# Furniture → skill
# ---------------------------------------------------------------------------


class TestFurnitureSkill:
    def test_boundary_comes_from_the_skill(self, tmp_path):
        _pet_skill(tmp_path)
        cfg = load_room(_room(tmp_path, MIJI), workspace=tmp_path)
        (miji,) = cfg.furniture
        assert miji.skill == "pet-cat"
        assert miji.allowed_tools == ("memorize",)
        assert miji.scope_grants == tuple(
            {"resource": "exec", "action": "execute", "selector": f"{PET} {s}"} for s in CARE)
        assert cfg.errors == ()

    def test_furniture_may_narrow(self, tmp_path):
        _pet_skill(tmp_path)
        narrow = MIJI + (
            'allowed_tools = []\n'
            f'scope_grants = [{{ resource = "exec", action = "execute", selector = "{PET} status" }},'
            f' {{ resource = "exec", action = "execute", selector = "{PET} feed extra" }}]\n')
        (miji,) = load_room(_room(tmp_path, narrow), workspace=tmp_path).furniture
        assert miji.allowed_tools == ()
        assert [g["selector"] for g in miji.scope_grants] == [f"{PET} status", f"{PET} feed extra"]

    @pytest.mark.parametrize("extra,named", [
        ('allowed_tools = ["memorize", "fetch_url"]\n', "fetch_url"),
        (f'scope_grants = [{{ resource = "exec", action = "execute", selector = "{PET}" }}]\n', PET),
        (f'scope_grants = [{{ resource = "exec", action = "execute", selector = "{PET} buy" }}]\n',
         f"{PET} buy"),
        ('scope_grants = [{ resource = "path", action = "write", selector = "news" }]\n', "news"),
        ('scope_grants = [{ resource = "exec", action = "execute", selector = "*" }]\n', "*"),
    ])
    def test_furniture_cannot_widen(self, tmp_path, caplog, extra, named):
        _pet_skill(tmp_path)
        with caplog.at_level(logging.ERROR):
            cfg = load_room(_room(tmp_path, MIJI + extra), workspace=tmp_path)
        assert cfg.furniture == ()
        assert any("miji" in e and named in e for e in cfg.errors), cfg.errors

    def test_write_narrowing_stays_under_the_skill_path(self, tmp_path):
        _pet_skill(tmp_path, PET_SKILL.replace("  tools: [memorize]\n",
                                               "  write: [skills/pet-cat/notes]\n"))
        ok = MIJI + ('scope_grants = [{ resource = "path", action = "write",'
                     ' selector = "skills/pet-cat/notes/today" }]\n')
        assert load_room(_room(tmp_path, ok), workspace=tmp_path).furniture
        bad = MIJI + ('scope_grants = [{ resource = "path", action = "write",'
                      ' selector = "skills/pet-cat/notes/../../../news" }]\n')
        assert load_room(_room(tmp_path, bad), workspace=tmp_path).furniture == ()

    def test_hand_grants_without_a_skill_are_rejected(self, tmp_path):
        """家具自己不帶授權: a grant belongs in the skill's allows."""
        legacy = ('[[furniture]]\nname = "miji"\ncommand = ["python3", "skills/pet-cat/pet.py", "peek"]\n'
                  'allowed_tools = ["run_bash"]\n'
                  f'scope_grants = [{{ resource = "exec", action = "execute", selector = "{PET}" }}]\n')
        cfg = load_room(_room(tmp_path, legacy), workspace=tmp_path)
        assert cfg.furniture == ()
        assert any("miji" in e and "skill" in e for e in cfg.errors)

    def test_peek_only_furniture_needs_no_skill(self, tmp_path):
        plain = '[[furniture]]\nname = "clock"\ncommand = ["true"]\n'
        (f,) = load_room(_room(tmp_path, plain), workspace=tmp_path).furniture
        assert (f.skill, f.allowed_tools, f.scope_grants) == (None, (), ())

    @pytest.mark.parametrize("setup,named", [
        (lambda ws: None, "pet-cat"),                                        # no such skill
        (lambda ws: _pet_skill(ws, script=False), "skills/pet-cat/pet.py"),  # script gone
        (lambda ws: _pet_skill(ws, "---\nname: pet-cat\n---\n"), "allows"),  # nothing declared
    ])
    def test_broken_reference_is_rejected_loudly(self, tmp_path, caplog, setup, named):
        setup(tmp_path)
        other = '\n[[furniture]]\nname = "clock"\ncommand = ["true"]\n'
        cfg = load_room(_room(tmp_path, MIJI + other), workspace=tmp_path)
        # The broken one is rejected with a reason; the rest of the room lives.
        assert [f.name for f in cfg.furniture] == ["clock"]
        assert any("miji" in e and named in e for e in cfg.errors), cfg.errors
        with caplog.at_level(logging.ERROR):
            room.register_room(_Daemon(), CIRC, _room(tmp_path, MIJI + other))
        assert any(r.levelno >= logging.ERROR and "miji" in r.getMessage() for r in caplog.records)

    def test_skill_name_must_be_a_string(self, tmp_path):
        cfg = load_room(_room(tmp_path, MIJI.replace('skill = "pet-cat"', "skill = 3")),
                        workspace=tmp_path)
        assert cfg.furniture == () and any("miji" in e for e in cfg.errors)

    def test_example_room_toml_points_miji_at_pet_cat(self, tmp_path):
        repo = Path(__file__).resolve().parent.parent
        example = repo / "autonomy/circadian/room.example.toml"
        _pet_skill(tmp_path)
        cfg = load_room(example, workspace=tmp_path)
        (miji,) = cfg.furniture
        assert miji.skill == "pet-cat" and cfg.errors == ()
        import tomllib
        entry = tomllib.loads(example.read_text(encoding="utf-8"))["furniture"][0]
        assert "scope_grants" not in entry and "allowed_tools" not in entry


# ---------------------------------------------------------------------------
# Wake: the boundary rides on the chime (scenario a) and covers care (c)
# ---------------------------------------------------------------------------

TZ = "Asia/Taipei"
CIRC = CircadianConfig(enabled=True, timezone=TZ, start="08:00", sleep="00:00")


class _Daemon:
    def __init__(self):
        self.chimes: list = []
        self.evaluator = self
        self.handlers: dict = {}

    async def deliver_chime(self, req):
        self.chimes.append(req)
        return True

    def register(self, trigger):
        pass

    def register_direct_handler(self, name, fn):
        self.handlers[name] = fn

    async def emit(self, name, ctx):
        pass


@pytest.fixture
def circ_dir(tmp_path):
    st.set_dir_for_test(tmp_path / "circadian")
    yield tmp_path / "circadian"
    st.set_dir_for_test(None)


async def _wake(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(room, "_awake", lambda *a: True)
    cfg = load_room(_room(tmp_path, MIJI), workspace=tmp_path)
    d = _Daemon()

    async def runner(f, now):
        return parse_furniture_output(f, json.dumps(
            {"signals": [{"id": "seeking", "key": "k1", "text": "她用頭撞你的手。"}]}))

    now = datetime(2026, 11, 27, 2, 0, tzinfo=timezone.utc)
    await room_tick(d, CIRC, now, config=cfg, runner=runner)
    (req,) = d.chimes
    return req


async def test_wake_carries_the_skill_boundary_before_the_turn(tmp_path, monkeypatch, circ_dir):
    """Scenario a: the boundary is derived from frontmatter at wake start and
    travels on the chime, so reading the skill never needs what it grants."""
    _pet_skill(tmp_path)
    req = await _wake(tmp_path, monkeypatch)
    assert req.allowed_tools == ("memorize",)
    assert {g["selector"] for g in req.scope_grants} == {f"{PET} {s}" for s in CARE}


async def test_rejection_is_written_to_the_room_log(tmp_path, monkeypatch, circ_dir):
    monkeypatch.chdir(tmp_path)
    room._reported_errors.clear()
    cfg = load_room(_room(tmp_path, MIJI + '\n[[furniture]]\nname = "c"\ncommand = ["true"]\n'),
                    workspace=tmp_path)

    async def runner(f, now):
        return [], None

    now = datetime(2026, 11, 27, 2, 0, tzinfo=timezone.utc)
    await room_tick(_Daemon(), CIRC, now, config=cfg, runner=runner)
    await room_tick(_Daemon(), CIRC, now + timedelta(minutes=5), config=cfg, runner=runner)
    rows = [json.loads(line) for p in (circ_dir / "log").glob("room-*.jsonl")
            for line in p.read_text().splitlines()]
    rejected = [r for r in rows if r["event"] == "furniture_rejected"]
    assert len(rejected) == 1 and "miji" in rejected[0]["error"]  # loud, not every tick


@pytest.mark.parametrize("command,runs", [
    *[(f"{PET} {s}", True) for s in CARE],          # status first, then her choice
    (f"{PET} play --gentle", True),                  # a shape is a prefix
    (f"{PET} buy bowl_refill_pack", False),          # not in this allows
    (f"{PET}", False),                               # the bare script is not a shape here
    ("python3 skills/pet-cat/other.py", False),
    (f"{PET} status && rm -rf skills", False),
])
async def test_judgement_based_care_runs_inside_one_allows(
        tmp_path, ledger, monkeypatch, circ_dir, command, runs):
    """Scenario c, over the real harness: the chime's grants (as the bot
    applies them) let one wake read her status and then play, feed or do
    nothing, and nothing past the allows."""
    env = _env(tmp_path, ledger, mode=PermissionMode.SELF)
    _pet_skill(env.ws)
    req = await _wake(env.ws, monkeypatch)
    source = f"autonomy:{req.schedule_name}"
    for g in req.scope_grants:
        env.perm.grant(ScopeGrant(resource=g["resource"], action=g["action"],
                                  selector=g["selector"], source=source))
    env.perm.declare(source, list(req.allowed_tools), unattended_only=True)
    r = await _call(env, "run_bash", {"command": command}, origin="chime", caps=ToolCapability.EXEC)
    assert r.success is runs


# ---------------------------------------------------------------------------
# Scenario b: attended conversation is not widened in this slice
# ---------------------------------------------------------------------------


def test_allows_is_read_only_at_furniture_wake():
    """Loading a skill (attended or not) never turns its allows into grants:
    only the room's wake path reads them in S1. S2 adds anchors and
    schedules through the same reader."""
    root = Path(__file__).resolve().parent.parent / "loom"
    users = sorted(
        str(p.relative_to(root)) for p in root.rglob("*.py")
        if "read_skill_allows" in p.read_text(encoding="utf-8")
        and p.name != "skill_allows.py"
    )
    assert users == ["autonomy/circadian/room.py"]


# ---------------------------------------------------------------------------
# #714 cross-review (克萊兒 · Antigravity): B1–B4, N1–N4
# ---------------------------------------------------------------------------


def _allows(ws: Path, body: str) -> None:
    _pet_skill(ws, f"---\nname: pet-cat\nallows:\n{body}---\n")


class TestB1GuardIgnoresCase:
    """APFS is case-insensitive: skill.md *is* SKILL.md there."""

    @pytest.mark.parametrize("rel", [
        "skills/pet-cat/skill.md", "skills/pet-cat/Skill.MD", "Skills/pet-cat/SKILL.md",
        "SKILLS/pet-cat/skill.md",
    ])
    def test_case_variants_of_skill_md_are_guarded(self, tmp_path, rel):
        from pathlib import PurePosixPath

        from loom.core.harness.approval import _skill_guard
        _pet_skill(tmp_path)
        widened = PET_SKILL.replace("  tools: [memorize]\n", "  tools: [memorize, fetch_url]\n")
        assert _skill_guard(PurePosixPath(rel), tmp_path, widened)
        assert _skill_guard(PurePosixPath(rel), tmp_path, None)

    @pytest.mark.parametrize("rel", ["skills/pet-cat/Checks.py", "skills/pet-cat/CHECKS.PY",
                                     "Skills/pet-cat/checks.py"])
    def test_case_variants_of_checks_are_guarded(self, tmp_path, rel):
        from pathlib import PurePosixPath

        from loom.core.harness.approval import _skill_guard
        _pet_skill(tmp_path)
        assert _skill_guard(PurePosixPath(rel), tmp_path, "x = 1\n")

    def test_case_variant_of_a_ref_module_is_guarded(self, tmp_path):
        from pathlib import PurePosixPath

        from loom.core.harness.approval import _skill_guard
        _pet_skill(tmp_path, PET_SKILL.replace("checks.restrict_to_pet_script",
                                               "guards.restrict_to_pet_script"))
        assert _skill_guard(PurePosixPath("skills/pet-cat/Guards.PY"), tmp_path, "x = 1\n")

    def test_shell_redirect_to_a_case_variant_is_hard(self, tmp_path):
        from loom.core.harness.approval import _shell_hard
        _pet_skill(tmp_path)
        assert _shell_hard("echo x > skills/pet-cat/skill.md", tmp_path) == "skill_guard"

    @pytest.mark.parametrize("mode", [PermissionMode.YOLO, PermissionMode.SELF])
    @pytest.mark.parametrize("origin", ["discord", "chime"])
    async def test_write_to_a_case_variant_asks_dk(self, tmp_path, ledger, mode, origin):
        env = _env(tmp_path, ledger, mode=mode)
        _pet_skill(env.ws)
        env.perm.declare("autonomy:room@0900", ["write_file"], unattended_only=True)
        widened = PET_SKILL.replace("  tools: [memorize]\n", "  tools: [memorize, fetch_url]\n")
        r = await env.run("write_file", {"path": "skills/pet-cat/skill.md", "content": widened},
                          origin=origin)
        assert not r.success or env.confirm.await_count == 1
        assert (env.ws / "skills/pet-cat/SKILL.md").read_text() == PET_SKILL


class TestB2ExecResolvesSymlinks:
    def test_symlink_to_outside_the_workspace_is_rejected(self, tmp_path):
        ws = tmp_path / "ws"
        _pet_skill(ws)
        outside = tmp_path / "outside.py"
        outside.write_text("print('x')\n")
        (ws / "skills/pet-cat/sym.py").symlink_to(outside)
        _allows(ws, "  exec: [python3 skills/pet-cat/sym.py]\n")
        with pytest.raises(SkillAllowsError, match="sym.py"):
            read_skill_allows("pet-cat", ws)

    def test_symlink_to_another_skill_is_rejected(self, tmp_path):
        _pet_skill(tmp_path)
        other = tmp_path / "skills/other"
        other.mkdir(parents=True)
        (other / "secret.py").write_text("")
        (tmp_path / "skills/pet-cat/sym.py").symlink_to("../other/secret.py")
        _allows(tmp_path, "  exec: [python3 skills/pet-cat/sym.py]\n")
        with pytest.raises(SkillAllowsError, match="sym.py"):
            read_skill_allows("pet-cat", tmp_path)

    def test_symlinked_directory_is_rejected(self, tmp_path):
        ws = tmp_path / "ws"
        _pet_skill(ws)
        out = tmp_path / "outdir"
        out.mkdir()
        (out / "run.py").write_text("")
        (ws / "skills/pet-cat/lib").symlink_to(out)
        _allows(ws, "  exec: [python3 skills/pet-cat/lib/run.py]\n")
        with pytest.raises(SkillAllowsError, match="lib/run.py"):
            read_skill_allows("pet-cat", ws)

    def test_a_symlinked_skill_directory_is_rejected(self, tmp_path):
        ws = tmp_path / "ws"
        elsewhere = tmp_path / "elsewhere"
        _pet_skill(elsewhere)  # a real skill tree outside the workspace
        (ws / "skills").mkdir(parents=True)
        (ws / "skills/pet-cat").symlink_to(elsewhere / "skills/pet-cat")
        with pytest.raises(SkillAllowsError, match="pet-cat"):
            read_skill_allows("pet-cat", ws)


class TestB3ExecPinsTheInterpreter:
    @pytest.mark.parametrize("shape", [
        'python3 -c "import os" skills/pet-cat/pet.py',
        "python3 -m http.server skills/pet-cat/pet.py",
        "bash -c evil skills/pet-cat/pet.py",
        "rm -rf skills/pet-cat/pet.py",
        "python3 -u skills/pet-cat/pet.py status",
        "/usr/bin/python3 skills/pet-cat/pet.py",
        "env python3 skills/pet-cat/pet.py",
        "node skills/pet-cat/pet.py",
        "python3",
    ])
    def test_dangerous_shapes_are_rejected(self, tmp_path, shape):
        _allows(tmp_path, f"  exec: ['{shape}']\n" if '"' in shape else f'  exec: ["{shape}"]\n')
        with pytest.raises(SkillAllowsError):
            read_skill_allows("pet-cat", tmp_path)

    @pytest.mark.parametrize("shape", [
        "python3 skills/pet-cat/pet.py status",
        "python skills/pet-cat/pet.py",
        "skills/pet-cat/pet.py feed",
    ])
    def test_interpreter_then_script_is_accepted(self, tmp_path, shape):
        _allows(tmp_path, f'  exec: ["{shape}"]\n')
        assert read_skill_allows("pet-cat", tmp_path).exec == (shape,)


class TestB4WriteStaysOffControlFiles:
    @pytest.mark.parametrize("prefix", [
        "skills", "skills/pet-cat", "skills/pet-cat/SKILL.md", "skills/pet-cat/checks.py",
        "skills/other", "Skills/pet-cat", "autonomy", "autonomy/circadian", "loom.toml",
        ".env", "notes/.private", "memory/care",
    ])
    def test_write_prefix_over_control_or_policy_is_rejected(self, tmp_path, prefix):
        care = tmp_path / "memory/care"
        care.mkdir(parents=True)
        (care / "room.toml").write_text("")
        (tmp_path / "skills/other").mkdir(parents=True)
        _allows(tmp_path, f'  write: ["{prefix}"]\n')
        with pytest.raises(SkillAllowsError, match="write"):
            read_skill_allows("pet-cat", tmp_path)

    def test_write_prefix_through_a_symlink_is_rejected(self, tmp_path):
        ws = tmp_path / "ws"
        _pet_skill(ws)
        (tmp_path / "out").mkdir()
        (ws / "notes").symlink_to(tmp_path / "out")
        _allows(ws, '  write: ["notes"]\n')
        with pytest.raises(SkillAllowsError, match="notes"):
            read_skill_allows("pet-cat", ws)

    @pytest.mark.parametrize("prefix", ["skills/pet-cat/notes", "news", "autonomy/circadian/journal"])
    def test_plain_content_prefixes_are_accepted(self, tmp_path, prefix):
        _allows(tmp_path, f'  write: ["{prefix}"]\n')
        assert read_skill_allows("pet-cat", tmp_path).write == (prefix,)

    @pytest.mark.parametrize("path,grant", [
        ("skills/pet-cat/SKILL.md", "skills/pet-cat"),
        ("autonomy/circadian/room.toml", "autonomy/circadian"),
        ("memory/care/room.toml", "memory/care"),
        ("memory/care/.env", "memory/care"),
    ])
    async def test_a_granted_write_still_meets_the_hard_boundary(self, tmp_path, ledger, path, grant):
        """Runtime half: a PV.ALLOW from a routine grant is a rule-allow too,
        so it passes the hard boundary (#704 S3)."""
        env = _env(tmp_path, ledger, mode=PermissionMode.SELF)
        _pet_skill(env.ws)
        (env.ws / "autonomy/circadian").mkdir(parents=True)
        (env.ws / "memory/care").mkdir(parents=True)
        env.perm.grant(ScopeGrant(resource="path", action="write", selector=grant,
                                  source="autonomy:room@0900"))
        r = await env.run("write_file", {"path": path, "content": "x = 1\n"}, origin="chime")
        assert not r.success
        assert env.confirm.await_count == 0  # unattended: skipped, not asked
        assert not (env.ws / path).exists() or (env.ws / path).read_text() != "x = 1\n"

    async def test_a_granted_plain_write_still_runs(self, tmp_path, ledger):
        env = _env(tmp_path, ledger, mode=PermissionMode.SELF)
        (env.ws / "memory/care").mkdir(parents=True)
        env.perm.grant(ScopeGrant(resource="path", action="write", selector="memory/care",
                                  source="autonomy:room@0900"))
        r = await env.run("write_file", {"path": "memory/care/today.md", "content": "她睡了。\n"},
                          origin="chime")
        assert r.success


class TestN1ScopedToolsGoThroughTheirOwnKey:
    @pytest.mark.parametrize("tool", ["write_file", "run_bash", "spawn_agent"])
    def test_scoped_tool_in_allows_tools_is_rejected(self, tmp_path, tool):
        _allows(tmp_path, f"  tools: [{tool}]\n")
        with pytest.raises(SkillAllowsError, match=tool):
            read_skill_allows("pet-cat", tmp_path)


class TestN2NullAllows:
    def test_null_allows_is_not_an_empty_boundary(self, tmp_path):
        _pet_skill(tmp_path, "---\nname: pet-cat\nallows:\n---\n")
        with pytest.raises(SkillAllowsError, match="allows"):
            read_skill_allows("pet-cat", tmp_path)

    def test_guard_tells_absent_from_null(self, tmp_path):
        from pathlib import PurePosixPath

        from loom.core.harness.approval import _skill_guard
        bare = "---\nname: pet-cat\n---\n"
        _pet_skill(tmp_path, bare)
        assert _skill_guard(PurePosixPath("skills/pet-cat/SKILL.md"), tmp_path,
                            "---\nname: pet-cat\nallows:\n---\n")


class TestN3ExecDeniesAbsolutePaths:
    def test_exec_grants_deny_absolute_paths(self, tmp_path):
        _pet_skill(tmp_path)
        fields = allows_permission_fields(read_skill_allows("pet-cat", tmp_path), "s")
        assert all(g["constraints"] == {"absolute_paths": "deny"}
                   for g in fields["scope_grants"] if g["resource"] == "exec")

    def test_a_narrowing_furniture_keeps_the_constraint(self, tmp_path):
        _pet_skill(tmp_path)
        narrow = MIJI + (f'scope_grants = [{{ resource = "exec", action = "execute",'
                         f' selector = "{PET} status" }}]\n')
        (miji,) = load_room(_room(tmp_path, narrow), workspace=tmp_path).furniture
        assert miji.scope_grants[0]["constraints"]["absolute_paths"] == "deny"

    async def test_absolute_path_argument_is_not_covered(self, tmp_path, ledger, monkeypatch, circ_dir):
        env = _env(tmp_path, ledger, mode=PermissionMode.SELF)
        _pet_skill(env.ws)
        req = await _wake(env.ws, monkeypatch)
        for g in req.scope_grants:
            env.perm.grant(ScopeGrant(resource=g["resource"], action=g["action"],
                                      selector=g["selector"], constraints=g.get("constraints", {}),
                                      source="autonomy:x"))
        r = await _call(env, "run_bash", {"command": f"{PET} status /etc/passwd"},
                        origin="chime", caps=ToolCapability.EXEC)
        assert not r.success


class TestN4RoomReporting:
    async def test_disabled_room_forgets_reported_errors(self, tmp_path, monkeypatch, circ_dir):
        monkeypatch.chdir(tmp_path)
        room._reported_errors.clear()
        p = _room(tmp_path, MIJI)
        now = datetime(2026, 11, 27, 2, 0, tzinfo=timezone.utc)

        async def runner(f, n):
            return [], None

        await room_tick(_Daemon(), CIRC, now, config=load_room(p, workspace=tmp_path), runner=runner)
        await room_tick(_Daemon(), CIRC, now, config=room.RoomConfig(), runner=runner)
        await room_tick(_Daemon(), CIRC, now, config=load_room(p, workspace=tmp_path), runner=runner)
        rows = [json.loads(line) for f in (circ_dir / "log").glob("room-*.jsonl")
                for line in f.read_text().splitlines()]
        assert len([r for r in rows if r["event"] == "furniture_rejected"]) == 2

    async def test_register_room_ticks_the_file_it_registered(self, tmp_path, monkeypatch, circ_dir):
        monkeypatch.chdir(tmp_path)
        p = tmp_path / "elsewhere.toml"
        p.write_text('[room]\nenabled = true\n[[furniture]]\nname = "c"\ncommand = ["true"]\n')
        seen = []

        async def fake_tick(daemon, circadian, now, *, config=None, runner=None):
            seen.append(config)

        monkeypatch.setattr(room, "room_tick", fake_tick)
        d = _Daemon()
        assert room.register_room(d, CIRC, p)
        await d.handlers[room.TICK_TRIGGER](None, {})
        assert seen and seen[0] is not None and [f.name for f in seen[0].furniture] == ["c"]

    def test_startup_and_first_tick_report_once(self, tmp_path, caplog, monkeypatch, circ_dir):
        import asyncio
        monkeypatch.chdir(tmp_path)
        room._reported_errors.clear()
        p = _room(tmp_path, MIJI + '\n[[furniture]]\nname = "c"\ncommand = ["true"]\n')

        async def runner(f, n):
            return [], None

        with caplog.at_level(logging.ERROR):
            room.register_room(_Daemon(), CIRC, p)
            asyncio.run(room_tick(_Daemon(), CIRC, datetime.now(timezone.utc),
                                  config=load_room(p, workspace=tmp_path), runner=runner))
        assert len([r for r in caplog.records if "miji" in r.getMessage()]) == 1
