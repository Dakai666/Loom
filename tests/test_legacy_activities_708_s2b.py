"""Consume S2b deployment artifacts through real loaders and permission seams.

Only synthetic workspaces/ledger and recording executors; no life CLI or API.
"""
from datetime import datetime, timezone
from pathlib import Path
import shutil

import pytest

from loom.autonomy.chime import ChimeRequest
from loom.autonomy.schedules import load_schedules
from loom.core.harness.mode import PermissionMode
from loom.core.harness.permissions import ToolCapability
from loom.autonomy.skill_allows import read_skill_allows, SkillAllowsError
from tests.test_activity_skills_708 import apply
from tests.test_approval_classifier_690_s6 import ledger, _no_network  # noqa: F401
from tests.test_permission_mode_704 import _env, _decisions
from tests.test_single_decision_704_s3 import _call
from tests.test_memory_documents import db  # noqa: F401

PACKAGE = Path(__file__).resolve().parents[1] / "docs/examples/708-s2b"
COMMANDS = {
    "skill_weekly_review": "loom skill weekly",
    "monthly_big_dream": "loom memory big-dream --execute --yes",
}


def install(ws):
    """Copy only tracked, public deployment artifacts to a synthetic workspace."""
    template = PACKAGE / "memory-housekeeping.SKILL.md"
    assert template.is_file(), "S2b housekeeping deployment template is missing"
    target = ws / "skills/memory-housekeeping"
    target.mkdir(parents=True)
    shutil.copyfile(template, target / "SKILL.md")
    tarot = PACKAGE / "sisi_mood_tarot.SKILL.md"
    assert tarot.is_file(), "S2b tarot deployment template is missing"
    target = ws / "skills/sisi_mood_tarot"
    target.mkdir(parents=True)
    shutil.copyfile(tarot, target / "SKILL.md")
    (target / "draw_and_write.py").write_text("# synthetic script, never executed\n")
    # Match the live metadata blocker without reading/copying live contents.
    (target / "log").mkdir()
    (target / "log/.gitkeep").write_text("synthetic\n")
    path = ws / "autonomy/schedules.toml"
    path.parent.mkdir(parents=True)
    snippet = PACKAGE / "schedules.toml"
    assert snippet.is_file(), "S2b schedule replacement tables are missing"
    shutil.copyfile(snippet, path)
    return {item["name"]: item for item in load_schedules(path)["schedules"]}


def request(item):
    return ChimeRequest(
        input_trust="local",  # item comes only from the reviewed operator registry template
        schedule_name=item["name"], intent=item["intent"],
        fired_at=datetime.now(timezone.utc),
        allowed_tools=tuple(item["allowed_tools"]), scope_grants=tuple(item["scope_grants"]),
    )


@pytest.mark.parametrize("mode", list(PermissionMode))
@pytest.mark.parametrize("origin", ["autonomy", "chime"])
async def test_housekeeping_report_scope_reaches_output_and_refuses_escape(
        tmp_path, ledger, monkeypatch, mode, origin):
    env = _env(tmp_path, ledger, mode=mode)
    monkeypatch.chdir(env.ws)
    items = install(env.ws)
    assert "memory_housekeeping" in items
    apply(env, request(items["memory_housekeeping"]))
    assert (await env.run("write_file", {
        "path": "outputs/self_check/2026-11-27-memory-corpus.md", "content": "Synthetic census",
    }, origin=origin)).success
    for path in ["outputs/elsewhere/report.md", "outputs/self_check/../outside.md",
                 "outputs/self_check/loom.toml",
                 "outputs/self_check/.hidden"]:
        assert not (await env.run("write_file", {"path": path, "content": "x"}, origin=origin)).success, path
    assert env.confirm.await_count == 0


