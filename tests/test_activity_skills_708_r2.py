"""#715 R2 adversarial review regressions; all data is synthetic."""
import re
import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from loom.autonomy.chime import ChimeRequest
from loom.autonomy.skill_allows import read_skill_allows
from loom.core.harness.middleware import ToolCall, ToolResult
from loom.core.harness.mode import PermissionMode
from loom.core.harness.permissions import ToolCapability, TrustLevel
from loom.core.ledger.correlation import turn_scope
from tests.test_activity_skills_708 import PET, apply, config, fields, load, skill
from tests.test_approval_classifier_690_s6 import ledger, _no_network  # noqa: F401
from tests.test_permission_mode_704 import _env
from tests.test_single_decision_704_s3 import _call


def test_b1_documented_news_boundary_loads_with_an_unrelated_dirty_tmp(tmp_path):
    """Consume the actual migration YAML, rather than assert its spelling."""
    guide = Path(__file__).resolve().parents[1] / "docs/708-s2-skill-migration.md"
    block = re.findall(r"```yaml\n(.*?)```", guide.read_text(), re.S)[2]
    raw = yaml.safe_load(block)
    skill(tmp_path, "news-aggregator", raw["allows"]["exec"])
    (tmp_path / "skills/news-aggregator/SKILL.md").write_text("---\n" + block + "---\n")
    (tmp_path / "tmp").mkdir()
    (tmp_path / "tmp/.DS_Store").write_text("synthetic")
    boundary = read_skill_allows("news-aggregator", tmp_path)
    assert boundary.write == ("news",)
    item = load(config(tmp_path, "schedule", 'skills=["news-aggregator"]\nallowed_tools=["write_file"]\n'), "schedule")[0]
    assert [g["selector"] for g in fields(item, "scope_grants") if g["resource"] == "path"] == ["news"]


@pytest.mark.parametrize("kind", ["anchor", "schedule"])
@pytest.mark.parametrize("narrow", ["absent", "exec_only", "empty"])
def test_b2_write_declaration_requires_an_effective_write_scope(tmp_path, kind, narrow, caplog):
    skill(tmp_path, "pet-cat", [PET + " status"], ["news"] if narrow != "absent" else [])
    extra = 'skills=["pet-cat"]\nallowed_tools=["write_file"]\n'
    if narrow == "exec_only":
        extra += f'scope_grants=[{{resource="exec",action="execute",selector="{PET} status"}}]\n'
    elif narrow == "empty":
        extra += 'scope_grants=[]\n'
    items = load(config(tmp_path, kind, extra), kind)
    assert [fields(i, "name") for i in items] == ["healthy"]
    assert any(r.levelname == "ERROR" and "write_file" in r.message and "path:write" in r.message
               for r in caplog.records)


@pytest.mark.parametrize("mode", list(PermissionMode))
async def test_b2_incomplete_activity_never_delivers_write_then_exec(tmp_path, ledger, monkeypatch, mode):
    env = _env(tmp_path, ledger, mode=mode)
    monkeypatch.chdir(env.ws)
    skill(env.ws, "pet-cat", [PET + " status"])
    script = env.ws / "skills/pet-cat/pet.py"
    original = script.read_text()
    items = load(config(env.ws, "anchor", 'skills=["pet-cat"]\nallowed_tools=["write_file"]\n'), "anchor")
    bad = next((i for i in items if i.name == "dawn"), None)
    if bad is not None:  # reproduce the pre-fix exploit through the middleware
        apply(env, ChimeRequest(schedule_name="dawn", intent="Synthetic", fired_at=datetime.now(timezone.utc),
                               allowed_tools=bad.allowed_tools, scope_grants=bad.scope_grants))
        write = await env.run("write_file", {"path": "skills/pet-cat/pet.py", "content": "# replaced"})
        assert not write.success
    assert bad is None
    assert script.read_text() == original
    assert env.perm.grants == []


@pytest.mark.parametrize("mode", list(PermissionMode))
async def test_b2_active_nested_skill_script_cannot_be_replaced_before_exec(tmp_path, ledger, monkeypatch, mode):
    """A valid write prefix may contain the script: ceiling alone is insufficient."""
    env = _env(tmp_path, ledger, mode=mode)
    monkeypatch.chdir(env.ws)
    command = "python3 skills/demo/scripts/run.py status"
    skill(env.ws, "demo", [command], ["skills/demo/scripts", "news"])
    item = load(config(env.ws, "anchor", 'skills=["demo"]\nallowed_tools=["write_file"]\n'), "anchor")[0]
    added, source = apply(env, ChimeRequest(schedule_name="demo", intent="Synthetic", fired_at=datetime.now(timezone.utc),
                            allowed_tools=item.allowed_tools, scope_grants=item.scope_grants))
    script = env.ws / "skills/demo/scripts/run.py"
    original = script.read_text()
    executed = []

    async def executor(call):
        if call.tool_name == "write_file":
            target = env.ws / call.args["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(call.args["content"])
        else:
            executed.append(script.read_text())  # inspect, never execute a script
        return ToolResult(call_id=call.id, tool_name=call.tool_name, success=True)

    async def attempt(name, args, caps):
        call = ToolCall(tool_name=name, args=args, trust_level=TrustLevel.GUARDED,
                        capabilities=caps, origin="chime", session_id="t")
        with turn_scope("r2_sequence"):
            result = await env.blast.process(call, executor)
            await asyncio.sleep(0)
            return result

    write = await attempt("write_file", {"path": "skills/demo/scripts/run.py", "content": "# replaced"}, ToolCapability.MUTATES)
    run = await attempt("run_bash", {"command": command}, ToolCapability.EXEC)
    assert not write.success
    assert script.read_text() == original and run.success and executed == [original]
    assert (await attempt("write_file", {"path": "news/today.md", "content": "ok"}, ToolCapability.MUTATES)).success
    # It is scoped to a live routine's actual executable, not all project code.
    from loom.platform.discord.bot import LoomDiscordBot
    from types import SimpleNamespace
    LoomDiscordBot._revoke_chime_permissions(None, SimpleNamespace(perm=env.perm), added, source)
    if mode is PermissionMode.YOLO:
        assert (await attempt("write_file", {"path": "skills/demo/scripts/run.py", "content": "# edit after turn"}, ToolCapability.MUTATES)).success


@pytest.mark.parametrize("mode", list(PermissionMode))
@pytest.mark.parametrize("write_prefix,read_prefix,output,reference,allowed", [
    ("news", None, "news/cat.png", "news/ref.png", True),
    ("news", None, "outputs/cat.png", "news/ref.png", False),
    ("news", "references", "news/cat.png", "references/ref.png", True),
    ("news", "references", "news/cat.png", "news/ref.png", False),
    (None, "references", "news/cat.png", "references/ref.png", True),
    (None, "references", "news/cat.png", "news/ref.png", False),
])
async def test_n1_real_image_read_write_network_ceiling_matrix(
        tmp_path, ledger, mode, write_prefix, read_prefix, output, reference, allowed):
    from loom.core.harness.scope import ScopeGrant
    from loom.platform.cli.tools import _make_openai_image_scope_resolver
    from tests.test_single_decision_704_s3 import _register

    grants = [ScopeGrant(resource="exec", action="execute", selector=PET + " status", source="autonomy:news")]
    for action, prefix in [("write", write_prefix), ("read", read_prefix)]:
        if prefix:
            grants.append(ScopeGrant(resource="path", action=action, selector=prefix, source="autonomy:news"))
    env = _env(tmp_path, ledger, mode=mode, grants=grants)
    caps = ToolCapability.MUTATES | ToolCapability.NETWORK
    _register(env, "image_generate", resolver=_make_openai_image_scope_resolver(env.ws), caps=caps)
    env.perm.declare("autonomy:news", ["image_generate"], unattended_only=True)
    out = await _call(env, "image_generate", {"prompt": "cat", "output_path": output,
                      "subject_reference": [{"image_file": reference}]}, caps=caps)
    assert out.success is allowed
    assert env.confirm.await_count == 0 and env.model.requests == []


def test_n1_new_effect_does_not_hide_ceiling_from_rule_classification(tmp_path):
    from loom.core.harness.approval import classify_rules, RuleOutcome
    from loom.core.harness.permissions import PermissionContext
    from loom.core.harness.scope import ScopeGrant, ScopeRequest, ScopeRequirement, PermissionVerdict

    perm = PermissionContext(session_id="r2")
    perm.grant(ScopeGrant(resource="path", action="write", selector="news", source="autonomy:news"))
    call = ToolCall(tool_name="write_file", args={"path": "outputs/out.md", "content": "x"},
                    capabilities=ToolCapability.MUTATES, trust_level=TrustLevel.GUARDED, session_id="r2")
    request = ScopeRequest(tool_name="write_file", capabilities=call.capabilities, requirements=[
        ScopeRequirement(resource="path", action="write", selector="outputs"),
        ScopeRequirement(resource="network", action="connect", selector="example.com"),
    ])
    verdict = perm.evaluate(request, call.trust_level)
    assert verdict is PermissionVerdict.CONFIRM
    result = classify_rules(call, request, verdict, perm, tmp_path)
    assert result.outcome is RuleOutcome.REFUSE and result.reason == "scope_ceiling"


@pytest.mark.parametrize("kind", ["anchor", "schedule"])
@pytest.mark.parametrize("tools", ['[" memorize ", " write_file "]', '["memorize", " "]'])
def test_n4_tool_names_are_trimmed_and_empty_names_rejected(tmp_path, kind, tools):
    skill(tmp_path, "news-aggregator", [], ["news"])
    items = load(config(tmp_path, kind, f'skills=["news-aggregator"]\nallowed_tools={tools}\n'), kind)
    if '" "' in tools:
        assert [fields(i, "name") for i in items] == ["healthy"]
    else:
        assert fields(items[0], "allowed_tools") in (["memorize", "write_file"], ("memorize", "write_file"))


@pytest.mark.parametrize("legacy", [False, True])
def test_n3_nested_schedule_skills_rejects_instead_of_falling_back(tmp_path, legacy, caplog):
    extra = ('scope_grants=[{resource="exec",action="execute",selector="workspace"}]\n' if legacy else '')
    extra += '[schedules.target]\ntype="discord_thread"\nskills=["missing"]\n'
    items = load(config(tmp_path, "schedule", extra), "schedule")
    assert [i["name"] for i in items] == ["healthy"]
    assert any(r.levelname == "ERROR" and "target.skills" in r.message for r in caplog.records)


def test_n1_read_grant_does_not_authorize_the_same_path_for_writing():
    from loom.core.harness.permissions import PermissionContext
    from loom.core.harness.scope import ScopeGrant, ScopeRequest, ScopeRequirement, PermissionVerdict

    perm = PermissionContext(session_id="r2")
    perm.grant(ScopeGrant(resource="path", action="read", selector="news", source="autonomy:news"))
    request = ScopeRequest(tool_name="write_file", capabilities=ToolCapability.MUTATES, requirements=[
        ScopeRequirement(resource="path", action="write", selector="news")])
    assert perm.evaluate(request, TrustLevel.GUARDED) is PermissionVerdict.CONFIRM
    assert perm.diff(request).covered == [] and len(perm.diff(request).missing) == 1


@pytest.mark.parametrize("redirect,allowed", [("", True), (" 2>&1", False)])
async def test_n2_dawn_plain_command_works_but_real_reported_redirect_stays_denied(
        tmp_path, ledger, monkeypatch, redirect, allowed):
    from tests.test_activity_skills_708 import dawn_request, JOURNEY

    env = _env(tmp_path, ledger)
    apply(env, await dawn_request(env.ws, monkeypatch))
    result = await _call(env, "run_bash", {"command": JOURNEY + redirect}, caps=ToolCapability.EXEC)
    assert result.success is allowed


@pytest.mark.parametrize("bad", [".DS_Store", "count_limit"])
def test_b1_news_hidden_files_and_scan_limit_still_fail_closed(tmp_path, bad):
    from loom.autonomy.skill_allows import SkillAllowsError

    skill(tmp_path, "news-aggregator", [], ["news"])
    news = tmp_path / "news"
    news.mkdir()
    if bad == ".DS_Store":
        (news / bad).write_text("synthetic")
        pattern = "hidden_path"
    else:
        for i in range(2001):
            (news / f"report-{i}.md").touch()
        pattern = "more than 2000"
    with pytest.raises(SkillAllowsError, match=pattern):
        read_skill_allows("news-aggregator", tmp_path)