@pytest.mark.parametrize("name,prefix", [
    ("shared_learning", "news"), ("curiosity", "outputs"), ("deep_weave", "outputs"),
])
@pytest.mark.parametrize("mode", list(PermissionMode))
async def test_retained_research_anchor_keeps_general_tools_and_existing_write_ceiling(
        tmp_path, ledger, monkeypatch, name, prefix, mode, caplog):
    from loom.autonomy.circadian.rhythm import load_rhythm
    from loom.autonomy.circadian.lifecycle import _deliver_phase_chime
    from loom.autonomy.circadian import state as st
    from tests.test_circadian_phase_chime import _make_daemon, CFG
    from tests.test_single_decision_704_s3 import _register
    from loom.platform.cli.tools import _fetch_url_resolver

    env = _env(tmp_path, ledger, mode=mode)
    monkeypatch.chdir(env.ws)
    path = env.ws / "autonomy/circadian/rhythm.toml"
    path.parent.mkdir(parents=True)
    snippet = PACKAGE / "rhythm.toml"
    assert snippet.is_file(), "S2b retained anchor tables are missing"
    shutil.copyfile(snippet, path)
    anchors = load_rhythm(path)
    assert len(anchors) == 3
    anchor = next(a for a in anchors if a.name == name)
    daemon, requests, _ = _make_daemon()
    st.set_dir_for_test(env.ws / "circadian-state")
    try:
        await _deliver_phase_chime(daemon, CFG, anchor)
    finally:
        st.set_dir_for_test(None)
    apply(env, requests[0])
    assert anchor.open_slot and requests[0].open_slot
    assert env.perm.grants == []
    assert env.perm.declared_tools() == frozenset()
    from loom.core.harness.approval import _routine_ceiling
    assert not _routine_ceiling(env.perm, "path", "write")
    assert not any("legacy scope_grants without skills" in r.message for r in caplog.records)


@pytest.mark.parametrize("name", ["daily_mood_tarot", "memory_housekeeping",
                                 "skill_weekly_review", "monthly_big_dream"])
async def test_schedule_planner_daemon_delivery_preserves_loaded_boundary(tmp_path, ledger, monkeypatch, name):
    from tests.test_circadian_phase_chime import _make_daemon
    from loom.core.harness.middleware import ToolResult
    from loom.core.ledger.correlation import turn_scope
    from types import SimpleNamespace
    from loom.autonomy.fire_record import FireOutcome

    env = _env(tmp_path, ledger)
    monkeypatch.chdir(env.ws)
    items = install(env.ws)
    config = env.ws / "loom.toml"
    config.write_text('[autonomy]\nenabled=true\n')
    daemon, _, _ = _make_daemon()
    assert daemon.load_config(config) == 4
    trigger = next(t for t in daemon.evaluator.list() if t.name == name)
    plan = await daemon._planner.handle(trigger, {})
    observed = []

    async def run(messages, **kwargs):
        # Run the real independent-schedule entry path; only the session/model
        # boundary is stubbed, then the real resolver/middleware decides calls.
        del messages, kwargs
        if name == "memory_housekeeping":
            result = await env.run("write_file", {"path": "outputs/self_check/report.md", "content": "x"}, origin="autonomy")
        else:
            command = COMMANDS.get(name, "python3 skills/sisi_mood_tarot/draw_and_write.py")
            args = {"command": command}
            if name == "monthly_big_dream":
                args["timeout"] = 10800
            result = await _call(env, "run_bash", args, origin="autonomy", caps=ToolCapability.EXEC)
        observed.append(result.success)
        return SimpleNamespace(text="synthetic done")

    # _run_agent expects an async stream. Retain the genuine grant/revoke seam.
    async def stream_turn(*args, **kwargs):
        await run(args, **kwargs)
        if False:
            yield ToolResult(call_id="unused", tool_name="unused", success=True)

    daemon._session = SimpleNamespace(perm=env.perm, registry=env.blast._registry, workspace=env.ws,
                                     stream_turn=stream_turn)
    with turn_scope("schedule_s2b"):
        result = await daemon._run_agent(plan)
    assert result.outcome is FireOutcome.COMPLETED
    assert observed == [True]
    assert env.perm.grants == []
    assert env.perm.declared_by("write_file", attended=False) is None


@pytest.mark.parametrize("name", ["sisi_mood_tarot", "memory-housekeeping"])
async def test_real_load_skill_reads_template_without_adding_grants_or_changing_mode(
        tmp_path, ledger, db, monkeypatch, name):
    from loom.core.harness.middleware import ToolCall
    from loom.core.memory.procedural import ProceduralMemory, SkillGenome
    from loom.platform.cli.skill_tools import make_load_skill_tool
    from loom.core.ledger.correlation import turn_scope

    env = _env(tmp_path, ledger)
    monkeypatch.chdir(env.ws)
    items = install(env.ws)
    schedule = "daily_mood_tarot" if name == "sisi_mood_tarot" else "memory_housekeeping"
    apply(env, request(items[schedule]))
    proc = ProceduralMemory(db)
    await proc.upsert(SkillGenome(name=name, body=(env.ws / "skills" / name / "SKILL.md").read_text()))
    tool = make_load_skill_tool(proc, skills_dirs=[env.ws / "skills"])
    env.blast._registry.register(tool)
    before = list(env.perm.grants)
    mode = env.perm.mode
    call = ToolCall(tool_name=tool.name, args={"name": name}, origin="autonomy",
                    trust_level=tool.trust_level, capabilities=tool.capabilities, session_id="t")
    with turn_scope("load_s2b"):
        result = await env.blast.process(call, tool.executor)
    assert result.success
    assert f'skill_content name="{name}"' in result.output
    assert env.perm.grants == before
    assert env.perm.mode is mode


@pytest.mark.parametrize("mode", list(PermissionMode))
@pytest.mark.parametrize("origin", ["autonomy", "chime"])
async def test_tarot_exec_only_keeps_plain_script_without_native_write_grants(
        tmp_path, ledger, monkeypatch, mode, origin):
    env = _env(tmp_path, ledger, mode=mode)
    monkeypatch.chdir(env.ws)
    items = install(env.ws)
    assert "daily_mood_tarot" in items
    item = items["daily_mood_tarot"]
    assert item["allowed_tools"] == []
    apply(env, request(item))
    command = "python3 skills/sisi_mood_tarot/draw_and_write.py"
    script = env.ws / "skills/sisi_mood_tarot/draw_and_write.py"
    original = script.read_text()
    assert (await _call(env, "run_bash", {"command": command}, origin=origin,
                        caps=ToolCapability.EXEC)).success
    assert (await _decisions(ledger))[-1]["decision_source"] == "grant"
    for candidate in ["ls -la skills/sisi_mood_tarot/ 2>&1 | head -30",
                      "test -f skills/sisi_mood_tarot/draw_and_write.py && " + command,
                      "ls skills/sisi_mood_tarot/ && " + command,
                      "cd skills/sisi_mood_tarot && python3 draw_and_write.py",
                      command + " 2>&1", command + " && echo x", command + " | cat",
                      "date", "python3 skills/sisi_mood_tarot/other.py"]:
        assert not (await _call(env, "run_bash", {"command": candidate}, origin=origin,
                                caps=ToolCapability.EXEC)).success
    # No path grant/declaration: model rejection is not the scope contract.
    assert not any(g.resource == "path" for g in env.perm.grants)
    assert env.perm.declared_by("write_file", attended=False) is None
    # Active script and guarded frontmatter remain protected in every mode.
    for path, content in [(str(script.relative_to(env.ws)), "# replaced"),
                          ("skills/sisi_mood_tarot/SKILL.md", "---\nallows: {}\n---\n")]:
        assert not (await env.run("write_file", {"path": path, "content": content}, origin=origin)).success
    assert script.read_text() == original
    assert env.confirm.await_count == 0


@pytest.mark.parametrize("prefix,blocker", [
    ("skills/sisi_mood_tarot", None),
    ("skills/sisi_mood_tarot/log", ".gitkeep"),
    ("skills/sisi_mood_tarot/backups", "room.toml"),
    ("personalities", "AGENTS.md"),
    ("personalities", ".hidden"),
    ("loom/core/personalities", "loom.toml"),
    ("skills/sisi_mood_tarot/backups", "symlink"),
])
def test_b4_real_loader_still_rejects_control_hidden_symlink_and_skill_root(tmp_path, prefix, blocker):
    import yaml
    install(tmp_path)
    target = tmp_path / prefix
    target.mkdir(parents=True, exist_ok=True)
    if blocker == "symlink":
        (target / "link").symlink_to(tmp_path / "outside")
    elif blocker:
        (target / blocker).write_text("synthetic")
    path = tmp_path / "skills/sisi_mood_tarot/SKILL.md"
    path.write_text("---\n" + yaml.safe_dump({"allows": {"write": [prefix]}}) + "---\n")
    with pytest.raises(SkillAllowsError):
        read_skill_allows("sisi_mood_tarot", tmp_path)


@pytest.mark.parametrize("prefix", ["personalities", "loom/core/personalities",
                                    "skills/sisi_mood_tarot/backups"])
def test_b4_path_string_alone_does_not_imply_control_or_personality_refusal(tmp_path, prefix):
    import yaml
    install(tmp_path)
    # A clean directory, unlike the log/.gitkeep case, is loadable. This does
    # not grant Tarot native writes: the approved template remains exec-only.
    target = tmp_path / prefix
    target.mkdir(parents=True, exist_ok=True)
    (target / "synthetic.md").write_text("synthetic")
    path = tmp_path / "skills/sisi_mood_tarot/SKILL.md"
    path.write_text("---\n" + yaml.safe_dump({"allows": {"write": [prefix]}}) + "---\n")
    assert read_skill_allows("sisi_mood_tarot", tmp_path).write == (prefix,)


@pytest.mark.parametrize("mode", list(PermissionMode))
@pytest.mark.parametrize("origin", ["autonomy", "chime"])
@pytest.mark.parametrize("name", list(COMMANDS))
async def test_narrowed_legacy_cli_keeps_original_invocation_and_rejects_extra_effects(
        tmp_path, ledger, monkeypatch, mode, origin, name, caplog):
    env = _env(tmp_path, ledger, mode=mode)
    monkeypatch.chdir(env.ws)
    items = install(env.ws)
    assert name in items
    apply(env, request(items[name]))
    command = COMMANDS[name]
    args = {"command": command}
    if name == "monthly_big_dream":
        args["timeout"] = 10800
    assert (await _call(env, "run_bash", args, origin=origin, caps=ToolCapability.EXEC)).success
    assert env.ran[-1].args == args  # original flags/timeout reach executor unchanged
    assert (await _decisions(ledger))[-1]["decision_source"] == "grant"
    for candidate in [command + " --batch 1", command + " --db /tmp/other.db",
                      command + " --no-write", command + " 2>&1", command + " && echo x",
                      command + " | cat", "cd . && " + command, "date", "loom memory health"]:
        assert not (await _call(env, "run_bash", {"command": candidate},
                                origin=origin, caps=ToolCapability.EXEC)).success
    if name == "monthly_big_dream":
        for candidate in ["loom memory big-dream", "loom memory big-dream --execute",
                          "loom memory big-dream --yes", "loom memory big-dream --yes --execute"]:
            assert not (await _call(env, "run_bash", {"command": candidate},
                                    origin=origin, caps=ToolCapability.EXEC)).success
    assert any("legacy scope_grants without skills" in r.message and name in r.message
               for r in caplog.records)
    assert env.confirm.await_count == 0
